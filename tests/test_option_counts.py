import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from availability.option_counts import requested,policy
from availability.company import Stock
from availability.contracts import Task
from availability.security import Security
from availability.subagent import Agent
from availability.presentation import availability_text
from availability.research import extract
from availability.followup import party_counts
from test_agent import ScriptedModel


@pytest.mark.parametrize('text,domain,count',[
    ('Show 2 hotels and 5 flights','hotels',2),('Show 2 hotels and 5 flights','flights',5),
    ('Show five hotel options for 2 adults','hotels',5),('Show 5 options for flights','flights',5),
    ('Show 4-star hotels for 2 guests, 1 room','hotels',None),('Find 2 hotel rooms','hotels',None),
    ('Find business flights for 2 passengers','flights',None),('Show 7 options','hotels',7),
    ('Show 4 options for hotels','flights',None),('Show five hotels and flights','flights',5),
    ('Show 6 each for hotels and flights','hotels',6),('Show 15 hotels','hotels',15)])
def test_option_counts_are_not_occupancy_or_star_ratings(text,domain,count):assert requested(text,domain)==count


def test_list_lengths_are_not_misread_as_people():
    assert not party_counts('Look for 5 hotels and 6 flight options')
    assert party_counts('Look for 5 hotels for 2 people')=={2}
    assert party_counts('Dinner for 4')=={4}


def test_default_counts_vary_and_remain_stable_within_a_run(database,users,monkeypatch):
    sequence=iter([0,3]);monkeypatch.setattr('availability.option_counts.secrets.randbelow',lambda n:next(sequence))
    uid=users[0]['id'];sid=database.new_session(uid)
    for rid in ('count-run-a','count-run-b','count-run-c'):database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'completed',?)",(rid,uid,sid,time.time()))
    first=policy(database,'count-run-a','Show hotel options',['hotels'])
    assert first['hotels']=={'count':4,'explicit':False}
    assert policy(database,'count-run-a','Show hotel options',['hotels'])==first
    assert policy(database,'count-run-b','Show hotel options',['hotels'])['hotels']['count']==7
    assert policy(database,'count-run-c','Show 2 hotels',['hotels'])['hotels']=={'count':2,'explicit':True}


async def test_more_than_three_candidates_survive_extraction():
    names=['Hotel '+str(i) for i in range(7)];answer='\n'.join(names);url='https://example.com/hotels'
    data={'candidates':[{'name':name,'evidence_quote':name,'source_urls':[url],'stars':4,'connections':[],'route_verified':False} for name in names]}
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(data)))))
    result=await extract(client,'hotels',answer,[{'url':url}],{'_option_limit':7,'stars':4})
    assert len(result)==7


@pytest.mark.parametrize('wanted,available',[ (2,8),(7,8),(15,20),(7,3)])
async def test_subagent_honors_counts_batches_and_truthful_shortfall(tmp_path,monkeypatch,wanted,available):
    research_limits=[];stock=Stock(tmp_path/'company.sqlite','hotels',Security(tmp_path))
    async def search(client,domain,c,*args):
        excluded=c.get('_exclude_names',[]);names=[f'Hotel {i}' for i in range(available) if f'Hotel {i}' not in excluded][:_limit(c)]
        research_limits.append(c['_option_limit'])
        return {'status':'completed','answer':' '.join(names),'sources':[{'url':'https://example.com/hotels','title':'Hotels'}],
            'candidates':[{'name':n,'stars':4,'evidence_quote':n,'connections':[],'source_urls':['https://example.com/hotels']} for n in names]}
    def _limit(c):return c['_option_limit']
    async def company(security,domain,name,args):return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.research.search',search);monkeypatch.setattr('availability.subagent.call_company',company)
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query_index':0},'id':'web','type':'tool_call'}]),AIMessage(content='',tool_calls=[{'name':'search_hotels','args':{'query_index':0},'id':'check','type':'tool_call'}]),AIMessage(content='Done')])
    task=Task(request_id='counted-hotels-request',user_id='user1',session_id='session1',source_message_id=1,
        queries=[{'domain':'hotels','criteria':{'destination':'Paris','check_in':'2099-12-03','check_out':'2099-12-21','guests':2,'rooms':1}}],preferences={'option_counts':{'hotels':{'count':wanted,'explicit':True}}})
    response=await Agent('travel',tmp_path,model,InMemorySaver(),research_client=object()).query(task)
    assert len(response['results'][0]['options'])==min(wanted,available)
    text=availability_text([response])
    assert ('you asked for '+str(wanted)) in text if available<wanted else 'you asked for' not in text
    if wanted>10:assert research_limits==[10,5]
