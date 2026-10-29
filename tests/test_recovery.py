import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent import browser_tasks as module
from personal_agent.browser_recovery import combine_observations, input_need, RecoveryBudget
from personal_agent.browser_policy import action_binding, classify_action
from personal_agent.browser_tasks import BrowserTasks, Worker
from personal_agent.agent import Engine
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel,wait_service
from test_browser_tasks import StubBrowserHost,task_row,observed


def test_active_login_frame_wins_over_background_error():
    background={'url':'https://booking.example/venue','text':'Unfortunately we cannot accommodate that reservation','fields':[]}
    phone={'url':'https://login.booking.example/login','text':'Enter your mobile phone number','fields':[{'type':'tel','empty':True}],'focused':True}
    view=combine_observations(background,[phone])
    assert view['text']=='Enter your mobile phone number'
    assert 'accommodate' not in view['text'] and 'accommodate' in view['background_text']
    assert input_need(view)['kind']=='information'
    phone['manual_required']=True
    assert input_need(combine_observations(background,[phone]))['kind']=='manual'
    assert classify_action('request_user',{},view)[0]=='input'
    assert classify_action('extract',{},dict(view,manual_required=True))[0]=='read'


def test_recovery_budget_allows_alternatives_and_stops_loops():
    budget=RecoveryBudget(max_failures=4,max_same=2)
    state={'url':'https://example.com','text':'unchanged'}
    assert budget.failure('click',{'index':1},state,'stale')
    assert budget.failure('click',{'index':1},state,'stale')
    assert not budget.failure('click',{'index':1},state,'stale')
    assert budget.failure('click',{'index':2},state,'fresh target')
    assert not budget.failure('click',{'index':3},state,'another target')
    assert len(budget.journal)==5


async def test_input_boundary_ownership_replay_and_restart(database,users,monkeypatch):
    monkeypatch.setattr(module,'public_url',AsyncMock())
    manager=BrowserTasks(database,StubBrowserHost(),'sk-test',None)
    user=users[0]['id'];task_row(database,user)
    manager.workers['browser-task']=Worker(SimpleNamespace(returncode=None),'nonce',user,'browser-run',asyncio.Lock())
    manager.send=AsyncMock()
    state=observed('Phone');params={'kind':'information','prompt':'Which phone number should I use?'}
    await manager.before('browser-task',{'seq':1,'operation':'request_user','params':params,'observed':state,'fingerprint':action_binding('request_user',params,state)})
    row=database.one('SELECT * FROM browser_use_actions WHERE seq=1')
    assert row['category']=='input' and row['status']=='proposed' and manager.send.await_count==0
    database.execute("UPDATE runs SET status='input' WHERE id='browser-run'")
    manager.recover()
    assert manager.owned(user,'browser-run','browser-task')['status']=='input'
    assert database.one('SELECT status FROM browser_use_actions WHERE id=?',(row['id'],))['status']=='proposed'
    with pytest.raises(LookupError):await manager.answer_input(users[1]['id'],'browser-run','browser-task',row['id'],{'type':'browser_input','text':'+1 202 555 0100'})
    await manager.answer_input(user,'browser-run','browser-task',row['id'],{'type':'browser_input','text':'+1 202 555 0100'})
    assert manager.send.call_args.args[1]['response']=='+1 202 555 0100'
    with pytest.raises(LookupError):await manager.answer_input(user,'browser-run','browser-task',row['id'],{'type':'browser_input','text':'another'})
    assert manager.send.await_count==1


async def test_secret_requests_are_routed_to_manual_takeover(database,users,monkeypatch):
    monkeypatch.setattr(module,'public_url',AsyncMock())
    manager=BrowserTasks(database,StubBrowserHost(),'sk-test',None)
    user=users[0]['id'];task_row(database,user)
    manager.workers['browser-task']=Worker(SimpleNamespace(returncode=None),'nonce',user,'browser-run',asyncio.Lock())
    manager.send=AsyncMock();state=observed()
    params={'kind':'information','prompt':'What is your verification code?'}
    await manager.before('browser-task',{'seq':1,'operation':'request_user','params':params,'observed':state,'fingerprint':action_binding('request_user',params,state)})
    desc=json.loads(database.one('SELECT description FROM browser_use_actions')['description'])
    assert desc['kind']=='manual' and 'out of chat' in desc['prompt']


class InputSimulation(BrowserTasks):
    async def start(self,user,run,call_id,objective,domains,inspect_only=False):
        previous=self.db.one('SELECT id FROM browser_use_tasks WHERE call_id=?',(call_id,))
        if previous:return previous['id']
        tid='input-test-task'
        self.db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES(?,?,?,?,?,?,0,'running',0)",(tid,user,run,call_id,objective,json.dumps(domains)))
        self.workers[tid]=Worker(SimpleNamespace(returncode=None),'nonce',user,run,asyncio.Lock())
        self.simulation=asyncio.create_task(self.simulate(tid))
        return tid
    async def simulate(self,tid):
        state=observed('Phone');params={'kind':'information','prompt':'Which phone number should I use?'}
        await self.before(tid,{'seq':1,'operation':'request_user','params':params,'observed':state,'fingerprint':action_binding('request_user',params,state)})
    async def send(self,tid,payload):
        assert payload['response']=='+1 202 555 0100'
        self.db.execute("UPDATE browser_use_tasks SET status='completed',result=? WHERE id=?",(json.dumps({'status':'verified','verification':{'success':True,'reason':'Controlled confirmation'}}),tid))
        self.workers[tid].changed.set()


async def test_langgraph_input_survives_reload_and_does_not_become_memory(database,users,tmp_path,monkeypatch):
    monkeypatch.setattr(module,'public_url',AsyncMock())
    user=users[0]['id'];sid=database.new_session(user)
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Complete the requested reservation','official_domains':['example.com']},'id':'input-delegate','type':'tool_call'}]),AIMessage(content='The task is complete.')])
    path=str(tmp_path/'input-graph.sqlite')
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        manager=InputSimulation(database,StubBrowserHost(),'sk-test',None)
        engine=Engine(database,manager.browsers,saver,'sk-test',model=model,browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        run=(await service.submit(user,sid,'Book my requested reservation'))['run_id'];await wait_service(service,run)
        pending=service.active(user)
        assert pending['status']=='input'
        assert 'phone number' in database.one('SELECT content FROM messages ORDER BY id DESC')['content']
        await engine.client.close()
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        engine=Engine(database,manager.browsers,saver,'sk-test',model=model,browser_tasks=manager)
        reloaded=Service(database,engine);reloaded.memory=Memory(database,SummaryModel());reloaded.recover()
        assert reloaded.active(user)['status']=='input'
        with pytest.raises(LookupError):await reloaded.respond_input(users[1]['id'],run,pending['approval_id'],'phone')
        with pytest.raises(LookupError):await reloaded.respond_input(user,run,'stale-id','phone')
        await reloaded.respond_input(user,run,pending['approval_id'],'+1 202 555 0100');await wait_service(reloaded,run)
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        assert len(database.rows('SELECT * FROM browser_use_tasks'))==1
        assert not database.facts(user,inspect=True)
        assert 'Controlled confirmation' in database.one('SELECT content FROM messages ORDER BY id DESC')['content']
        await engine.client.close()


async def test_input_after_worker_restart_uses_same_goal_and_saved_answers(database,users,monkeypatch):
    monkeypatch.setattr(module,'public_url',AsyncMock())
    manager=BrowserTasks(database,StubBrowserHost(),'sk-test',None);user=users[0]['id'];task_row(database,user)
    manager.workers['browser-task']=Worker(SimpleNamespace(returncode=None),'nonce',user,'browser-run',asyncio.Lock())
    manager.send=AsyncMock();state=observed();params={'kind':'information','prompt':'What time works for you?'}
    await manager.before('browser-task',{'seq':1,'operation':'request_user','params':params,'observed':state,'fingerprint':action_binding('request_user',params,state)})
    action=database.one('SELECT * FROM browser_use_actions')
    manager.workers.clear();manager.start=AsyncMock()
    await manager.answer_input(user,'browser-run','browser-task',action['id'],{'type':'browser_input','text':'7 PM'})
    call=manager.start.call_args
    assert call.args[:4]==(user,'browser-run','call-browser-task','Add blue shoes size 10 quantity 1 to cart')
    assert call.kwargs['restart']['response']=='7 PM'
    assert call.kwargs['restart']['previous_answers']
    assert database.one('SELECT status FROM browser_use_actions')['status']=='answered'
    assert manager.send.await_count==0


def test_navigation_uses_path_semantics_not_brand_or_hostname():
    state={'url':'https://booking.example/','node':{'tag':'A','role':'link','name':'Browse restaurants','href':'https://booking.example/restaurants'}}
    assert classify_action('navigate',{'url':'https://booking.example/restaurants'},state)[0]=='read'
    assert classify_action('click',{},state)[0]=='read'
    assert classify_action('navigate',{'url':'https://booking.example/checkout'},state)[0]=='read'


def test_input_endpoints_are_owned_and_cross_origin_rejected(client,database,users,credentials):
    from conftest import authenticate
    sid=database.new_session(users[0]['id']);database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('private-input',?,?,'input',0)",(users[0]['id'],sid))
    authenticate(client,credentials,'user02')
    assert client.post('/api/runs/private-input/input',json={'input_id':'x','text':'value'}).status_code==404
    assert client.post('/api/runs/private-input/takeover',json={'input_id':'x','operation':'type','text':'private-value'}).status_code==404
    assert client.post('/api/runs/private-input/takeover',json={'input_id':'x','operation':'type','text':'private-value'},headers={'origin':'https://untrusted.example'}).status_code==403
    invalid=client.post('/api/runs/private-input/takeover',json={'input_id':'x','operation':'type','text':'private-value'*200})
    assert invalid.status_code==422 and 'private-value' not in invalid.text


async def test_observation_reconnects_without_replaying_actions(monkeypatch):
    from personal_agent import browser_use_worker as worker
    browser=SimpleNamespace(reconnect=AsyncMock())
    inspect=AsyncMock(side_effect=[ConnectionError('Disconnected'),{'text':'Current page after reconnect'}])
    monkeypatch.setattr(worker,'_observe',inspect)
    assert (await worker.observe(browser))['text']=='Current page after reconnect'
    assert browser.reconnect.await_count==1 and inspect.await_count==2
    inspect=AsyncMock(side_effect=ValueError('Stale selector'));monkeypatch.setattr(worker,'_observe',inspect)
    with pytest.raises(ValueError):await worker.observe(browser,{'selector':'#missing'})
    assert browser.reconnect.await_count==1  # Stale controls require new targeting, not a reconnect.


async def test_cancel_waiting_input_revokes_manual_control(database,users):
    user=users[0]['id'];sid=database.new_session(user)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('waiting-cancel',?,?,'input',0)",(user,sid))
    engine=SimpleNamespace(model=None,browser_tasks=SimpleNamespace(cancel_run=AsyncMock()))
    service=Service(database,engine)
    await service.cancel(user,'waiting-cancel')
    assert service.active(user) is None
    assert 'cancelled' in database.one('SELECT content FROM messages ORDER BY id DESC')['content']
    engine.browser_tasks.cancel_run.assert_awaited_once_with(user,'waiting-cancel')


async def test_information_pause_allows_owned_panel_and_checks_required_fields(database,users):
    from personal_agent.browser_host import BrowserHost
    user=users[0]['id'];sid=database.new_session(user)
    pending=json.dumps({'action':{'kind':'information','prompt':'Enter your phone'}})
    database.execute("INSERT INTO runs(id,user_id,session_id,status,approval_id,approval,created) VALUES('panel-input',?,?,'input','panel-id',?,0)",(user,sid,pending))
    host=BrowserHost(database)
    assert host.pending_manual(user,'panel-input','panel-id')['kind']=='information'
    for who,aid in [(users[1]['id'],'panel-id'),(user,'old-id')]:
        with pytest.raises(LookupError):host.pending_manual(who,'panel-input',aid)
    host.view_page=AsyncMock(return_value=object())
    host.snapshot=AsyncMock(return_value={'fields':[{'type':'tel','empty':True}]})
    assert not await host.manual_ready(user,'panel-input')
    host.snapshot.return_value={'fields':[{'type':'tel','empty':False}]}
    assert await host.manual_ready(user,'panel-input')
    host.snapshot.return_value={'fields':[{'type':'text','required':True,'empty':True}]}
    assert not await host.manual_ready(user,'panel-input')
    host.snapshot.return_value={'fields':[],'manual_required':True}
    assert not await host.manual_ready(user,'panel-input')


async def test_queued_panel_input_is_rechecked_after_cancellation(database,users):
    from personal_agent.browser_host import BrowserHost
    user=users[0]['id'];sid=database.new_session(user)
    pending=json.dumps({'action':{'kind':'information'}})
    database.execute("INSERT INTO runs(id,user_id,session_id,status,approval_id,approval,created) VALUES('panel-queued',?,?,'input','panel-id',?,0)",(user,sid,pending))
    host=BrowserHost(database);lock=asyncio.Lock();host.preview_locks[user]=lock
    host.view_page=AsyncMock()
    await lock.acquire()
    task=asyncio.create_task(host.manual_action(user,'panel-queued','panel-id','type',text='Not sent'))
    await asyncio.sleep(0)
    database.execute("UPDATE runs SET status='cancelled' WHERE id='panel-queued'")
    lock.release()
    with pytest.raises(LookupError):await task
    host.view_page.assert_not_awaited()


async def test_fast_approval_waits_for_interrupt_checkpoint_finalization(database,users):
    from langchain_core.messages import AIMessageChunk
    user=users[0]['id'];sid=database.new_session(user)
    database.execute('CREATE TABLE run_usage(run_id TEXT PRIMARY KEY, searches INTEGER NOT NULL DEFAULT 0)')
    class Graph:
        closed=False
        resumed=False
        async def astream(self,payload,*args,**kw):
            try:
                if not self.closed:
                    yield 'updates',{'__interrupt__':[SimpleNamespace(value={'type':'browser_approval','action':{'operation':'click','url':'https://example.com','task':'Controlled action'}})]}
                else:
                    self.resumed=True
                    yield 'messages',(AIMessageChunk(content='Completed the controlled action.'),{'langgraph_node':'model'})
            finally:
                # The UI can observe approval while the saver is still finishing.
                await asyncio.sleep(.05)
                self.closed=True
    graph=Graph();engine=SimpleNamespace(model=SummaryModel(),graph=lambda *a:graph)
    service=Service(database,engine)
    run=(await service.submit(user,sid,'Perform the controlled action'))['run_id']
    while not service.active(user) or service.active(user)['status']!='approval':await asyncio.sleep(.001)
    pending=service.active(user)
    assert not graph.closed  # Deliberately respond before the stream finalizes.
    await service.resume(user,run,'approve',pending['approval_id'])
    await wait_service(service,run)
    assert graph.resumed and database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
    assert len(database.rows("SELECT id FROM events WHERE kind='approval'"))==1
