"""HTTPS UI -> real Service/LangGraph -> owned BrowserTasks -> Chromium handoff."""
import asyncio
import json
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import uvicorn
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import async_playwright, expect

from personal_agent import config, browser_host as host_module, browser_tasks as task_module
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.browser_host import BrowserHost
from personal_agent.browser_policy import action_binding
from personal_agent.browser_tasks import BrowserTasks, Worker
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chromium requires explicit enablement')
async def test_new_prompt_and_new_chat_cancel_paused_task_without_stop(database,users,credentials,tmp_path,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr(host_module,'public_url',AsyncMock());monkeypatch.setattr(task_module,'public_url',AsyncMock())
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    user=users[0]['id']
    class Host(BrowserHost):
        async def context(self,user):
            fresh=user not in self.contexts;ctx=await super().context(user)
            if fresh:
                await ctx.route('https://**.fixture.test/**',lambda r:r.fulfill(content_type='text/html',body='<h1>Website verification</h1><label>Verification code <input autocomplete="one-time-code" required></label>'))
            return ctx
    host=Host(database)
    class Simulation(BrowserTasks):
        simulations=[]
        async def start(self,user,run,call_id,objective,domains,inspect_only=False):
            previous=self.db.one('SELECT id FROM browser_use_tasks WHERE call_id=?',(call_id,))
            if previous:return previous['id']
            tid='task-'+run
            self.db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES(?,?,?,?,?,?,0,'running',0)",(tid,user,run,call_id,objective,json.dumps(domains)))
            host.set_scope(user,run,domains);host.run_for_user[user]=run
            page=(await host.context(user)).pages[0];await page.goto('https://identity.fixture.test/login')
            host.set_view(user,run,{'url':page.url});self.workers[tid]=Worker(SimpleNamespace(returncode=0),'nonce',user,run,asyncio.Lock())
            self.simulations.append(asyncio.create_task(self.pause(tid)));return tid
        async def pause(self,tid):
            w=self.workers[tid];state=await host.snapshot(w.user,w.run);params={'kind':'manual','prompt':'Complete verification directly in the website'}
            await self.before(tid,{'seq':1,'operation':'request_user','params':params,'observed':state,'fingerprint':action_binding('request_user',params,state)})
        async def terminate(self,tid):
            self.workers.pop(tid,None)
    manager=Simulation(database,host,'sk-controlled',None)
    call=lambda index:AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Wait for website verification','official_domains':['identity.fixture.test']},'id':'pause-'+str(index),'type':'tool_call'}])
    model=ScriptedModel(responses=[call(1),AIMessage(content='The new prompt was answered.'),call(2),AIMessage(content='Fresh chat answer.')])
    server=server_task=None
    async def until(condition):
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if condition():return
            await asyncio.sleep(.025)
        for task in manager.simulations:
            if task.done():task.result()
        raise TimeoutError('Replacement UI condition not met')
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'ui-replacement.sqlite')) as saver:
        engine=Engine(database,host,saver,'sk-controlled',model=model,browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
            server_task=asyncio.create_task(server.serve(sockets=[sock]));await until(lambda:server.started)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                page=await browser.new_page(ignore_https_errors=False,viewport={'width':1400,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(config.ORIGIN);await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
                await page.locator('#password-form').wait_for(state='visible');await page.locator('#new-password').fill('replacement-ui-password-2026');await page.locator('#new-password').press('Enter');await page.locator('#composer').wait_for(state='visible')
                async def prompt(text):
                    await expect(page.locator('#input')).to_be_enabled();await page.locator('#input').fill(text);await page.locator('#input').press('Enter')
                await prompt('Complete website verification')
                await expect(page.get_by_role('button',name='Return to chat',exact=True)).to_be_visible()
                old=dict(service.active(user))
                # A private code is rejected, never stored, and the UI remains usable.
                await prompt('654321')
                await expect(page.locator('#error')).to_contain_text('browser')
                assert service.active(user)['id']==old['id']
                assert not database.one("SELECT id FROM messages WHERE content LIKE '%654321%'")
                await prompt('Find blue shoes instead')
                await until(lambda:not service.active(user))
                await expect(page.locator('#messages')).to_contain_text('The new prompt was answered.')
                await expect(page.get_by_role('button',name='Return to chat',exact=True)).to_have_count(0)
                assert database.one('SELECT status FROM runs WHERE id=?',(old['id'],))['status']=='cancelled'
                assert database.one('SELECT status FROM browser_use_tasks WHERE run_id=?',(old['id'],))['status']=='cancelled'
                stale=await page.evaluate('''async r=>(await fetch('/api/runs/'+r.id+'/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_id:r.approval_id,text:'continue'})})).status''',old)
                assert stale==404
                await prompt('Complete another website verification')
                await expect(page.get_by_role('button',name='Return to chat',exact=True)).to_be_visible();paused=dict(service.active(user))
                await page.get_by_role('button',name='New Chat',exact=True).click()
                await expect(page.locator('#messages')).to_be_empty()
                assert not service.active(user)
                assert database.one('SELECT status FROM runs WHERE id=?',(paused['id'],))['status']=='cancelled'
                assert not database.one("SELECT id FROM browser_use_actions WHERE status='proposed'")
                await prompt('Tell me about colors')
                await until(lambda:not service.active(user));await expect(page.locator('#messages')).to_contain_text('Fresh chat answer.')
                new=database.one("SELECT session_id FROM messages WHERE content='Tell me about colors'")['session_id']
                assert new!=paused['session_id']
                assert 'Complete another website verification' not in str(model.seen[-1])
                assert not errors
                await browser.close()
        finally:
            await service.close();await manager.close();await host.close();await engine.client.close()
            await asyncio.gather(*manager.simulations,return_exceptions=True)
            if server:server.should_exit=True
            if server_task:await server_task


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real preview continuity check')
async def test_related_followup_inspects_preview_without_resuming_old_task(database,users,credentials,tmp_path,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr(host_module,'public_url',AsyncMock())
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    uid=users[0]['id'];host=BrowserHost(database,headless=True)
    class CompletedBrowser(BrowserTasks):
        calls=0
        read_flags=[]
        async def delegate(self,user,run,call_id,task,domains,inspect_only=False):
            self.calls+=1;self.read_flags.append(inspect_only);host.set_scope(user,run,domains);host.run_for_user[user]=run
            context=await host.context(user)
            await context.route('https://benefits.fixture.test/**',lambda route:route.fulfill(content_type='text/html',body='<main><h1>Card benefits</h1><p>Points and travel protection.</p></main>'))
            if self.calls==1:await context.pages[0].goto('https://benefits.fixture.test/')
            host.set_view(user,run,{'url':context.pages[0].url,'title':'Card benefits'})
            result={'status':'verified','verification':{'success':True,'reason':'Benefits visible.'},'url':context.pages[0].url}
            database.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created,result) VALUES(?,?,?,?,?,?,0,'completed',?,?)",('preview-task-'+str(self.calls),user,run,call_id,task,json.dumps(domains),time.time(),json.dumps(result)))
            return result
    manager=CompletedBrowser(database,host,'sk-controlled',None)
    model=ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Open the card benefits page and summarize it','official_domains':['benefits.fixture.test']},'id':'benefits','type':'tool_call'}]),
        AIMessage(content='The card benefits include points.'),
        AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Read all card benefits','official_domains':['benefits.fixture.test']},'id':'follow1','type':'tool_call'}]),
        AIMessage(content='All card benefits include points and travel protection.'),
        AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Read more benefits details','official_domains':['benefits.fixture.test']},'id':'follow2','type':'tool_call'}]),
        AIMessage(content='These benefits depend on eligibility.'),
        AIMessage(content='The same benefits are included.'),AIMessage(content='Hello!'),
        AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Open a new benefits page','official_domains':['benefits.fixture.test']},'id':'fresh-topic','type':'tool_call'}]),
        AIMessage(content='The benefits page was opened as a fresh request.')])
    server=server_task=None
    async def ready(condition):
        async with asyncio.timeout(20):
            while not condition():await asyncio.sleep(.025)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'preview-ui.sqlite')) as saver:
        engine=Engine(database,host,saver,'sk-controlled',model=model,browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
            server_task=asyncio.create_task(server.serve(sockets=[sock]));await ready(lambda:server.started)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                page=await browser.new_page(viewport={'width':1400,'height':1000});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(config.ORIGIN);await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
                await page.locator('#password-form').wait_for(state='visible');await page.locator('#new-password').fill('preview-continuity-password');await page.locator('#new-password').press('Enter');await page.locator('#composer').wait_for(state='visible')
                async def prompt(text):
                    await expect(page.locator('#input')).to_be_enabled();await page.locator('#input').fill(text);await page.locator('#input').press('Enter')
                    await ready(lambda:not service.active(uid))
                    await expect(page.locator('#messages .user').last).to_contain_text(text)
                await prompt('Open https://benefits.fixture.test/ and summarize card benefits')
                await expect(page.locator('#browser-view')).to_be_visible()
                original=await page.locator('#browser-frame').get_attribute('data-url')
                await page.evaluate("window.previewHides=0;new MutationObserver(()=>{if(document.getElementById('browser-view').hidden)window.previewHides++}).observe(document.getElementById('browser-view'),{attributes:true,attributeFilter:['hidden']})")
                await prompt('show me all card benefits')
                await expect(page.locator('#messages')).to_contain_text('All card benefits include points')
                await expect(page.locator('#browser-view')).to_be_visible()
                assert await page.locator('#browser-url').get_attribute('href')=='https://benefits.fixture.test/'
                assert await page.evaluate('window.previewHides')==0
                await page.reload();await expect(page.locator('#browser-view')).to_be_visible()
                await prompt('tell me more')
                await expect(page.locator('#messages')).to_contain_text('These benefits depend on eligibility.')
                await expect(page.locator('#browser-view')).to_be_visible()
                assert manager.calls==3
                assert manager.read_flags==[False,True,True]
                await page.locator('#browser-take-control').click()
                await expect(page.get_by_role('button',name='Return to chat',exact=True)).to_be_visible()
                await page.get_by_role('button',name='Return to chat',exact=True).click()
                await expect(page.locator('#browser-view')).to_be_hidden()
                await prompt('Which card benefits are included?')
                await expect(page.locator('#messages')).to_contain_text('The same benefits are included.')
                await expect(page.locator('#browser-view')).to_be_hidden()  # Explicit dismissal wins.
                await prompt('hello')
                await expect(page.locator('#messages')).to_contain_text('Hello!')
                await expect(page.locator('#browser-view')).to_be_hidden()
                assert manager.calls==3 and not errors
                assert not any(key[0]==uid for key in host.views)
                assert 'All card benefits include points' not in str(service.memory.context(uid,service.db.one("SELECT session_id FROM runs WHERE id=(SELECT run_id FROM browser_use_tasks ORDER BY created DESC LIMIT 1)")['session_id'],''))
                await prompt('Open the card benefits website again')
                await expect(page.locator('#messages')).to_contain_text('The benefits page was opened as a fresh request.')
                await expect(page.locator('#browser-view')).to_be_visible()
                assert manager.calls==4 and manager.read_flags[-1] is False
                await browser.close()
        finally:
            if server:server.should_exit=True
            if server_task:await server_task
            await host.close();await engine.client.close();sock.close()
