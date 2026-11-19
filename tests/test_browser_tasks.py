import asyncio
import json
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent import browser_tasks as module
from personal_agent.browser_tasks import BrowserTasks, Worker, GoalCheck
from personal_agent.browser_policy import action_binding, classify_action,legacy_duplicate_key
from personal_agent.agent import Engine
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel, wait_service


class StubBrowserHost:
    def __init__(self):
        self.locks={};self.contexts={};self.dialogs={}
    close_user=AsyncMock()
    def set_scope(self,*args):pass


def task_row(db,user,run='browser-run',tid='browser-task'):
    sid=db.new_session(user)
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(run,user,sid))
    db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES(?,?,?,?,?,? ,0,'running',0)",
               (tid,user,run,'call-'+tid,'Add blue shoes size 10 quantity 1 to cart','["example.com"]'))
    return sid


def observed(control='Place order'):
    return {'url':'https://example.com/product','text':'Blue shoes size 10 quantity 1 $80',
            'node':{'tag':'BUTTON','name':control,'role':'button','type':'button','visible':True,'disabled':False},
            'prices':['$80'],'selections':[{'name':'size','value':'10'}]}


async def prepare(manager,tid='browser-task',seq=1,control='Place order'):
    state=observed(control);params={'index':1}
    await manager.before(tid,{'seq':seq,'operation':'click','params':params,'observed':state,'fingerprint':action_binding('click',params,state)})
    return manager.db.one('SELECT * FROM browser_use_actions WHERE task_id=? AND seq=?',(tid,seq))


@pytest.fixture
def boundary(database,users,monkeypatch):
    monkeypatch.setattr(module,'public_url',AsyncMock())
    manager=BrowserTasks(database,StubBrowserHost(),'sk-test',None)
    user=users[0]['id'];task_row(database,user)
    lock=asyncio.Lock();worker=Worker(SimpleNamespace(returncode=None),'nonce',user,'browser-run',lock)
    manager.workers['browser-task']=worker
    manager.send=AsyncMock()
    return manager,user


def test_policy_blocks_host_tools_private_fields_and_readonly_mutations():
    assert classify_action('evaluate',{'code':'anything'},observed())[0]=='blocked'
    assert classify_action('write_file',{},observed())[0]=='blocked'
    private=observed();private['node'].update(tag='INPUT',type='password',sensitive=True)
    assert classify_action('input',{'text':'password'},private)[0]=='blocked'
    assert classify_action('click',{},observed(),inspect_only=True)[0]=='blocked'
    assert classify_action('click',{},observed())[0]=='approval'
    assert classify_action('navigate',{'url':'https://example.com/cart/add'},observed())[0]=='approval'
    assert classify_action('find_elements',{'attributes':['value']},observed())[0]=='blocked'
    assert classify_action('click_selector',{'selector':'button'},observed())[0]=='approval'
    radio=observed('10.0');radio['node'].update(role='radio',tag='LABEL')
    assert classify_action('click_selector',{'selector':'label[for=s10]'},radio)[0]=='preparation'
    assert classify_action('click_selector',{'selector':'label[for=s10]'},radio,inspect_only=True)[0]=='blocked'


def test_search_semantics_do_not_approve_checkout_or_account_forms():
    search=observed('Search products');search['node'].update(tag='INPUT',type='search',role='searchbox',form=None)
    assert classify_action('send_keys',{'keys':'ENTER'},search)[0]=='preparation'
    search['node']['form']={'method':'post','action':'https://example.com/message','sensitive':False}
    assert classify_action('send_keys',{'keys':'ENTER'},search)[0]=='approval'
    search['node']['form']=None;search['url']='https://example.com/checkout'
    assert classify_action('send_keys',{'keys':'ENTER'},search)[0]=='approval'
    opener=observed('Search');opener['node'].update(type='button',form=None)
    assert classify_action('click',{},opener)[0]=='preparation'
    opener['node'].update(name='Cancel',search_context=True)
    assert classify_action('click',{},opener)[0]=='preparation'
    opener['node']['name']='Cancel order'
    assert classify_action('click',{},opener)[0]=='approval'


async def test_search_retries_ignore_old_coarse_enter_receipt_but_cart_does_not(boundary):
    manager,user=boundary
    original=await prepare(manager)
    legacy=legacy_duplicate_key('send_keys',{'keys':'ENTER'},{'url':'https://example.com/product'})
    manager.db.execute("UPDATE browser_use_actions SET status='executed',duplicate_key=? WHERE id=?",(legacy,original['id']))
    search=observed('Search products');search['node'].update(tag='INPUT',type='search',form=None,value='blue shoes')
    params={'keys':'ENTER'}
    await manager.before('browser-task',{'seq':2,'operation':'send_keys','params':params,'observed':search,'fingerprint':action_binding('send_keys',params,search)})
    assert manager.send.call_args.args[1]['decision']=='allow'
    checkout=observed('Place order')
    await manager.before('browser-task',{'seq':3,'operation':'send_keys','params':params,'observed':checkout,'fingerprint':action_binding('send_keys',params,checkout)})
    assert manager.send.call_args.args[1]['decision']=='deny'  # Preserve unknown legacy keyboard transactions.
    manager.db.execute("UPDATE browser_use_actions SET duplicate_key=? WHERE id=?",(legacy_duplicate_key('click',{'index':1},observed()),original['id']))
    cart=await prepare(manager,seq=4)
    assert cart['status']=='blocked' and manager.send.call_args.args[1]['decision']=='deny'
    result=await manager.finish('browser-task',{'observed':observed(),'reported_success':False})
    assert result['status']=='blocked' and 'matching submission' in result['verification']['reason']


async def test_approval_is_owned_and_consumed_once(boundary,users):
    manager,user=boundary;row=await prepare(manager)
    assert manager.send.await_count==0
    with pytest.raises(LookupError):
        await manager.decide(users[1]['id'],'browser-run','browser-task',row['id'],'approve')
    await manager.decide(user,'browser-run','browser-task',row['id'],'approve')
    await manager.decide(user,'browser-run','browser-task',row['id'],'approve')
    assert manager.send.await_count==1
    assert manager.send.call_args.args[1]['category']=='approval'
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE id=?',(row['id'],))['status']=='approved'


async def test_rejection_and_duplicate_unknown_never_dispatch(boundary):
    manager,user=boundary;row=await prepare(manager)
    await manager.decide(user,'browser-run','browser-task',row['id'],'reject')
    assert manager.send.call_args.args[1]['decision']=='deny'
    manager.db.execute("UPDATE browser_use_actions SET status='unknown' WHERE id=?",(row['id'],))
    duplicate=await prepare(manager,seq=2)
    assert duplicate['status']=='blocked'
    assert manager.send.call_args.args[1]['decision']=='deny'


async def test_type_and_scope_checks_happen_before_dispatch(boundary):
    manager,user=boundary
    state=observed('A menu link');state['node'].update(tag='A',role='link',href='https://example.com/menu')
    params={'index':1,'text':'10'}
    await manager.before('browser-task',{'seq':1,'operation':'input','params':params,'observed':state,'fingerprint':action_binding('input',params,state)})
    assert manager.send.call_args.args[1]['decision']=='deny' and manager.send.call_args.args[1]['retry']
    state=observed();params={'url':'https://outside.example/page'}
    await manager.before('browser-task',{'seq':2,'operation':'navigate','params':params,'observed':state,'fingerprint':action_binding('navigate',params,state)})
    assert manager.send.call_args.args[1]['decision']=='deny'
    state['url']='https://previous.example/old-task';params={'url':'https://example.com/product'}
    await manager.before('browser-task',{'seq':3,'operation':'navigate','params':params,'observed':state,'fingerprint':action_binding('navigate',params,state)})
    assert manager.send.call_args.args[1]['decision']=='allow'


async def test_restart_invalidates_pending_approval_and_preserves_unknown(boundary):
    manager,user=boundary;first=await prepare(manager)
    second=await prepare(manager,seq=2,control='Place order')
    manager.db.execute("UPDATE browser_use_actions SET status='dispatching' WHERE id=?",(first['id'],))
    manager.recover()
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE id=?',(first['id'],))['status']=='unknown'
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE id=?',(second['id'],))['status']=='invalidated'
    assert manager.owned(user,'browser-run','browser-task')['status']=='interrupted'
    assert manager.send.await_count==0


async def test_cancellation_invalidates_unsubmitted_actions(boundary):
    manager,user=boundary;row=await prepare(manager)
    manager.terminate=AsyncMock()
    await manager.cancel_run(user,'browser-run')
    assert manager.owned(user,'browser-run','browser-task')['status']=='cancelled'
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE id=?',(row['id'],))['status']=='invalidated'
    manager.terminate.assert_awaited_once_with('browser-task')
    assert manager.send.await_count==0


async def test_verifier_cannot_accept_ungrounded_evidence(boundary):
    manager,user=boundary;row=await prepare(manager)
    manager.db.execute("UPDATE browser_use_actions SET status='executed' WHERE id=?",(row['id'],))
    class Model:
        def with_structured_output(self,*args):return self
        async def ainvoke(self,*args):return GoalCheck(success=True,reason='Added',evidence_ids=[999])
    manager.model=Model()
    result=await manager.finish('browser-task',{'observed':{'url':'https://example.com/product','text':'Cart empty'},'result':'I added it','reported_success':True})
    assert result['status']=='unverified' and not result['verification']['success']


async def test_provider_approval_expands_actions_but_not_citations(boundary):
    manager,user=boundary
    manager.browsers.blocked_destinations={};manager.browsers.scopes={}
    manager.db.execute('CREATE TABLE browser_delegations(user_id TEXT,run_id TEXT,domain TEXT,UNIQUE(user_id,run_id,domain))')
    state=observed();state['links']=['https://provider.example/login']
    params={'url':'https://provider.example/login'}
    event={'seq':1,'operation':'request_domain','params':params,'observed':state,'fingerprint':action_binding('request_domain',params,state)}
    await manager.before('browser-task',event)
    row=manager.db.one('SELECT * FROM browser_use_actions WHERE seq=1')
    assert row['status']=='proposed'
    await manager.decide(user,'browser-run','browser-task',row['id'],'approve')
    task=manager.owned(user,'browser-run','browser-task')
    assert json.loads(task['action_domains'])==['example.com','provider.example']
    assert json.loads(task['domains'])==['example.com']
    count=len(manager.db.rows("SELECT id FROM events WHERE kind='sources'"))
    manager.sources('browser-run',{'url':'https://provider.example/receipt'},['example.com'])
    assert len(manager.db.rows("SELECT id FROM events WHERE kind='sources'"))==count


async def test_http_error_content_cannot_become_browser_evidence(boundary):
    manager,user=boundary
    manager.browsers.document_status={(user,'https://example.com/product'):403}
    class Model:
        def with_structured_output(self,*args):raise AssertionError('Error pages cannot be verified')
    manager.model=Model()
    result=await manager.finish('browser-task',{'observed':observed(),'result':'success','reported_success':True})
    assert result['status']=='unverified'
    assert not manager.db.rows("SELECT id FROM events WHERE kind='sources'")


async def test_langgraph_cart_preparation_runs_without_approval_but_checkout_does_not(database,users,tmp_path,monkeypatch):
    monkeypatch.setattr(module,'public_url',AsyncMock())
    user=users[0]['id'];sid=database.new_session(user)
    class Simulated(BrowserTasks):
        sent=[]
        queue=asyncio.Queue()
        async def start(self,user,run,call_id,objective,domains,inspect_only=False):
            existing=self.db.one('SELECT id FROM browser_use_tasks WHERE call_id=?',(call_id,))
            if existing:return existing['id']
            tid='simulated-task'
            self.db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES(?,?,?,?,?,? ,0,'running',0)",
                (tid,user,run,call_id,objective,json.dumps(domains)))
            self.workers[tid]=Worker(SimpleNamespace(returncode=None),'nonce',user,run,asyncio.Lock())
            self.simulation=asyncio.create_task(self.simulate(tid))
            return tid
        async def send(self,tid,payload):await self.queue.put(payload)
        async def simulate(self,tid):
            for seq,control in [(1,'Add to Cart'),(2,'Place order')]:
                row=await prepare(self,tid,seq,control)
                decision=await self.queue.get()
                if decision['decision']=='deny':break
                self.sent.append(control)
                self.db.execute("UPDATE browser_use_actions SET status='executed' WHERE id=?",(row['id'],))
            self.db.execute("UPDATE browser_use_tasks SET status='completed',result=? WHERE id=?",(json.dumps({'status':'rejected','verification':{'reason':'Checkout rejected'}}),tid))
            self.workers[tid].changed.set()
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Add shoes to cart then prepare checkout','official_domains':['example.com']},'id':'delegate','type':'tool_call'}]),AIMessage(content='Checkout was rejected.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'delegation.sqlite')) as saver:
        manager=Simulated(database,StubBrowserHost(),'sk-test',None)
        engine=Engine(database,manager.browsers,saver,'sk-test',model=model,browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            run=(await service.submit(user,sid,'Add shoes to cart and prepare checkout'))['run_id'];await wait_service(service,run)
            first=service.active(user);assert first['status']=='approval' and manager.sent==['Add to Cart']
            assert 'Place order' in first['approval']
            await service.submit(user,sid,'reject '+first['approval_id']);await wait_service(service,run)
            assert manager.sent==['Add to Cart']
            assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        finally:
            if hasattr(manager,'simulation'):
                manager.simulation.cancel();await asyncio.gather(manager.simulation,return_exceptions=True)
            await engine.client.close()


@pytest.mark.parametrize('domain',['shop.example','cards.example','travel.example'])
def test_routine_controls_and_viewing_forms_do_not_request_approval(domain):
    state=observed('First name');state['url']='https://'+domain+'/products'
    state['node'].update(tag='INPUT',type='text',role='textbox',form={'method':'post','sensitive':False})
    assert classify_action('click',{},state)[0]=='preparation'
    assert classify_action('input',{'text':'Test'},state)[0]=='preparation'
    assert classify_action('send_keys',{'keys':'Backspace'},state)[0]=='preparation'
    state['node'].update(tag='BUTTON',type='button',role='button',name='Size guide',form=None)
    assert classify_action('click',{},state)[0]=='preparation'
    state['node'].update(name='Choose blue')
    assert classify_action('click',{},state)[0]=='preparation'
    state['node'].update(type='submit',name='More product details')
    assert classify_action('click',{},state)[0]=='preparation'  # No form: button default type is inert.
    state['node'].update(tag='A',role='link',name='Apply now',href='https://'+domain+'/apply/card')
    assert classify_action('click',{},state)[0]=='read'
    assert classify_action('navigate',{'url':'https://'+domain+'/checkout'},state)[0]=='read'
    assert classify_action('navigate',{'url':'https://'+domain+'/apply/card'},state)[0]=='read'


@pytest.mark.parametrize('label',['Place order','Pay now','Confirm booking','Send code','Delete account','Submit application','Save changes','Subscribe','Accept terms','Enroll now','Transfer funds','Authorize payment'])
def test_consequential_buttons_still_require_exact_approval(label):
    assert classify_action('click',{},observed(label))[0]=='approval'
    state=observed(label);state['node'].update(tag='A',role='link',href='https://example.com/action')
    assert classify_action('click',{},state)[0]=='approval'


async def test_cart_preparation_is_automatic_and_unknown_mutations_are_not_repeated(boundary):
    manager,user=boundary
    first=await prepare(manager,control='Add to Cart')
    assert first['category']=='mutation' and first['status']=='prepared'
    assert manager.send.call_args.args[1]['decision']=='allow'
    manager.db.execute("UPDATE browser_use_actions SET status='unknown' WHERE id=?",(first['id'],))
    second=await prepare(manager,seq=2,control='Add to Cart')
    assert second['status']=='blocked' and manager.send.call_args.args[1]['decision']=='deny'
    manager.model=None
    result=await manager.finish('browser-task',{'observed':observed(),'reported_success':False})
    assert result['status']=='unknown'


async def test_cancellation_invalidates_approval_but_preserves_dispatched_cart_outcome(boundary):
    manager,user=boundary
    cart=await prepare(manager,control='Add to Cart');pending=await prepare(manager,seq=2,control='Place order')
    manager.db.execute("UPDATE browser_use_actions SET status='dispatching' WHERE id=?",(cart['id'],))
    manager.terminate=AsyncMock()
    await manager.cancel_run(user,'browser-run')
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE id=?',(cart['id'],))['status']=='unknown'
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE id=?',(pending['id'],))['status']=='invalidated'
    manager.terminate.assert_awaited_once_with('browser-task')
    dispatched=manager.send.await_count
    await manager.decide(user,'browser-run','browser-task',pending['id'],'approve')
    assert manager.send.await_count==dispatched


def test_ambiguous_controls_and_sensitive_forms_do_not_get_routine_exemptions():
    assert classify_action('click',{},observed('Execute'))[0]=='approval'
    form=observed('Next');form['node']['form']={'method':'post','sensitive':True}
    assert classify_action('click',{},form)[0]=='approval'
    form['node']['name']='Search';assert classify_action('click',{},form)[0]=='approval'
