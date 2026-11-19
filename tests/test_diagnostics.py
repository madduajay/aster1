import asyncio,json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from personal_agent.browser_diagnostics import diagnostic_stage,exception_details,diagnostic_text,clean_message,safe_target,load_diagnostics
from personal_agent.browser_tasks import BrowserTasks
from personal_agent.service import Service
from personal_agent import browser_tasks as module
from test_browser_tasks import StubBrowserHost,task_row,observed
from conftest import authenticate


def controlled_error():
    try:
        with diagnostic_stage('resolve_target_reference'):
            raise KeyError('backendNodeId')
    except Exception as exc:
        return exception_details(exc,operation='click',stage='observe_before_action',params={'index':71},state={},dispatched=False)


def test_preserves_real_exception_stage_source_and_requested_target():
    details=controlled_error()
    assert details['error_class']=='KeyError' and details['message']=="'backendNodeId'"
    assert details['observation_stage']=='resolve_target_reference'
    assert details['stage']=='observe_before_action' and details['target']=={'index':71}
    assert details['trace'][-1]['file']=='test_diagnostics.py' and details['trace'][-1]['line']>0
    body=diagnostic_text(details)
    assert 'KeyError' in body and 'resolve_target_reference' in body and 'index=71' in body and 'Action dispatched: no' in body
    assert 'stale target' not in body


def test_diagnostics_exclude_credentials_private_values_query_and_locals():
    private='private-typed-password'
    try:
        raise RuntimeError(f'Cannot type {private} at https://secret:pass@example.com/login?token=keep-private#otp ; Bearer sk-proj-topsecretvalue ; password=hidden-password ; Cookie: session=hidden-session ; user@example.com 654321')
    except Exception as exc:
        details=exception_details(exc,operation='input',stage='execute_action',params={'index':3,'text':private},state={'node':{'name':'Password','value':private,'sensitive':True}},dispatched=True)
    encoded=json.dumps(details)
    for value in [private,'keep-private','secret:pass','sk-proj-topsecretvalue','hidden-password','hidden-session','user@example.com','654321']:
        assert value not in encoded,value
    assert 'example.com/login' in encoded and all('locals' not in frame for frame in details['trace']) and 'text' not in details['target']
    assert details['target']['label']=='[private field]'
    assert 'secret-value' not in str(safe_target({'selector':'input[value="secret-value"]'},{}))
    assert clean_message('Bad URL https://[invalid')=='Bad URL [URL]'
    assert 'second-cookie' not in clean_message('Connection failed; Cookie: first=a; second=second-cookie')
    assert 'json-secret' not in clean_message('{"password": "json-secret"}')


async def test_details_persist_before_action_record_and_are_owned(client,database,users,credentials,monkeypatch):
    from personal_agent.browser_tasks import Worker
    manager=BrowserTasks(database,StubBrowserHost(),'sk-test',None);user=users[0]['id'];sid=task_row(database,user)
    manager.workers['browser-task']=Worker(SimpleNamespace(returncode=None),'nonce',user,'browser-run',asyncio.Lock())
    manager.record_diagnostic('browser-task',controlled_error(),seq=4)
    assert not database.rows('SELECT * FROM browser_use_actions')  # Failure occurred before action registration.
    assert len(database.rows('SELECT * FROM browser_use_diagnostics'))==1
    assert load_diagnostics(database,users[1]['id'],run='browser-run')==[]
    items=load_diagnostics(database,user,run='browser-run')
    assert items[0]['target']['index']==71
    event=json.loads(database.one("SELECT payload FROM events WHERE kind='diagnostic'")['payload'])
    assert 'KeyError' in event['text']
    database.execute("UPDATE browser_use_tasks SET status='completed',result=? WHERE id='browser-task'",(json.dumps({'status':'unverified','verification':{'reason':'Target inspection failed.'}}),))
    service=Service(database,SimpleNamespace(model=None))
    result=service.browser_outcome(user,'browser-run','Fallback answer')
    assert 'KeyError' in result and 'Source:' in result and 'index=71' in result
    assert 'KeyError' in service.command(user,sid,0,'/debug')
    assert 'No browser exception' in service.command(user,database.new_session(user),0,'/debug')
    authenticate(client,credentials,'user01')
    result=client.get('/api/runs/browser-run/diagnostics')
    assert result.status_code==200 and result.json()['diagnostics'][0]['error_class']=='KeyError'
    client.cookies.clear();authenticate(client,credentials,'user02')
    assert client.get('/api/runs/browser-run/diagnostics').status_code==404


async def test_exact_snapshot_failure_stage_and_exception_is_preserved(monkeypatch):
    from personal_agent import browser_use_worker as worker
    client=SimpleNamespace(cdp_client=SimpleNamespace(send=SimpleNamespace(Runtime=SimpleNamespace(evaluate=AsyncMock(side_effect=ConnectionError('Read failed'))))),session_id='session')
    browser=SimpleNamespace(get_or_create_cdp_session=AsyncMock(return_value=client))
    with pytest.raises(ConnectionError) as error:await worker._observe(browser)
    assert error.value.browser_stage=='page_snapshot'


@pytest.mark.skipif(__import__('os').environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chromium requires explicit enablement')
async def test_debug_details_visible_in_https_chat_and_after_reload(database,users,credentials,tmp_path,monkeypatch):
    import socket,time,uvicorn
    from playwright.async_api import async_playwright,expect
    from langchain_core.messages import AIMessageChunk
    from personal_agent import config
    from personal_agent.app import create_app
    from personal_agent.browser_tasks import Worker
    from conftest import SummaryModel
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    manager=BrowserTasks(database,StubBrowserHost(),'sk-controlled',None)
    database.execute('CREATE TABLE run_usage(run_id TEXT PRIMARY KEY,searches INTEGER DEFAULT 0)')
    class Graph:
        def __init__(self,user,run,context):self.user,self.run=user,run
        async def astream(self,*args,**kwargs):
            tid='display-diagnostic'
            database.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created,result) VALUES(?,?,?,?,?,'[\"example.com\"]',0,'completed',0,?)",(tid,self.user,self.run,'debug-ui-call','Open a product',json.dumps({'status':'unverified','verification':{'reason':'The product target could not be inspected.'}})))
            manager.workers[tid]=Worker(SimpleNamespace(returncode=0),'nonce',self.user,self.run,asyncio.Lock())
            manager.record_diagnostic(tid,controlled_error(),seq=4)
            yield 'messages',(AIMessageChunk(content='The browser task could not finish.'),{'langgraph_node':'model'})
    service=Service(database,SimpleNamespace(model=SummaryModel(),browsers=manager.browsers,graph=lambda user,run,context,*args:Graph(user,run,context)))
    server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    async def until(condition):
        end=time.monotonic()+15
        while time.monotonic()<end:
            if condition():return
            await asyncio.sleep(.025)
        raise TimeoutError('Debug UI did not reach expected state')
    try:
        await until(lambda:server.started)
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
            page=await browser.new_page(ignore_https_errors=False)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            await page.goto(config.ORIGIN);await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter');await page.locator('#password-form').wait_for(state='visible');await page.locator('#new-password').fill('diagnostic-ui-test-password');await page.locator('#new-password').press('Enter');await page.locator('#composer').wait_for(state='visible')
            await page.locator('#input').fill('Open a product');await page.locator('#input').press('Enter')
            await expect(page.locator('#messages')).to_contain_text('Browser debug details:')
            for text in ['KeyError','backendNodeId','resolve_target_reference','test_diagnostics.py:','index=71','Action dispatched: no']:
                await expect(page.locator('#messages')).to_contain_text(text)
            await until(lambda:not service.active(users[0]['id']))
            await page.reload();await expect(page.locator('#messages')).to_contain_text('KeyError')
            await page.locator('#input').fill('/debug');await page.locator('#input').press('Enter')
            await expect(page.locator('#messages .assistant').last).to_contain_text('resolve_target_reference')
            assert not errors
            assert '"error_class"' not in await page.locator('#messages').inner_text()
            await page.screenshot(path=str(config.ROOT/'.runtime/exception-debug-ui.png'))
            await browser.close()
    finally:
        await service.close();server.should_exit=True;await task
