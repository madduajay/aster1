import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from availability.contracts import Task,criteria
from availability.followup import continuation_roles,room_counts,party_counts,cabins
from availability.subagent import Agent
from test_availability import H,F,D,service_for,model_for
from test_agent import ScriptedModel,wait_service
from personal_agent.agent import ResearchDecision


@pytest.mark.parametrize('text,domain,missing,expected',[
    ('1 room','hotels',['rooms'],{'travel'}),('one room','hotels',['rooms'],{'travel'}),
    ('2 adults','hotels',['guests'],{'travel'}),('2 passengers','flights',['passengers'],{'travel'}),
    ('From Phoenix','flights',['origin'],{'travel'}),('Business class','flights',['cabin'],{'travel'}),
    ('any airport is fine','flights',['origin'],{'travel'}),
    ('2026-12-01','dining',['date'],{'dining'}),('2 people','dining',['party_size'],{'dining'}),
    ('hello','hotels',['rooms'],set()),('My favorite number is 1','hotels',['rooms'],set()),
    ('1 room','flights',['origin'],set()),('Cancel my trip','hotels',['rooms'],set())])
def test_narrow_missing_detail_continuations(text,domain,missing,expected):
    assert continuation_roles(text,[{'domain':domain,'missing_fields':missing}])==expected


@pytest.mark.parametrize('text,rooms,party',[
    ('Two adults, one room',{1},{2}),('a double room for my wife and me',{1},{2}),
    ('a couple of rooms for three of us',{2},{3}),('2 adults and 1 child in one suite',{1},{3}),
    ('hotel for 2 nights',set(),set()),('2 guests, 1 room; flight for 1',{1},{1,2}),
    ('I want a hotel in Dubai',set(),set())])
def test_counts_require_user_evidence_instead_of_defaults(text,rooms,party):
    assert room_counts(text)==rooms
    assert party_counts(text)==party


def test_business_trip_does_not_automatically_mean_business_cabin():
    assert not cabins('I am taking a business trip')
    assert cabins('Business class please')=={'business'}


@pytest.mark.parametrize('domain,c,resource,role',[
    ('hotels',{**H,'hotel':'Hilton'},'hotel','travel'),
    ('flights',{**F,'cabin':'business','connections':['London']},'carrier','travel'),
    ('dining',{**D,'venue':'Nobu'},'venue','dining')])
async def test_main_shortlist_cannot_bypass_subagent_discovery(database,users,domain,c,resource,role):
    service=service_for(database,[AIMessage(content='Hi')]);uid=users[0]['id'];sid=database.new_session(uid)
    try:
        for index,explicit in enumerate((False,True)):
            database.message(uid,sid,'user',('Find '+c[resource] if explicit else 'Recommend '+domain)+' for 2 guests, 2 passengers, 1 room, business class')
            run='discovery-'+str(index)
            database.execute("UPDATE runs SET status='completed' WHERE user_id=?",(uid,))
            database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,uid,sid,time.time()))
            query=service.availability.envelope(uid,run,[{'domain':domain,'criteria':c}],role).queries[0]
            if domain=='flights':assert 'connections' not in query.criteria
            if explicit:assert query.criteria[resource]==c[resource]
            else:
                assert resource not in query.criteria and query.research
                assert 'connections' not in query.criteria
    finally:await service.close();await service.engine.client.close()


async def test_main_discards_malformed_optional_connections_before_delegating(database,users):
    service=service_for(database,[AIMessage(content='Hi')]);uid=users[0]['id'];sid=database.new_session(uid)
    try:
        database.message(uid,sid,'user','Business class flights from Phoenix to Dubai for 2 passengers')
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('route-error',?,?,'running',?)",(uid,sid,time.time()))
        query=service.availability.envelope(uid,'route-error',[{'domain':'flights','criteria':{**F,'connections':'nonstop'}}],'travel').queries[0]
        assert 'connections' not in query.criteria
        assert criteria('flights',query.criteria)[1]==[]
    finally:await service.close();await service.engine.client.close()


@pytest.mark.parametrize('user_text,property_name,expected_hotel',[
    ('Find availability for hyatt grand central hotel new york December 1 to 3, 2099, 2 people, 1 room','Hyatt Grand Central New York','Hyatt Grand Central New York'),
    ('Check Hyatt Grand Central NYC for 2 guests, 1 room','Hyatt Grand Central New York','Hyatt Grand Central New York'),
    ('Check The Plaza hotel in New York for 2 guests, 1 room','The Plaza','The Plaza'),
    ('Recommend New York hotels for 2 guests, 1 room','Hyatt Grand Central New York',None),
    ('Check Hyatt Grand Central New York for 2 guests, 1 room','Park Hyatt New York','Hyatt Grand Central New York')])
async def test_hotel_mentions_tolerate_generic_words_without_allowing_substitution(database,users,user_text,property_name,expected_hotel):
    service=service_for(database,[AIMessage(content='Hi')]);uid=users[0]['id'];sid=database.new_session(uid)
    try:
        database.message(uid,sid,'user',user_text)
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('property-mention',?,?,'running',?)",(uid,sid,time.time()))
        q=service.availability.envelope(uid,'property-mention',[{'domain':'hotels','criteria':{**H,'hotel':property_name}}],'travel').queries[0]
        assert ('hotel' in q.criteria)==bool(expected_hotel)
        if expected_hotel:
            from availability.targets import hotel_key
            assert hotel_key(q.criteria['hotel'])==hotel_key(expected_hotel)
    finally:await service.close();await service.engine.client.close()


@pytest.mark.parametrize('reply,status,partial,origin,cabin',[
    ('From Phoenix','needs_input',{'destination':'Paris'},'Phoenix','economy'),
    ('any airport is fine','needs_input',{'destination':'Paris'},'Los Angeles','economy'),
    ('Make that business class','completed',{'cabin':'business'},'Phoenix','business')])
async def test_server_preserves_trip_when_model_omits_supplied_fields(database,users,reply,status,partial,origin,cabin):
    uid=users[0]['id'];sid=database.new_session(uid)
    previous={'destination':'Paris','departure_date':'2099-12-01','passengers':1,'cabin':'economy'}
    if status=='completed':previous['origin']='Phoenix'
    source=database.message(uid,sid,'user','Check economy flights '+('from Phoenix ' if status=='completed' else 'from LA ' if reply=='any airport is fine' else '')+'to Paris on 2099-12-01, 1 passenger.')
    service=service_for(database,[AIMessage(content='',tool_calls=[{'name':'delegate_travel','args':{'queries':[{'domain':'flights','criteria':partial}]},'id':'flight','type':'tool_call'}]),AIMessage(content='Acknowledged.')])
    task=Task(request_id='flight-prior-request',user_id=uid,session_id=sid,source_message_id=source,queries=[{'domain':'flights','criteria':previous}])
    prior={'domain':'flights','criteria':previous,'status':status,'available':True,'options':[]}
    if status=='needs_input':prior['missing_fields']=['origin']
    service.availability.store.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task.request_id,uid,sid,'travel',task.model_dump_json(),json.dumps({'results':[prior]}),time.time()))
    if reply=='any airport is fine':
        # Recover even after a previous follow-up already repeated the question.
        repeated=database.message(uid,sid,'user','any airport is fine')
        again=task.model_copy(update={'request_id':'repeated-airport-question','source_message_id':repeated})
        service.availability.store.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(again.request_id,uid,sid,'travel',again.model_dump_json(),json.dumps({'results':[prior]}),time.time()+.01))
    router=AsyncMock(return_value=ResearchDecision(requires_research=False,travel_availability=False))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router))
    calls=[]
    async def api(role,path,user,body):
        q=body['queries'][0];c=q['criteria'];calls.append(c)
        return {'request_id':body['request_id'],'results':[{'domain':'flights','criteria':c,'query_id':q['query_id'],'status':'completed','available':True,
            'options':[{'carrier':'Air France','flight_number':'AF1','origin':c['origin'],'destination':c['destination'],'cabin':c['cabin'],'departure':c['departure_date']+'T12:00:00'}]}]}
    service.availability.api=api
    try:
        rid=(await service.submit(uid,sid,reply))['run_id'];await wait_service(service,rid)
        assert len(calls)==1
        assert calls[0]['origin']==origin and calls[0]['cabin']==cabin
        assert calls[0]['departure_date']=='2099-12-01' and calls[0]['passengers']==1
        assert 'AF1' in database.rows("SELECT content FROM messages WHERE role='assistant'")[-1]['content']
    finally:await service.close();await service.engine.client.close()


@pytest.mark.parametrize('reply',['1 room','continue'])
async def test_one_room_resumes_pending_hotel_even_if_router_misses_followup(database,users,reply):
    uid=users[0]['id'];sid=database.new_session(uid)
    source=database.message(uid,sid,'user','Find The Plaza in Dubai December 1–3, 2099, two guests.')
    c={**H,'hotel':'The Plaza','destination':'Dubai'};incomplete={k:v for k,v in c.items() if k!='rooms'}
    response=AIMessage(content='',tool_calls=[{'name':'delegate_travel','args':{'queries':[{'domain':'hotels','criteria':c}]},'id':'travel','type':'tool_call'}])
    service=service_for(database,[response,AIMessage(content='Got it, I’ll use one room.')])
    task=Task(request_id='pending-hotel-request',user_id=uid,session_id=sid,source_message_id=source,queries=[{'domain':'hotels','criteria':incomplete}])
    pending={'results':[{'domain':'hotels','criteria':incomplete,'status':'needs_input','missing_fields':['rooms']}]}
    service.availability.store.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task.request_id,uid,sid,'travel',task.model_dump_json(),json.dumps(pending),time.time()))
    router=AsyncMock(return_value=ResearchDecision(requires_research=False,travel_availability=False))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router))
    calls=[]
    async def api(role,path,user,body):
        calls.append(body);q=body['queries'][0]
        result={'query_id':q['query_id'],'domain':'hotels','criteria':c,'status':'completed','available':True,
            'options':[{'name':'The Plaza','room_type':'Deluxe room','check_in':c['check_in'],'check_out':c['check_out']}]}
        return {'request_id':body['request_id'],'results':[result]}
    service.availability.api=api
    try:
        if reply=='continue':
            database.message(uid,sid,'user','1 room')
            database.message(uid,sid,'assistant','Got it. I’ll use one room.')
        run=(await service.submit(uid,sid,reply))['run_id'];await wait_service(service,run)
        assert len(calls)==1 and calls[0]['queries'][0]['criteria']==c
        assert service.engine.model.choices[0]=={'type':'function','function':{'name':'delegate_travel'}}
        assert 'Deluxe room' in database.rows("SELECT content FROM messages WHERE role='assistant'")[-1]['content']
        assert not service.availability.pending(users[1]['id'],sid)
        assert not service.availability.pending(uid,sid,floor=source)
        assert not service.availability.store.rows('SELECT * FROM monitors')
    finally:await service.close();await service.engine.client.close()


async def test_subagent_web_search_research_and_company_results_remain_separate(tmp_path,monkeypatch):
    calls=[]
    async def research(client,domain,c,focus,domains):
        calls.append(('web',domain));return {'status':'completed','answer':'The Plaza has a central location.',
            'sources':[{'title':'The Plaza','url':'https://www.theplazany.com/','source_kind':'independent'}]}
    async def company(security,domain,name,args):
        calls.append(('company',domain));return {'query_id':args['query_id'],'domain':domain,'criteria':args['criteria'],
            'status':'completed','available':False,'options':[],'revision':0,'provenance':'Company hotels'}
    monkeypatch.setattr('availability.research.search',research);monkeypatch.setattr('availability.subagent.call_company',company)
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query_index':0},'id':'web','type':'tool_call'}]),
        AIMessage(content='',tool_calls=[{'name':'search_hotels','args':{'query_index':0},'id':'company','type':'tool_call'}]),AIMessage(content='Checked.')])
    agent=Agent('travel',tmp_path,model,InMemorySaver(),research_client=object())
    task=Task(request_id='research-test-request',user_id='user1',session_id='session1',source_message_id=1,queries=[{'domain':'hotels','criteria':H,'research':True}])
    result=await agent.query(task)
    assert calls==[('web','hotels'),('company','hotels')]
    assert result['results'][0]['available'] is False and result['results'][0]['options']==[]
    assert result['research'][0]['sources'][0]['url']=='https://www.theplazany.com/'
