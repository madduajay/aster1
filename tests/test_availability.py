import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from availability.company import Stock,exception_delay
from availability.contracts import Task,digest,criteria,identity
from availability.security import Security
from availability.store import Store
from availability.subagent import Agent
from availability.gateway import Gateway
from personal_agent.service import Service
from personal_agent.agent import Engine,ResearchDecision
from personal_agent.memory import Memory
from conftest import SummaryModel,StubBrowsers
from test_agent import ScriptedModel,wait_service

D={'venue':'Carbone','location':'New York','date':'2099-12-01','party_size':2}
H={'hotel':'The Fifth Avenue Hotel','destination':'New York','check_in':'2099-12-01','check_out':'2099-12-03','guests':2,'rooms':1}
F={'origin':'JFK','destination':'Dubai','departure_date':'2099-12-01','passengers':2,'cabin':'first class','carrier':'Emirates'}


@pytest.mark.parametrize('domain,c,delay',[
    ('dining',D,10),('dining',{**D,'venue':'CARBONE NYC','location':'Manhattan'},10),
    ('dining',{**D,'location':'Las Vegas'},0),('dining',{**D,'venue':'Nobu'},0),
    ('hotels',H,20),('hotels',{**H,'hotel':'5th Avenue Hotel'},20),('hotels',{**H,'hotel':'The Plaza'},0),
    ('hotels',{**H,'hotel':'Fifth avenue new york hotel'},20),
    ('hotels',{**H,'hotel':'5th Ave NYC Hotel'},20),
    ('hotels',{**H,'hotel':'The Langham New York Fifth Avenue'},0),
    ('hotels',{**H,'destination':'Paris'},0),('flights',F,30),('flights',{**F,'destination':'DXB','carrier':'EK','cabin':'first'},30),
    ('flights',{**F,'cabin':'business'},0),('flights',{**F,'destination':'London'},0),('flights',{**F,'carrier':'Qatar'},0)])
def test_precise_inventory_exceptions(domain,c,delay):assert exception_delay(domain,c)==delay


async def test_fifth_avenue_alias_repairs_initial_inventory_but_retains_twenty_second_opening(tmp_path):
    clock=[100.];path=tmp_path/'hotel.sqlite';security=Security(tmp_path)
    c={**H,'hotel':'Fifth avenue new york hotel'}
    stock=Stock(path,'hotels',security,lambda:clock[0])
    stale={'query_id':'missed-alias','domain':'hotels','criteria':c,'available':True,'revision':0,'options':[{'name':c['hotel']}]}
    stock.store.execute('INSERT INTO inventory(id,criteria,result) VALUES(?,?,?)',('missed-alias',json.dumps(c),json.dumps(stale)))
    restored=Stock(path,'hotels',security,lambda:clock[0])
    assert json.loads(restored.store.one('SELECT result FROM inventory')['result'])['available'] is False
    assert restored.store.one('SELECT due FROM inventory')['due'] is None
    assert restored.search('missed-alias',{**H,'hotel':'The Fifth Avenue Hotel'})['available'] is False
    restored.watch('missed-alias',c)
    assert restored.store.one('SELECT due FROM inventory')['due']==120
    clock[0]=119;await restored.deliver(httpx.MockTransport(lambda request:httpx.Response(200)))
    assert restored.search('missed-alias',c)['available'] is False
    clock[0]=120;await restored.deliver(httpx.MockTransport(lambda request:httpx.Response(200)))
    opened=Stock(path,'hotels',security,lambda:clock[0])
    assert opened.search('missed-alias',c)['available'] is True
    assert opened.search('missed-alias',H)['revision']==1


async def test_user_named_fifth_avenue_city_alias_is_not_replaced_by_generic_discovery(database,users):
    service=service_for(database,[AIMessage(content='Hi')]);uid=users[0]['id'];sid=database.new_session(uid)
    try:
        database.message(uid,sid,'user','Check Fifth avenue new york hotel December 1 to 5, 2099, two guests, one room.')
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('hotel-alias',?,?,'running',?)",(uid,sid,time.time()))
        query=service.availability.envelope(uid,'hotel-alias',[{'domain':'hotels','criteria':{**H,'hotel':'Fifth avenue new york hotel'}}],'travel').queries[0]
        assert query.criteria['hotel']=='Fifth avenue new york hotel'
    finally:await service.close();await service.engine.client.close()


async def test_company_persists_opening_and_retries_signed_outbox_without_reset(tmp_path):
    clock=[100.];security=Security(tmp_path);stock=Stock(tmp_path/'company.sqlite','dining',security,lambda:clock[0])
    assert not stock.search('query-123',D)['available']
    clock[0]=200.;assert stock.store.one('SELECT due FROM inventory')['due'] is None
    stock.watch('query-123',D);assert stock.store.one('SELECT due FROM inventory')['due']==210
    clock[0]=205.;stock.watch('query-123',D);assert stock.store.one('SELECT due FROM inventory')['due']==210
    delivered=[]
    def failure(request):delivered.append(request);return httpx.Response(503)
    clock[0]=210.;await stock.deliver(httpx.MockTransport(failure))
    assert stock.search('query-123',D)['available']
    assert not stock.store.one('SELECT delivered FROM inventory')['delivered']
    restored=Stock(tmp_path/'company.sqlite','dining',security,lambda:clock[0])
    def success(request):
        assert security.verify('dining',request.content,request.headers)
        assert request.url.path=='/webhooks/company/dining'
        assert json.loads(request.content)['criteria_hash']==digest(identity('dining',D))
        delivered.append(request);return httpx.Response(200)
    await restored.deliver(httpx.MockTransport(success));await restored.deliver(httpx.MockTransport(success))
    assert len(delivered)==2 and restored.search('query-123',D)['available']
    assert restored.store.one('SELECT delivered FROM inventory')['delivered']==1


def test_only_nondefault_required_details_need_input():
    assert criteria('flights',{})[1]==['origin','destination','departure_date','cabin']
    hotel,missing=criteria('hotels',{'hotel':'Plaza'})
    assert not missing and hotel['rooms']==1 and hotel['guests']==2
    with pytest.raises(ValueError):criteria('hotels',{**H,'check_out':H['check_in']})


def model_for(*indices,domains=None):
    domains=domains or {0:'hotels',1:'flights'}
    return ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'search_'+domains[i],'args':{'query_index':i},'id':'check-'+str(i),'type':'tool_call'} for i in indices]),AIMessage(content='Checked.')])


def envelope(queries,user='user1',rid='request-123'):
    return Task(request_id=rid,user_id=user,session_id='session1',source_message_id=1,queries=queries,preferences={'cuisine':'Italian'})


async def test_travel_harness_checks_independent_categories_and_isolates_scope(tmp_path,monkeypatch):
    stocks={d:Stock(tmp_path/(d+'.sqlite'),d,Security(tmp_path)) for d in ('hotels','flights')};calls=[]
    async def company(security,domain,tool,args):
        calls.append((domain,tool,args))
        if domain=='flights':raise ConnectionError('Temporarily unavailable')
        return stocks[domain].search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    model=model_for(0,1);agent=Agent('travel',tmp_path,model,InMemorySaver())
    task=envelope([{'domain':'hotels','criteria':H},{'domain':'flights','criteria':F}])
    result=await agent.query(task)
    assert [r['status'] for r in result['results']]==['completed','error']
    assert {d for d,_,_ in calls}=={'hotels','flights'}
    assert not result['results'][0]['available'] and 'available' not in result['results'][1]
    assert all('task' not in str(m.content) for m in model.seen[0] if m.type=='tool')
    assert all(r['user_id']=='user1' for r in agent.store.rows('SELECT * FROM queries'))
    with pytest.raises(ValueError):await agent.query(task.model_copy(update={'user_id':'user2'}))
    with pytest.raises(ValueError):await agent.query(envelope([{'domain':'dining','criteria':D}]))


async def test_missing_flight_fields_do_not_block_hotel_search(tmp_path,monkeypatch):
    calls=[];stock=Stock(tmp_path/'hotels.sqlite','hotels',Security(tmp_path))
    async def company(security,domain,tool,args):calls.append(domain);return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    agent=Agent('travel',tmp_path,model_for(0),InMemorySaver())
    response=await agent.query(envelope([{'domain':'hotels','criteria':{**H,'hotel':'Plaza'}},{'domain':'flights','criteria':{'destination':'Dubai'}}]))
    assert calls==['hotels'] and response['results'][0]['available']
    assert response['results'][1]['status']=='needs_input' and 'origin' in response['results'][1]['missing_fields']


def test_inbox_rejects_invalid_correlations_and_ignores_cancelled(tmp_path):
    store=Store(tmp_path/'shared.sqlite');task=envelope([{'domain':'dining','criteria':D}])
    stock=Stock(tmp_path/'company.sqlite','dining',Security(tmp_path));result=stock.search('query1',D)
    store.save_query('query1',task,'dining',D,result)
    store.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)',('query1','user1','request1','dining','av-main-test',time.time()+100,'active',time.time()))
    event={**result,'available':True,'options':[{'name':'Carbone','times':['19:00']}],'revision':1,'event_id':'opening:query1','criteria_hash':digest(identity('dining',D))}
    store.accept_event(event);store.accept_event(event)
    assert len(store.rows('SELECT * FROM inbox'))==1
    with pytest.raises(ValueError):store.accept_event({**event,'domain':'hotels'})
    with pytest.raises(ValueError):store.accept_event({**event,'criteria_hash':'wrong'})
    with pytest.raises(ValueError):store.accept_event({**event,'revision':0})
    store.execute("UPDATE subscriptions SET state='cancelled'")
    assert store.accept_event(event) is False


def service_for(database,responses):
    engine=Engine(database,StubBrowsers(),InMemorySaver(),'sk-test',model=ScriptedModel(responses=responses))
    service=Service(database,engine);service.memory=Memory(database,SummaryModel());return service


async def test_main_uses_subagent_and_renders_company_values_not_model_claims(database,users):
    service=service_for(database,[AIMessage(content='',tool_calls=[{'name':'delegate_travel','args':{'queries':[{'domain':'hotels','criteria':H}]},'id':'travel','type':'tool_call'}]),AIMessage(content='Guaranteed Presidential Suite at $1!')])
    calls=[]
    async def api(role,path,user,body=None):
        calls.append((role,path))
        task=Task.model_validate(body);q=task.queries[0];c=q.criteria
        result=Stock(database.path.parent/'hotelstock.sqlite','hotels',service.availability.security).search(q.query_id or 'av-'+digest([task.user_id,task.session_id,task.request_id,q.domain,c])[:40],c)
        result.update(available=True,options=[{'name':'The Fifth Avenue Hotel','room_type':'Deluxe room','check_in':c['check_in'],'check_out':c['check_out']}])
        return {'request_id':task.request_id,'results':[result]}
    service.availability.api=api
    try:
        uid=users[0]['id'];sid=database.new_session(uid)
        run=(await service.submit(uid,sid,'Check The Fifth Avenue Hotel in New York December 1–3, 2099, for two guests, one room.'))['run_id']
        await wait_service(service,run)
        reply=database.rows("SELECT content FROM messages WHERE role='assistant'")[-1]['content']
        assert calls==[('travel','/v1/availability/query')]
        assert 'Deluxe room' in reply and 'Presidential' not in reply and '$1' not in reply
        assert not service.availability.store.rows('SELECT * FROM monitors')
    finally:await service.close();await service.engine.client.close()


async def test_projection_is_atomic_deduplicated_user_scoped_and_cancel_safe(database,users):
    service=service_for(database,[AIMessage(content='Hi')]);gateway=service.availability
    uid=users[0]['id'];sid=database.new_session(uid);database.message(uid,sid,'user','Check availability.')
    q={'id':'query1','user_id':uid,'session_id':sid}
    result={'domain':'dining','criteria':D,'status':'completed','available':True,'options':[{'name':'Carbone','times':['19:00']}]}
    try:
        gateway.project(q,result);gateway.project(q,result)
        assert len(database.rows('SELECT * FROM notifications'))==1
        assert len(database.rows('SELECT * FROM availability_chat_messages'))==1
        gateway.store.execute('DELETE FROM changes') # A lost cache cannot duplicate durable main chat/alerts.
        gateway.project(q,result)
        assert len(database.rows('SELECT * FROM notifications'))==1
        assert not service.background.snapshot(users[1]['id'])['notifications']
        database.execute('UPDATE notifications SET read_at=?',(time.time(),))
        gateway.project(q,result)
        assert not service.background.snapshot(uid)['notifications']
        assert len(service.background.snapshot(uid)['chat_updates'])==1
        gateway.store.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)',('query2',uid,'request1','dining','av-main-test',time.time()+100,'cancelled',time.time()))
        gateway.project({**q,'id':'query2'},result)
        assert len(database.rows('SELECT * FROM notifications'))==1
    finally:await service.close();await service.engine.client.close()


async def test_monitor_checks_are_fresh_and_report_failure_recovery_only_once(database,users):
    service=service_for(database,[AIMessage(content='Hi')]);gateway=service.availability
    uid=users[0]['id'];sid=database.new_session(uid);source=database.message(uid,sid,'user','Monitor the Plaza every five minutes.')
    c={**H,'hotel':'The Plaza'};mid='monitor-failure-test'
    task=Task(request_id='monitor-failure-request',user_id=uid,session_id=sid,source_message_id=source,scope_id=mid,queries=[{'domain':'hotels','criteria':c}])
    qid='av-'+digest([uid,sid,mid,'hotels',c])[:40]
    stock=Stock(database.path.parent/'hotelstock.sqlite','hotels',gateway.security);available=stock.search(qid,c)
    gateway.store.execute('INSERT INTO monitors(id,user_id,session_id,envelope,expires,state,last_result,schedule_id) VALUES(?,?,?,?,?,?,?,?)',
        (mid,uid,sid,task.model_dump_json(),time.time()+300,'active',json.dumps({'results':[available]}),'test-schedule'))
    failure=[True];calls=[]
    async def api(role,path,user,body=None):
        calls.append(body['request_id'])
        result={'query_id':qid,'domain':'hotels','criteria':c,'status':'error','error':'ConnectionError'} if failure[0] else stock.search(qid,c)
        return {'request_id':body['request_id'],'results':[result]}
    gateway.api=api
    try:
        await gateway.poll({'monitor_id':mid});await gateway.poll({'monitor_id':mid})
        assert len(database.rows('SELECT * FROM notifications'))==1
        failure[0]=False
        await gateway.poll({'monitor_id':mid});await gateway.poll({'monitor_id':mid})
        assert len(database.rows('SELECT * FROM notifications'))==2
        assert len(set(calls))==4 # Every check invoked the subagent anew.
        assert 'Deluxe room' in database.rows('SELECT body FROM notifications')[-1]['body']
        await gateway.cancel(mid,uid);await gateway.poll({'monitor_id':mid})
        assert len(calls)==4
        gateway.store.execute("UPDATE monitors SET state='active',expires=? WHERE id=?",(time.time()-1,mid))
        await gateway.poll({'monitor_id':mid})
        assert gateway.store.one('SELECT state FROM monitors WHERE id=?',(mid,))['state']=='expired'
        assert len(calls)==4 and len(service.background.snapshot(uid)['chat_updates'])==2
    finally:await service.close();await service.engine.client.close()


async def test_repeat_query_and_alias_reuse_open_inventory_until_explicit_cancellation(database,users):
    service=service_for(database,[AIMessage(content='Hi')]);gateway=service.availability
    uid=users[0]['id'];sid=database.new_session(uid);source=database.message(uid,sid,'user','Check Carbone for 2.')
    clock=[100.];stock=Stock(database.path.parent/'provider.sqlite','dining',gateway.security,lambda:clock[0])
    try:
        def task(run,c):
            database.execute("UPDATE runs SET status='completed'")
            database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,uid,sid,time.time()))
            return gateway.envelope(uid,run,[{'domain':'dining','criteria':c}],'dining')
        first=task('first',D);qid=first.queries[0].query_id
        initial=stock.search(qid,D);stock.watch(qid,D)
        gateway.store.save_query(qid,first,'dining',D,initial)
        gateway.store.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)',(qid,uid,'first-dining','dining','av-main-first',time.time()+100,'active',time.time()))
        alias={**D,'venue':'CARBONE NYC','location':'NYC'}
        second=task('second',alias)
        assert second.queries[0].query_id==qid
        assert not stock.search(qid,alias)['available']
        await gateway.prepare_watches(second,{'results':[initial]}) # Must reuse existing parent, no Temporal call.
        assert stock.store.one('SELECT due FROM inventory')['due']==110
        clock[0]=110
        await stock.deliver(httpx.MockTransport(lambda request:httpx.Response(200)))
        assert stock.search(qid,alias)['available']
        await gateway.cancel_watch(qid,uid)
        third=task('third',D)
        assert third.queries[0].query_id!=qid
    finally:await service.close();await service.engine.client.close()


async def test_combined_monitor_silently_drops_expired_categories(database,users):
    service=service_for(database,[AIMessage(content='Hi')]);gateway=service.availability
    uid=users[0]['id'];sid=database.new_session(uid);source=database.message(uid,sid,'user','Monitor hotel and flight availability.')
    expired={**H,'check_in':'2000-01-01','check_out':'2000-01-02'};flight={**F,'cabin':'business'};mid='mixed-expiry-monitor'
    task=Task(request_id='mixed-expiry-request',user_id=uid,session_id=sid,source_message_id=source,scope_id=mid,queries=[{'domain':'hotels','criteria':expired},{'domain':'flights','criteria':flight}])
    qid='av-'+digest([uid,sid,mid,'flights',flight])[:40]
    stock=Stock(database.path.parent/'flights.sqlite','flights',gateway.security);available=stock.search(qid,flight)
    gateway.store.execute('INSERT INTO monitors(id,user_id,session_id,envelope,expires,state,last_result,schedule_id) VALUES(?,?,?,?,?,?,?,?)',
        (mid,uid,sid,task.model_dump_json(),time.time()+300,'active',json.dumps({'results':[{'domain':'hotels','criteria':expired,'status':'completed','available':False,'options':[]},available]}),'test-schedule'))
    calls=[]
    async def api(role,path,user,body=None):
        calls.append(body['queries'])
        return {'request_id':body['request_id'],'results':[stock.search(qid,flight)]}
    gateway.api=api
    try:
        await gateway.poll({'monitor_id':mid})
        assert len(calls)==1 and [q['domain'] for q in calls[0]]==['flights']
        assert not database.rows('SELECT * FROM notifications')
        assert gateway.monitors(uid)[0]['queries'][0]['state']=='expired'
    finally:await service.close();await service.engine.client.close()
