import asyncio
import json
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent import browser_tasks as module
from personal_agent.browser_tasks import BrowserTasks, Worker, GoalCheck
from personal_agent.browser_policy import action_binding, classify_action,legacy_duplicate_key,sanitize_find_elements
from personal_agent.agent import Engine
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel, wait_service
from personal_agent.browser_intent import native_entry_url,browser_preview_run


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


def test_native_entry_uses_scoped_observed_login_for_account_queries_without_session_parameters(boundary,users):
    manager,user=boundary;db=manager.db
    run=db.one("SELECT session_id FROM runs WHERE id='browser-run'")
    db.message(user,run['session_id'],'user','Show my account balance')
    db.execute("UPDATE runs SET created=? WHERE id='browser-run'",(time.time()+1,))
    state={'url':'https://example.com/en/account/login?tracking=private-session','fields':[{'type':'password'}]}
    db.execute("INSERT INTO browser_use_actions(id,task_id,seq,operation,category,status,description,fingerprint,duplicate_key,before_state) VALUES('entry','browser-task',1,'request_user','input','answered','{}','fixture','fixture',?)",(json.dumps(state),))
    assert native_entry_url(db,user,'browser-run','Start at https://example.com',['example.com'])=='https://example.com/en/account/login'
    assert native_entry_url(db,user,'browser-run','',['unrelated.example']) is None
    assert native_entry_url(db,users[1]['id'],'browser-run','',['example.com']) is None
    db.message(user,run['session_id'],'user','Open https://example.com/help for my account')
    assert native_entry_url(db,user,'browser-run','',['example.com'])=='https://example.com/help'


def test_native_entry_does_not_put_query_tokens_or_mutating_urls_in_chrome_arguments(boundary):
    manager,user=boundary
    for url in ['https://example.com/login?token=private','https://example.com/logout','https://user:private@example.com/login']:
        assert native_entry_url(manager.db,user,'browser-run',url,['example.com']) is None


@pytest.fixture
def boundary(database,users,monkeypatch):
    monkeypatch.setattr(module,'public_url',AsyncMock())
    manager=BrowserTasks(database,StubBrowserHost(),'sk-test',None)
    user=users[0]['id'];task_row(database,user)
    lock=asyncio.Lock();worker=Worker(SimpleNamespace(returncode=None),'nonce',user,'browser-run',lock)
    manager.workers['browser-task']=worker
    manager.send=AsyncMock()
    return manager,user


@pytest.mark.parametrize('destination,expected',[
    ('https://example.com/benefits','allow'),
    ('https://unrelated.example/benefits','deny'),
    ('http://127.0.0.1/private','deny'),
    ('','deny'),
])
async def test_switch_validates_destination_instead_of_previous_task_site(boundary,destination,expected):
    manager,user=boundary
    state={'url':'https://previous-task.example/login','target_id':'old-tab',
           'switch_target':{'target_id':'new-tab','url':destination}}
    params={'tab_id':'new-tab'}
    await manager.before('browser-task',{'seq':1,'operation':'switch','params':params,'observed':state,
        'fingerprint':action_binding('switch',params,state)})
    assert manager.send.call_args.args[1]['decision']==expected
    changed={**state,'switch_target':{'target_id':'new-tab','url':'https://example.com/changed'}}
    assert action_binding('switch',params,state)!=action_binding('switch',params,changed)


def test_preview_survives_related_chain_but_not_topic_change_or_other_scope(boundary,users):
    manager,user=boundary;db=manager.db
    sid=db.one("SELECT session_id FROM runs WHERE id='browser-run'")['session_id']
    db.message(user,sid,'user','Open the card benefits page')
    db.execute('UPDATE messages SET created=1 WHERE session_id=?',(sid,))
    db.execute("UPDATE runs SET created=2,status='completed' WHERE id='browser-run'")
    db.execute("UPDATE browser_use_tasks SET status='completed',objective='Open the card benefits page' WHERE id='browser-task'")
    def follow(rid,text,stamp):
        mid=db.message(user,sid,'user',text);db.execute('UPDATE messages SET created=? WHERE id=?',(stamp,mid))
        db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'completed',?)",(rid,user,sid,stamp+.1))
    follow('follow1','show me all card benefits',3)
    assert browser_preview_run(db,user,sid)=='browser-run'
    follow('follow2','tell me more',4)
    assert browser_preview_run(db,user,sid)=='browser-run'
    assert browser_preview_run(db,users[1]['id'],sid) is None
    assert browser_preview_run(db,user,db.new_session(user)) is None
    follow('different','hello',5)
    assert browser_preview_run(db,user,sid) is None
    follow('later','show me all card benefits',6)
    assert browser_preview_run(db,user,sid) is None  # A topic break is not silently undone.


def test_cancelled_browser_task_does_not_reappear_on_followup(boundary):
    manager,user=boundary;db=manager.db
    sid=db.one("SELECT session_id FROM runs WHERE id='browser-run'")['session_id']
    db.execute("UPDATE runs SET status='cancelled' WHERE id='browser-run'")
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('follow',?,?,'completed',1)",(user,sid))
    assert browser_preview_run(db,user,sid) is None


def test_policy_blocks_host_tools_private_fields_and_readonly_mutations():
    assert classify_action('evaluate',{'code':'anything'},observed())[0]=='blocked'
    assert classify_action('write_file',{},observed())[0]=='blocked'
    private=observed();private['node'].update(tag='INPUT',type='password',sensitive=True)
    assert classify_action('input',{'text':'password'},private)[0]=='blocked'
    assert classify_action('click',{},observed(),inspect_only=True)[0]=='blocked'
    assert classify_action('click',{},observed())[0]=='approval'
    assert classify_action('navigate',{'url':'https://example.com/cart/add'},observed())[0]=='approval'


async def test_chromium_error_page_does_not_report_a_domain_scope_denial(boundary):
    manager,user=boundary;state=observed();state['url']='chrome-error://chromewebdata/'
    await manager.before('browser-task',{'seq':1,'operation':'wait','params':{},'observed':state,'fingerprint':action_binding('wait',{},state)})
    row=manager.db.one('SELECT description FROM browser_use_actions WHERE task_id=? AND seq=1',('browser-task',))
    description=json.loads(row['description'])
    assert 'navigation failed' in description['blocked_reason']
    assert 'outside the approved' not in description['blocked_reason']
    assert classify_action('find_elements',{'attributes':['value']},observed())[0]=='blocked'
    assert classify_action('click_selector',{'selector':'button'},observed())[0]=='approval'
    radio=observed('10.0');radio['node'].update(role='radio',tag='LABEL')
    assert classify_action('click_selector',{'selector':'label[for=s10]'},radio)[0]=='preparation'
    assert classify_action('click_selector',{'selector':'label[for=s10]'},radio,inspect_only=True)[0]=='preparation'


def test_inspection_keeps_public_metadata_and_omits_private_attributes():
    params={'selector':'a,input','attributes':['href','target','rel','action','data-testid','data-state',
        'aria-describedby','value','data-access-token','onclick','data-password','*']}
    safe=sanitize_find_elements(params)
    assert safe['attributes']==['href','target','rel','action','data-testid','data-state','aria-describedby']
    assert 'value' in params['attributes']  # Caller input is not mutated.
    assert classify_action('find_elements',safe,observed())[0]=='read'
    assert classify_action('find_elements',{'attributes':None},observed())[0]=='read'
    assert sanitize_find_elements({'attributes':['value','data-token']})['attributes']==['href','id','role','aria-label']


async def test_unsupported_read_attributes_are_recoverable_not_terminal(boundary):
    manager,user=boundary;state=observed();params={'selector':'a','attributes':['href','onclick']}
    await manager.before('browser-task',{'seq':1,'operation':'find_elements','params':params,'observed':state,
        'fingerprint':action_binding('find_elements',params,state)})
    reply=manager.send.call_args.args[1]
    assert reply['decision']=='deny' and reply['retry'] is True
    assert manager.owned(user,'browser-run','browser-task')['status']=='running'


@pytest.mark.parametrize('label',['Reserve a table','Book a table','Reservations','Book now'])
def test_reservation_entry_links_and_dialogs_are_routine_navigation(label):
    state=observed(label);state['node'].update(tag='A',type='',role='link',form=None,
        href='https://reservations.example/cities/city/venues/restaurant')
    assert classify_action('click',{},state)[0]=='read'
    state['node'].update(tag='BUTTON',type='button',role='button',href='',has_popup='dialog')
    assert classify_action('click',{},state)[0]=='read'
    state['node']['form']={'method':'post','sensitive':False}
    assert classify_action('click',{},state)[0]==('preparation' if label=='Reservations' else 'approval')


def test_reservation_entry_exemption_does_not_cover_submit_or_checkout():
    state=observed('Reserve a table');state['node'].update(tag='A',role='link',form=None,
        href='https://example.com/booking/confirm')
    assert classify_action('click',{},state)[0]=='approval'
    state['node']['href']='https://example.com/reservations'
    state['url']='https://example.com/checkout'
    assert classify_action('click',{},state)[0]=='approval'


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


async def test_read_and_summarize_verifies_browser_evidence_before_chat_delivery(boundary):
    manager,user=boundary
    manager.db.execute("UPDATE browser_use_tasks SET objective='Open the benefits page and summarize it' WHERE id='browser-task'")
    report='Eligible cards earn points and include travel protection.'
    class Model:
        def with_structured_output(self,*args):return self
        async def ainvoke(self,messages):
            assert 'BEFORE the main agent sends its final chat response' in messages[0].content
            data=json.loads(messages[1].content)
            assert 'candidate_report' not in data  # Draft prose is not source evidence.
            assert not data['actions']  # No chat-delivery or website submission is required.
            assert data['numbered_evidence']['0']=='Card benefits: Eligible cards earn points and include travel protection.'
            return GoalCheck(success=True,reason='The requested benefits are visible and support the summary.',evidence_ids=[0])
    manager.model=Model()
    result=await manager.finish('browser-task',{'observed':{'url':'https://example.com/benefits','text':'Card benefits: '+report},'result':report,'reported_success':True})
    assert result['status']=='verified' and result['verification']['success']


async def test_read_verifier_uses_user_goal_not_expanded_navigation_hints(boundary):
    manager,user=boundary;db=manager.db
    sid=db.one("SELECT session_id FROM runs WHERE id='browser-run'")['session_id']
    mid=db.message(user,sid,'user','Compare Alpha and Beta warranties')
    db.execute('UPDATE messages SET created=1 WHERE id=?',(mid,))
    db.execute("UPDATE runs SET created=2 WHERE id='browser-run'")
    db.execute("UPDATE browser_use_tasks SET inspect_only=1,objective='Read warranty pages. Optional links include application, footer and comparison pages.' WHERE id='browser-task'")
    class Model:
        def with_structured_output(self,*args):return self
        async def ainvoke(self,messages):
            data=json.loads(messages[1].content)
            assert data['task']=='Compare Alpha and Beta warranties'
            assert 'optional discovery hints' in messages[0].content
            assert all('task' not in a['details'] for a in data['actions'])
            return GoalCheck(success=True,reason='Both warranties were observed; optional links are not required.',evidence_ids=[0])
    manager.model=Model()
    result=await manager.finish('browser-task',{'observed':{'url':'https://example.com/warranties','text':'Alpha: three years. Beta: five years.','links':['https://example.com/warranty/compare']}})
    assert result['status']=='verified' and not result['browser_followup']['links']


@pytest.mark.parametrize('url,decision',[('https://example.com/details','allow'),('https://other.example/details','deny')])
async def test_direct_page_reads_keep_the_existing_domain_boundary(boundary,url,decision):
    manager,user=boundary;state={'url':url,'text':'Observed details'};params={}
    await manager.before('browser-task',{'seq':1,'operation':'read_page','params':params,'observed':state,
        'fingerprint':action_binding('read_page',params,state)})
    assert manager.send.call_args.args[1]['decision']==decision


async def test_shopping_listing_does_not_count_as_product_detail_evidence(boundary):
    manager,user=boundary;db=manager.db
    db.execute("UPDATE browser_use_tasks SET inspect_only=1 WHERE id='browser-task'")
    db.event('browser-run','shopping_browser',{'requested_count':2})
    class Model:
        def with_structured_output(self,*args):return self
        async def ainvoke(self,messages):
            data=json.loads(messages[1].content)
            assert data['shopping_requirements']['requested_count']==2
            assert data['shopping_requirements']['observed_product_pages']==[]
            return GoalCheck(success=True,reason='Listings visible',evidence_ids=[0])
    manager.model=Model()
    result=await manager.finish('browser-task',{'observed':{'url':'https://example.com/c/white-shirts','text':'White shirts: Alpha and Beta.',
        'links':['https://example.com/p/12345','https://example.com/p/67890']}})
    assert result['status']=='unverified'
    assert result['browser_followup']['links']==['https://example.com/p/12345','https://example.com/p/67890']


async def test_provider_approval_expands_actions_but_not_citations(boundary,monkeypatch):
    monkeypatch.setenv('PERSONAL_AGENT_BROWSER_POLICY','strict')
    manager,user=boundary
    manager.browsers.blocked_destinations={};manager.browsers.scopes={}
    manager.db.execute('CREATE TABLE IF NOT EXISTS browser_delegations(user_id TEXT,run_id TEXT,domain TEXT,UNIQUE(user_id,run_id,domain))')
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
