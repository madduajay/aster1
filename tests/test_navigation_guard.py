"""Navigation is replayable; real or ambiguous submissions remain guarded."""
import ast
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from personal_agent import config
from personal_agent.browser_policy import action_binding, classify_action, duplicate_key, navigation_action
from test_browser_tasks import boundary, observed


NAVIGATION=[
    {'name':'Categories','navigation_context':True},
    {'name':'Services','has_popup':'menu'},
    {'name':'Destinations','expanded':'false','controls_navigation':True},
    {'name':'Locations','role':'tab'},
    {'name':'Specifications','tag':'SUMMARY','details_toggle':True},
    {'name':'Products','tag':'A','role':'link','href':'https://example.com/catalog'},
]


@pytest.mark.parametrize('node',NAVIGATION)
@pytest.mark.parametrize('operation,params',[('click',{'index':7}),('click_selector',{'selector':'#observed'}),('send_keys',{'keys':'Enter'}),('send_keys',{'keys':'SPACE'})])
def test_navigation_is_generic_and_available_during_inspection(node,operation,params):
    state=observed();state['node'].update(node)
    assert navigation_action(operation,params,state)
    assert classify_action(operation,params,state)[0]=='read'
    assert classify_action(operation,params,state,inspect_only=True)[0]=='read'


def legacy_receipt(db,user,state,params,status):
    sid=db.new_session(user)
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('legacy-run',?,?,'cancelled',0)",(user,sid))
    db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES('legacy-task',?,'legacy-run','legacy-call','Earlier task','[\"example.com\"]',0,'cancelled',0)",(user,))
    old=dict(state);old['node']={k:v for k,v in state['node'].items() if k not in {'navigation_context','has_popup','expanded','controls_navigation','details_toggle'}}
    db.execute("INSERT INTO browser_use_actions(id,task_id,seq,operation,category,status,description,fingerprint,duplicate_key,before_state) VALUES('legacy-action','legacy-task',1,'click','approval',?,'{}','old-binding',?,?)",(status,duplicate_key('click',params,old),json.dumps(old)))


@pytest.mark.parametrize('status',['executed','dispatching','unknown','verified'])
@pytest.mark.parametrize('node',NAVIGATION)
async def test_legacy_misclassified_navigation_does_not_poison_later_tasks(boundary,status,node):
    manager,user=boundary;state=observed();state['node'].update(node);params={'index':8}
    legacy_receipt(manager.db,user,state,params,status)
    before=dict(manager.db.one("SELECT * FROM browser_use_actions WHERE id='legacy-action'"))
    await manager.before('browser-task',{'seq':1,'operation':'click','params':params,'observed':state,'fingerprint':action_binding('click',params,state)})
    assert manager.send.call_args.args[1]['decision']=='allow'
    assert manager.send.call_args.args[1]['category']=='read'
    assert manager.owned(user,'browser-run','browser-task')['status']=='running'
    assert dict(manager.db.one("SELECT * FROM browser_use_actions WHERE id='legacy-action'"))==before


@pytest.mark.parametrize('label',['Place order','Confirm booking','Submit application','Send message','Delete account','Add to Cart'])
async def test_navigation_hints_never_exempt_transaction_receipts(boundary,label):
    manager,user=boundary;state=observed(label);state['node'].update(navigation_context=True,has_popup='menu',expanded='false',controls_navigation=True)
    params={'index':9};legacy_receipt(manager.db,user,state,params,'unknown')
    assert not navigation_action('click',params,state)
    await manager.before('browser-task',{'seq':1,'operation':'click','params':params,'observed':state,'fingerprint':action_binding('click',params,state)})
    assert manager.send.call_args.args[1]['decision']=='deny'
    assert 'matching submission' in manager.send.call_args.args[1]['reason']
    assert manager.send.call_args.args[1]['inspect_only']


@pytest.mark.parametrize('extra',[
    {'form':{'method':'post','sensitive':False}},
    {'form':{'method':'get','sensitive':True}},
    {'type':'submit','form':{'method':'get','sensitive':False}},
    {'sensitive':True},
    {'role':'textbox','tag':'INPUT','type':'text'},
    {'href':'https://example.com/order/submit'},
    {'navigation_context':False},
])
def test_unknown_or_form_submitting_controls_are_not_inferred_as_navigation(extra):
    state=observed('Next');state['node'].update(navigation_context=True);state['node'].update(extra)
    assert not navigation_action('click',{},state)
    assert classify_action('click',{},state)[0]!='read'


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chromium requires explicit enablement')
async def test_actual_dom_metadata_is_generic_and_bound_to_the_observed_control(monkeypatch):
    # Read the exact trusted worker function without importing its isolated runtime.
    module=ast.parse((config.ROOT/'personal_agent/browser_use_worker.py').read_text())
    source=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='OBSERVE_NODE' for t in n.targets))
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
        try:
            page=await browser.new_page()
            await page.set_content('''<nav><button id=nav>Catalog</button><button id=order>Place order</button></nav>
            <button id=menu aria-haspopup=menu>Explore</button>
            <button id=disclosure aria-expanded=false aria-controls="links">Collections</button><section id=links><a href="https://example.com/catalog">Catalog</a></section>
            <button id=tab role=tab>Overview</button><details><summary id=summary>Details</summary>More content</details>
            <button id=application aria-expanded=false aria-controls="fields">Continue</button><section id=fields><form method=post><input type=password></form></section>''')
            for selector in ['#nav','#menu','#disclosure','#tab','#summary']:
                state=await page.locator(selector).evaluate('element => ('+source+').call(element)')
                assert navigation_action('click',{},state),state['node']
                assert classify_action('click',{},state)[0]=='read'
            for selector in ['#order','#application']:
                state=await page.locator(selector).evaluate('element => ('+source+').call(element)')
                assert not navigation_action('click',{},state)
                assert classify_action('click',{},state)[0]=='approval'
        finally:await browser.close()
