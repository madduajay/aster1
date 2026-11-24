import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent.browser_policy import browser_policy,classify_action,action_binding,manual_action_required
from personal_agent.browser_recovery import input_need
from personal_agent.browser_verification import navigation_evidence
from test_browser_tasks import boundary,observed,prepare


@pytest.mark.parametrize('label,tag,kind,form,text,operation,extra',[
    ('Search for a destination or hotel name','BUTTON','submit',{'method':'post','sensitive':False},'Hotel search','click',{}),
    ('2 December 02','DIV','',None,'Choose dates','click',{}),
    ('10 December 10','DIV','',None,'Choose dates','click',{}),
    ('Continue without Accepting →','BUTTON','submit',None,'Cookie preferences','click',{}),
    ('Reject','BUTTON','submit',None,'We use cookies','click',{}),
    ('Checkout','BUTTON','submit',None,'Room selection','click',{}),
    ('close_popup','A','',{'method':'post'},'Room selection','click',{}),
    ('Room Icon','A','',{'method':'post'},'Room selection','click',{}),
    ('Search by Destination City, Landmark, Hotel','INPUT','text',{'method':'post'},'Hotel search','send_keys',{'search_context':True}),
])
def test_observed_navigation_false_positives_proceed_without_approval(label,tag,kind,form,text,operation,extra):
    state=observed(label);state['text']=text
    state['node'].update(tag=tag,type=kind,form=form,**extra)
    assert classify_action(operation,{'keys':'Enter'} if operation=='send_keys' else {},state,policy='poc')[0] in {'preparation','read'}


def test_opening_account_details_is_read_only_but_account_changes_are_not():
    state=observed('Checking account details');state['url']='https://example.com/account'
    state['node'].update(tag='A',type='',href='https://example.com/account/checking')
    assert classify_action('click',{},state,policy='poc')[0] in {'preparation','read'}
    state['node'].update(name='Checking ...0946',href='#')
    assert classify_action('click',{},state,policy='poc')[0]=='preparation'
    state['node'].update(name='Delete account',href='https://example.com/account/delete')
    assert classify_action('click',{},state,policy='poc')[0]=='approval'


def test_public_embeddings_do_not_expand_the_agent_action_scope(database,monkeypatch):
    from personal_agent.browser_host import BrowserHost
    host=BrowserHost(database)
    root=SimpleNamespace(url='https://example.com/login',parent_frame=None)
    embedded=SimpleNamespace(url='https://support.example/frame',parent_frame=root)
    request=SimpleNamespace(frame=embedded)
    monkeypatch.setenv('PERSONAL_AGENT_BROWSER_POLICY','poc')
    assert host.embedded_dependency(request,['example.com'])
    assert not host.scopes
    root.url='https://unrelated.example/'
    assert not host.embedded_dependency(request,['example.com'])
    root.url='https://example.com/login';request.frame=root
    assert not host.embedded_dependency(request,['example.com'])
    request.frame=embedded;monkeypatch.setenv('PERSONAL_AGENT_BROWSER_POLICY','strict')
    assert not host.embedded_dependency(request,['example.com'])


@pytest.mark.parametrize('url,node,operation,params',[
    ('https://example.com/account/profile',{'tag':'INPUT','type':'text','name':'Display name'},'input',{'text':'Alex'}),
    ('https://example.com/product',{'tag':'BUTTON','type':'button','name':'Choose size','form':{'method':'post','sensitive':False}},'click',{}),
    ('https://example.com/product',{'tag':'A','type':'','name':'View payment options','href':'https://example.com/help/payments','form':None},'click',{}),
    ('https://example.com/product',{'tag':'A','type':'','name':'Sign in','href':'https://example.com/login','form':None},'click',{}),
])
def test_poc_automates_proven_preparation_that_strict_mode_would_prompt(url,node,operation,params):
    state=observed();state['url']=url;state['node'].update(node)
    assert classify_action(operation,params,state,policy='poc')[0] in {'read','preparation'}
    assert classify_action(operation,params,state,policy='strict')[0]=='approval'


def test_password_elsewhere_does_not_block_ordinary_fields_but_secrets_remain_manual():
    state=observed('First name');state.update(manual_required=True,human_required=False,fields=[{'type':'password','sensitive':True,'blocking':False}])
    state['node'].update(tag='INPUT',type='text',sensitive=False,form={'method':'post','sensitive':True})
    assert classify_action('input',{'text':'Alex'},state,policy='poc')[0]=='preparation'
    assert classify_action('input',{'text':'Alex'},state,policy='strict')[0]=='blocked'
    assert input_need(state,completion=True) is None
    state['node'].update(type='password',sensitive=True)
    assert classify_action('input',{'text':'Never sent'},state,policy='poc')[0]=='blocked'
    state['node'].update(type='submit',sensitive=False,name='Sign in')
    assert manual_action_required('click',{},state,policy='poc')
    state['human_required']=True
    assert classify_action('click',{},state,policy='poc')[0]=='blocked'


def test_only_actual_required_phone_validation_prompts_at_completion():
    state={'fields':[{'type':'tel','required':True,'empty':True,'blocking':False}]}
    assert input_need(state,completion=True) is None
    state['fields'][0]['blocking']=True
    assert input_need(state,completion=True)['kind']=='information'
    assert input_need(state,navigation_only=True,completion=True) is None


def test_final_submissions_and_financial_actions_do_not_become_routine():
    for label in ('Place order','Send message','Delete account','Close account','Open account','Save profile','Confirm booking'):
        assert classify_action('click',{},observed(label),policy='poc')[0]=='approval'
    state=observed('Submit application');state['node'].update(type='submit',form={'method':'post','financial':True})
    assert classify_action('click',{},state,policy='poc')[0]=='blocked'
    assert browser_policy('poc').recovery_failures==12
    assert browser_policy('strict').recovery_failures==6


async def test_observed_provider_transition_proceeds_without_approval(boundary):
    manager,user=boundary;manager.browsers.scopes={};manager.browsers.blocked_destinations={}
    state=observed();state['links']=['https://provider.example/booking']
    params={'url':state['links'][0]}
    await manager.before('browser-task',{'seq':1,'operation':'request_domain','params':params,'observed':state,'fingerprint':action_binding('request_domain',params,state)})
    row=manager.db.one('SELECT * FROM browser_use_actions WHERE seq=1')
    assert row['category']=='read' and row['status']=='prepared'
    assert 'provider.example' in manager.browsers.scopes[(user,'browser-run')]
    assert manager.send.await_args.args[1]['decision']=='allow'
    params={'url':'https://unobserved.example/'}
    await manager.before('browser-task',{'seq':2,'operation':'request_domain','params':params,'observed':state,'fingerprint':action_binding('request_domain',params,state)})
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE seq=2')['status']=='blocked'


async def test_unchanged_approval_is_reused_but_changed_price_requires_a_new_decision(boundary):
    manager,user=boundary
    prior=await prepare(manager)
    await manager.decide(user,'browser-run','browser-task',prior['id'],'approve')
    manager.db.execute("UPDATE browser_use_actions SET status='invalidated' WHERE id=?",(prior['id'],))
    same=await prepare(manager,seq=2)
    assert same['status']=='approved'
    assert json.loads(same['description'])['approval_reused']==prior['id']
    state=observed();state['prices']=['$90'];params={'index':1}
    await manager.before('browser-task',{'seq':3,'operation':'click','params':params,'observed':state,'fingerprint':action_binding('click',params,state)})
    assert manager.db.one('SELECT status FROM browser_use_actions WHERE seq=3')['status']=='proposed'


async def test_explicit_page_open_has_deterministic_proof_without_a_verifier_call(boundary):
    manager,user=boundary;run=manager.db.one("SELECT * FROM runs WHERE id='browser-run'")
    manager.db.message(user,run['session_id'],'user','Open https://example.com/application')
    manager.db.execute("UPDATE runs SET created=? WHERE id='browser-run'",(time.time(),))
    state=observed();state.update(url='https://example.com/application',text='Application form: choose a product, then enter your contact details.')
    manager.model=AsyncMock(side_effect=AssertionError('No model verification needed for this exact navigation proof'))
    result=await manager.finish('browser-task',{'observed':state,'result':'Page open','reported_success':True})
    assert result['status']=='verified' and result['verification']['method']=='observed_page'
    assert navigation_evidence(manager.db,user,'browser-run',{**state,'url':'https://example.com/different'},[]) is None
    assert navigation_evidence(manager.db,user,'browser-run',{**state,'human_required':True},[]) is None


async def test_public_redirects_are_automatic_but_private_and_unrelated_transitions_are_not(database,users,monkeypatch):
    from personal_agent.browser_host import BrowserHost
    from personal_agent import browser_host as module
    uid=users[0]['id'];host=BrowserHost(database)
    public=AsyncMock();monkeypatch.setattr(module,'public_url',public)
    request=SimpleNamespace(method='GET',url='https://provider.example/form',redirected_from=SimpleNamespace(url='https://example.com/start'),frame=SimpleNamespace(parent_frame=None))
    assert await host.follow_public_redirect(uid,'redirect-run',request,['example.com'])
    public.assert_awaited_once_with(request.url)
    assert host.scopes[(uid,'redirect-run')]==['example.com','provider.example']
    request.redirected_from=SimpleNamespace(url='https://unrelated.example/')
    assert not await host.follow_public_redirect(uid,'redirect-run',request,['example.com'])
    request.redirected_from=SimpleNamespace(url='https://example.com/start');request.url='http://127.0.0.1/private'
    public.side_effect=ValueError('Private destination')
    with pytest.raises(ValueError):await host.follow_public_redirect(uid,'redirect-run',request,['example.com'])
    assert '127.0.0.1' not in host.scopes[(uid,'redirect-run')]


@pytest.mark.parametrize('label,tag,kind',[
    ('Color','BUTTON','button'),('White','LI',''),('Apply selected filters','BUTTON','button'),
    ('Date Today','BUTTON','button'),('Sunday, November 22, 2026.','BUTTON','button'),
    ('5:45 PM Dining','BUTTON','button'),('Log in','BUTTON','button'),('Sign in','BUTTON','button'),
])
@pytest.mark.parametrize('inspect_only',[False,True])
def test_routine_view_controls_do_not_request_per_click_approval(label,tag,kind,inspect_only):
    state=observed(label);state['node'].update(tag=tag,type=kind,role='button',form=None)
    assert classify_action('click',{},state,inspect_only=inspect_only,policy='poc')[0]=='preparation'


@pytest.mark.parametrize('label',['Next','Continue','Save and continue'])
def test_non_private_form_steps_are_automatic_but_final_submission_is_not(label):
    state=observed(label);state['node'].update(type='submit',form={'method':'post','action':'https://example.com/step','sensitive':False,'financial':False})
    assert classify_action('click',{},state,policy='poc')[0]=='preparation'
    state['node']['name']='Submit application'
    assert classify_action('click',{},state,policy='poc')[0]=='approval'


async def test_filter_actions_pass_the_boundary_without_approval_proposals(boundary):
    manager,user=boundary
    for i,label in enumerate(['Color','White','Apply selected filters'],1):
        state=observed(label);state['node'].update(form=None,tag='LI' if label=='White' else 'BUTTON',type='' if label=='White' else 'button')
        state.update(manual_required=True,human_required=False,fields=[{'type':'password','sensitive':True}])
        params={'index':i}
        await manager.before('browser-task',{'seq':i,'operation':'click','params':params,'observed':state,'fingerprint':action_binding('click',params,state)})
        assert manager.send.call_args.args[1]['decision']=='allow'
    assert not manager.db.rows("SELECT id FROM browser_use_actions WHERE category='approval'")
