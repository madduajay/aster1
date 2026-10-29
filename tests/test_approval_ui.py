"""Real HTTPS UI approval flow with deterministic models; no provider calls."""
import asyncio
import json
import os
import socket
import time
from unittest.mock import AsyncMock

import pytest
import uvicorn
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import async_playwright,expect

from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel,ScriptedBrowser
from validation.browser_fixtures import FixtureBrowsers
from personal_agent import browser_host as host_module


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chromium requires explicit enablement')
async def test_approval_buttons_text_reload_and_stale_actions(database,users,credentials,tmp_path,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    monkeypatch.setattr(host_module,'public_url',AsyncMock())
    class Browser(ScriptedBrowser,FixtureBrowsers):
        def __init__(self):ScriptedBrowser.__init__(self);FixtureBrowsers.__init__(self,database)
        async def delegate(self,user,run,call_id,task,domains,inspect_only=False):
            if self.run_for_user.get(user)!=run:
                self.run_for_user[user]=run
                website=(await self.context(user)).pages[0];await website.goto('https://shop.example/product')
                self.set_view(user,run,{'url':website.url,'title':await website.title()})
            result=await ScriptedBrowser.delegate(self,user,run,call_id,task,domains,inspect_only)
            await self.freeze_view(user,run)
            return result
    browser_tools=Browser();assert browser_tools.headless is True
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Add the blue shoes, size 10, quantity 1 to cart. Stop before checkout.','official_domains':['example.com']},'id':'action','type':'tool_call'}]),AIMessage(content='The browser task finished.')])
    server=server_task=None
    async def until(condition):
        end=time.monotonic()+20
        while time.monotonic()<end:
            if condition():return
            await asyncio.sleep(.03)
        raise TimeoutError('Approval UI condition not met')
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'ui-checkpoints.sqlite')) as saver:
        engine=Engine(database,browser_tools,saver,'sk-controlled-placeholder',model=model,browser_tasks=browser_tools)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
            server_task=asyncio.create_task(server.serve(sockets=[sock]));await until(lambda:server.started)
            async with async_playwright() as pw:
                chromium=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                page=await chromium.new_page(ignore_https_errors=False,viewport={'width':1100,'height':900})
                problems=[];page.on('pageerror',lambda e:problems.append(type(e).__name__))
                await page.goto(config.ORIGIN)
                await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
                await page.locator('#password-form').wait_for(state='visible')
                await page.locator('#new-password').fill('controlled-ui-password-2026');await page.locator('#new-password').press('Enter')
                await page.locator('#composer').wait_for(state='visible')
                user=users[0]['id'];previous=None
                for index,choice in enumerate(['button-approve','button-cancel','text-approve','text-cancel']):
                    await page.locator('#input').fill('Add the blue shoes to cart, request '+str(index));await page.locator('#input').press('Enter')
                    await until(lambda:service.active(user) and service.active(user)['status']=='approval')
                    active=dict(service.active(user))
                    await expect(page.get_by_role('button',name='Approve',exact=True)).to_be_visible()
                    await expect(page.get_by_role('button',name='Cancel',exact=True)).to_be_visible()
                    await expect(page.locator('#browser-view')).to_be_visible()
                    await expect(page.locator('#browser-frame')).not_to_have_js_property('naturalWidth',0)
                    await expect(page.locator('#browser-url')).to_contain_text('shop.example')
                    text=await page.locator('#messages').inner_text()
                    assert active['approval_id'] not in text and '"operation"' not in text and '"verification"' not in text
                    assert 'size 10' in text and 'example.com' in text
                    # Bare API text has no displayed-action binding. The UI's
                    # text shortcut uses the same exact-ID endpoint as buttons.
                    unbound=await page.evaluate('''async sid => (await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,text:'approve'})})).status''',active['session_id'])
                    assert unbound==409 and service.active(user)['approval_id']==active['approval_id']
                    if previous:
                        stale=await page.evaluate('''async ({run,id}) => (await fetch('/api/runs/'+run+'/approval',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approval_id:id,decision:'approve'})})).status''',{'run':previous['id'],'id':previous['approval_id']})
                        assert stale==404 and service.active(user)['approval_id']==active['approval_id']
                    if index==1:
                        await page.reload();await expect(page.get_by_role('button',name='Cancel',exact=True)).to_be_visible()
                    if index==0:await page.screenshot(path=str(config.ROOT/'.runtime/browser-view-ui.png'))
                    if index==1:
                        await page.set_viewport_size({'width':390,'height':844})
                        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        await page.screenshot(path=str(config.ROOT/'.runtime/browser-view-mobile.png'))
                    if choice.startswith('button'):
                        await page.get_by_role('button',name='Approve' if choice.endswith('approve') else 'Cancel',exact=True).click()
                    else:
                        await page.locator('#input').fill('approve' if choice.endswith('approve') else 'cancel');await page.locator('#input').press('Enter')
                    await until(lambda:not service.active(user) and not service.tasks)
                    await expect(page.get_by_role('button',name='Approve',exact=True)).to_have_count(0)
                    await expect(page.get_by_role('button',name='Cancel',exact=True)).to_have_count(0)
                    assert len(browser_tools.completed)==(index//2+1)
                    last=database.one("SELECT content FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1")['content']
                    assert last==('Approve' if choice.endswith('approve') else 'Cancel')
                    previous=active
                assert not problems
                await page.set_viewport_size({'width':390,'height':844})
                await page.get_by_role('button',name='Logout',exact=True).click();await page.locator('#login-form').wait_for(state='visible')
                await chromium.close()
        finally:
            await service.close();await browser_tools.close();await engine.client.close()
            if server:server.should_exit=True
            if server_task:await server_task
