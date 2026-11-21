"""Actual MCP -> DeepAgent HTTP -> webhook -> Temporal -> chat integration."""
import asyncio
import json
import os
import socket
import time

import httpx
import pytest
import uvicorn
from temporalio.client import ScheduleOverlapPolicy
from temporalio.testing import WorkflowEnvironment

from availability.company import create_company
from availability.contracts import AGENT_PORTS,COMPANY_PORTS,Task,WatchRequest
from availability.subagent import create_subagent
from personal_agent import config
from test_availability import D,H,F,envelope,model_for,service_for
from langchain_core.messages import AIMessage
from test_agent import ScriptedModel

pytestmark=pytest.mark.skipif(os.environ.get('RUN_AVAILABILITY_TESTS')!='1',reason='Enable independent availability services integration tests')


async def serve(app):
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if task.done():await task
        if server.started:return server,task,port
        await asyncio.sleep(.01)
    raise TimeoutError('Local test service did not become ready')


async def until(condition,timeout=15):
    async with asyncio.timeout(timeout):
        while not condition():await asyncio.sleep(.05)


async def test_three_services_events_restart_dedup_schedules_and_cancel(database,users,monkeypatch):
    environment=await WorkflowEnvironment.start_local(download_dest_dir=str(config.ROOT/'.runtime/bin'),dev_server_database_filename=str(database.path.parent/'temporal.sqlite'))
    monkeypatch.setattr(config,'TEMPORAL_ADDRESS',environment.client.service_client.config.target_host)
    service=service_for(database,[AIMessage(content='Checked.')]);gateway=service.availability;servers=[];providers={};agents={}
    try:
        for domain in ('dining','hotels','flights'):
            app=create_company(domain,database.path.parent);server,task,port=await serve(app);servers.append((server,task))
            monkeypatch.setitem(COMPANY_PORTS,domain,port);providers[domain]=app
        for role,indices in [('dining',[0]),('travel',[0,1])]:
            app=create_subagent(role,database.path.parent,model_for(*indices,domains={0:'dining'} if role=='dining' else None),environment.client)
            server,task,port=await serve(app);servers.append((server,task));agents[role]=app
            monkeypatch.setitem(AGENT_PORTS,role,port)
        await gateway.start()
        uid=users[0]['id'];sid=database.new_session(uid);source=database.message(uid,sid,'user','Find dining, hotels and flights.')
        task=Task(request_id='integration-request',user_id=uid,session_id=sid,source_message_id=source,queries=[{'domain':'dining','criteria':{**D,'preferred_time':'7:00 PM'}},{'domain':'hotels','criteria':{**H,'hotel':'Fifth avenue new york hotel'}},{'domain':'flights','criteria':F}])
        result=await gateway.check(task)
        assert len(result['results'])==3 and all(not r['available'] for r in result['results'])
        assert json.loads(providers['dining'].state.stock.store.one('SELECT criteria FROM inventory')['criteria'])['preferred_time']=='19:00'
        assert not database.rows('SELECT * FROM notifications')
        registered=time.monotonic();await gateway.prepare_watches(task,result)
        await until(lambda:all(app.state.stock.store.one('SELECT due FROM inventory')['due'] for app in providers.values()))
        dues={d:app.state.stock.store.one('SELECT due FROM inventory')['due'] for d,app in providers.items()}
        assert 9<=dues['hotels']-dues['dining']<=11 and 19<=dues['flights']-dues['dining']<=21
        # Main worker is offline when the first callback arrives. Company and
        # subagent services continue; Temporal persists the signal for restart.
        await gateway.close()
        dining_server,dining_task=servers[-2]
        dining_server.should_exit=True;await dining_task
        await until(lambda:providers['dining'].state.stock.store.one('SELECT result FROM inventory') and json.loads(providers['dining'].state.stock.store.one('SELECT result FROM inventory')['result'])['available'],timeout=15)
        assert providers['dining'].state.stock.store.one('SELECT delivered FROM inventory')['delivered']==0
        app=create_subagent('dining',database.path.parent,model_for(0,domains={0:'dining'}),environment.client)
        server,restarted,port=await serve(app);servers.append((server,restarted));agents['dining']=app
        monkeypatch.setitem(AGENT_PORTS,'dining',port)
        await until(lambda:agents['dining'].state.agent.store.one("SELECT id FROM inbox WHERE state='done'"))
        assert time.monotonic()-registered>=9.5
        assert not database.rows('SELECT * FROM notifications')
        await gateway.start()
        await until(lambda:len(database.rows('SELECT * FROM notifications'))==3,timeout=28)
        assert time.monotonic()-registered>=29.5
        assert len(service.background.snapshot(uid)['chat_updates'])==3
        assert all(r['session_id']==sid for r in service.background.snapshot(uid)['chat_updates'])
        assert not service.background.snapshot(users[1]['id'])['chat_updates']
        await asyncio.wait_for(environment.client.get_workflow_handle('av-main-'+task.request_id).result(),5)
        # Retried webhook and scheduled observation of the same opening do not
        # create extra chat messages. Search remains opened after provider restart.
        for domain,app in providers.items():
            row=app.state.stock.store.one('SELECT * FROM inventory')
            assert app.state.stock.search(row['id'],json.loads(row['criteria']))['available']
            inbox=agents['dining' if domain=='dining' else 'travel'].state.agent.store.one('SELECT * FROM inbox WHERE query_id=?',(row['id'],))
            agent=agents['dining' if domain=='dining' else 'travel'].state.agent
            agent.store.accept_event(json.loads(inbox['payload']))
            await gateway.publish({'query_id':row['id'],'event':json.loads(inbox['payload'])})
            # Replay the narrow crash window after main completed but before
            # the subagent persisted its forwarding acknowledgement.
            agent.store.execute("UPDATE subscriptions SET state='active' WHERE id=?",(row['id'],))
            await agent.forward({'request':{'query_id':row['id']},'event':json.loads(inbox['payload'])})
            assert agent.store.one('SELECT state FROM subscriptions WHERE id=?',(row['id'],))['state']=='opened'
        assert len(database.rows('SELECT * FROM notifications'))==3
        # Explicit monitoring uses native Temporal Schedules, and executes the
        # same live DeepAgent endpoint immediately and on every triggered check.
        monitor_task=task.model_copy(update={'request_id':'monitor-request','queries':task.queries[:1]})
        monitor=await gateway.create_monitor(monitor_task,'')
        mid=monitor['monitor_id'];record=gateway.store.one('SELECT * FROM monitors WHERE id=?',(mid,))
        monitor_qid=monitor['results'][0]['query_id']
        await until(lambda:providers['dining'].state.stock.store.one('SELECT due FROM inventory WHERE id=?',(monitor_qid,))['due'])
        due=providers['dining'].state.stock.store.one('SELECT due FROM inventory WHERE id=?',(monitor_qid,))['due']
        repeated=await gateway.create_monitor(monitor_task,'')
        assert repeated['monitor_id']==mid and repeated['results']
        assert providers['dining'].state.stock.store.one('SELECT due FROM inventory WHERE id=?',(monitor_qid,))['due']==due
        assert len(gateway.store.rows('SELECT * FROM monitors'))==1
        schedule=await environment.client.get_schedule_handle(record['schedule_id']).describe()
        assert schedule.schedule.policy.overlap==ScheduleOverlapPolicy.SKIP
        assert schedule.schedule.policy.catchup_window.total_seconds()==300
        assert schedule.schedule.spec.intervals[0].every.total_seconds()==300
        assert 285<record['next_check']-record['anchor']<315
        before=len(database.rows('SELECT * FROM notifications'))
        checks=len(agents['dining'].state.agent.store.rows("SELECT * FROM tasks WHERE envelope LIKE ?",('%'+mid+'%',)))
        await environment.client.get_schedule_handle(record['schedule_id']).trigger()
        await until(lambda:len(agents['dining'].state.agent.store.rows("SELECT * FROM tasks WHERE envelope LIKE ?",('%'+mid+'%',)))>checks)
        assert len(database.rows('SELECT * FROM notifications'))==before
        await gateway.cancel(mid,uid)
        assert gateway.store.one('SELECT state FROM monitors WHERE id=?',(mid,))['state']=='cancelled'
        # Signed but late events are accepted/ignored after cancellation.
        qid=monitor['results'][0]['query_id'];provider=providers['dining'].state.stock
        provider.store.execute('UPDATE inventory SET due=? WHERE id=?',(time.time()-1,qid))
        await provider.deliver();await asyncio.sleep(.5)
        assert len(database.rows('SELECT * FROM notifications'))==before
        # API authentication, cross-user retrieval, forged webhook rejection.
        async with httpx.AsyncClient() as http:
            url=f'http://127.0.0.1:{AGENT_PORTS["dining"]}'
            assert (await http.get(url+'/v1/requests/integration-request-dining')).status_code==401
            assert (await http.get(url+'/v1/requests/integration-request-dining',headers=gateway.security.headers(users[1]['id']))).status_code==404
            assert (await http.post(url+'/webhooks/company/dining',json={'available':True})).status_code==401
        # A generic monitored hotel query discovers two properties. Its
        # unavailable child must signal the already-started main receiver.
        async def hotel_research(*args,**kwargs):return {'status':'completed','answer':'The Fifth Avenue Hotel and The Plaza.',
            'sources':[{'url':'https://example.com/nyc','title':'NYC hotels'}],
            'candidates':[{'name':name,'stars':5,'connections':[],'source_urls':['https://example.com/nyc'],'evidence_quote':name,'route_verified':False} for name in ('The Fifth Avenue Hotel','The Plaza')]}
        monkeypatch.setattr('availability.research.search',hotel_research)
        travel=agents['travel'].state.agent;travel.research_client=object()
        travel.model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query_index':0},'id':'web','type':'tool_call'}]),
            AIMessage(content='',tool_calls=[{'name':'search_hotels','args':{'query_index':0},'id':'check','type':'tool_call'}]),AIMessage(content='Checked')])
        generic={k:v for k,v in H.items() if k!='hotel'}
        hotel_task=task.model_copy(update={'request_id':'mixed-hotel-monitor','queries':[__import__('availability.contracts',fromlist=['Query']).Query(domain='hotels',criteria=generic)]})
        hotel_monitor=await gateway.create_monitor(hotel_task,'')
        children=hotel_monitor['results'][0]['candidate_results']
        assert hotel_monitor['results'][0]['available'] and len(children)==2
        unavailable=next(r for r in children if not r['available']);assert unavailable['criteria']['hotel']=='The Fifth Avenue Hotel'
        await until(lambda:len(database.rows('SELECT * FROM notifications'))==before+1,timeout=25)
        assert database.rows('SELECT body FROM notifications')[-1]['body'].startswith('The Fifth Avenue Hotel')
        await gateway.poll({'monitor_id':hotel_monitor['monitor_id']})
        assert len(database.rows('SELECT * FROM notifications'))==before+1
        await gateway.cancel(hotel_monitor['monitor_id'],uid)
    finally:
        await service.close();await service.engine.client.close()
        for server,task in reversed(servers):server.should_exit=True;await task
        await environment.shutdown()
