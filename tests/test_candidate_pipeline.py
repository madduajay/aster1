import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from availability.company import Stock
from availability.contracts import Task,candidate_id,leaves
from availability.research import extract
from availability.security import Security
from availability.subagent import Agent
from availability.presentation import availability_text
from test_agent import ScriptedModel


def candidate(name,stars=4):return {'name':name,'stars':stars,'connections':[],'evidence_quote':name,'source_urls':['https://example.com/hotels'],'route_verified':False}


@pytest.mark.parametrize('failed',[False,True])
async def test_researched_hotels_are_checked_by_exact_name_and_not_rendered_twice(tmp_path,monkeypatch,failed):
    c={'destination':'Dubai','check_in':'2099-12-01','check_out':'2099-12-10','guests':2,'rooms':1,'stars':4}
    evidence={'status':'completed','answer':'Research-only list must not be appended.','sources':[{'url':'https://example.com/hotels','title':'Hotels'}],
        'candidates':[candidate('Hotel Alpha'),candidate('Hotel Beta'),candidate('Luxury Five Star',5)]}
    async def research(*a,**k):return evidence
    monkeypatch.setattr('availability.research.search',research)
    calls=[];stock=Stock(tmp_path/'company.sqlite','hotels',Security(tmp_path))
    async def company(security,domain,name,args):
        calls.append(args)
        if failed and args['criteria']['hotel']=='Hotel Alpha':raise ConnectionError('unreachable')
        return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query_index':0},'id':'web','type':'tool_call'}]),
        AIMessage(content='',tool_calls=[{'name':'search_hotels','args':{'query_index':0},'id':'check','type':'tool_call'}]),AIMessage(content='Done')])
    agent=Agent('travel',tmp_path,model,InMemorySaver(),research_client=object())
    task=Task(request_id='candidates-request',user_id='user1',session_id='session1',source_message_id=1,queries=[{'domain':'hotels','criteria':c}])
    response=await agent.query(task);result=response['results'][0]
    assert {r['criteria']['hotel'] for r in calls}=={'Hotel Alpha','Hotel Beta'}
    assert all(r['criteria']['stars']==4 for r in calls)
    assert {r['query_id'] for r in calls}=={candidate_id(result['query_id'],'hotels',r['criteria']) for r in calls}
    text=availability_text([response])
    assert 'Research-only list' not in text and 'Dubai Hotel' not in text and 'Luxury Five Star' not in text
    assert text.count('Hotel Beta')==1 and text.count('Hotel Alpha')==1
    assert result['available'] is True
    if failed:assert len(result['options'])==1 and leaves(response['results'])[0]['status']=='error'


async def test_candidate_extraction_rejects_unquoted_names_wrong_stars_and_unknown_links():
    data={'candidates':[candidate('Hotel Alpha'),candidate('Invented'),candidate('Hotel Five',5),{**candidate('Hotel Beta'),'source_urls':['https://invented.com']} ]}
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(data)))))
    result=await extract(client,'hotels','Hotel Alpha and Hotel Five and Hotel Beta.',[{'url':'https://example.com/hotels','title':'Hotels'}],{'stars':4})
    assert [r['name'] for r in result]==['Hotel Alpha']


async def test_flight_monitor_rechecks_its_previously_verified_route(tmp_path,monkeypatch):
    c={'origin':'Phoenix','destination':'Dubai','departure_date':'2099-12-01','passengers':2,'cabin':'business'}
    evidence={'status':'completed','sources':[{'url':'https://example.com/hotels','title':'Route'}],
        'candidates':[{**candidate('British Airways'),'connections':['London'],'route_verified':True}]}
    monkeypatch.setattr('availability.research.search',AsyncMock(return_value=evidence))
    stock=Stock(tmp_path/'company.sqlite','flights',Security(tmp_path));calls=[]
    async def company(security,domain,name,args):
        calls.append(args);return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    for index in range(2):
        model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query_index':0},'id':'web','type':'tool_call'}]),
            AIMessage(content='',tool_calls=[{'name':'search_flights','args':{'query_index':0},'id':'check','type':'tool_call'}]),AIMessage(content='Done')])
        agent=Agent('travel',tmp_path,model,InMemorySaver(),research_client=object())
        task=Task(request_id='poll-flight-'+str(index),scope_id='monitor1',user_id='user1',session_id='session1',source_message_id=1,
            queries=[{'domain':'flights','criteria':c,'query_id':'av-stable-flight-query'}])
        response=await agent.query(task)
        assert response['results'][0]['available']
    assert len(calls)==2 and calls[0]==calls[1]


async def test_route_extraction_does_not_join_alternative_hubs_or_accept_mixed_cabin():
    quotes=['Airline Alpha via LAX, DFW or JFK.','Airline Beta via YYZ, mixed-cabin business itinerary.','Airline Gamma via DFW then DOH.']
    names=['Airline Alpha','Airline Beta','Airline Gamma']
    data={'candidates':[{**candidate(name),'connections':connections,'route_verified':True,'evidence_quote':quote}
        for name,connections,quote in zip(names,[['LAX','DFW','JFK'],['YYZ'],['DFW','DOH']],quotes)]}
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(data)))))
    result=await extract(client,'flights','\n'.join(quotes),[{'url':'https://example.com/hotels','title':'Routes'}],{'cabin':'business'})
    assert [r['name'] for r in result]==['Airline Gamma']


async def test_partner_route_names_can_be_separated_by_segment_descriptions():
    quote='PHX → MUC → DXB, with Lufthansa on PHX–MUC and Emirates on MUC–DXB.'
    data={'candidates':[{**candidate('Lufthansa + Emirates'),'connections':['MUC'],'route_verified':True,'evidence_quote':quote}]}
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(data)))))
    result=await extract(client,'flights',quote,[{'url':'https://example.com/hotels','title':'Routes'}],{'cabin':'business'})
    assert result[0]['name']=='Lufthansa + Emirates' and result[0]['connections']==['MUC']
