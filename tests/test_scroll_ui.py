"""Reading position and focus survive streaming/completion in the real HTTPS UI."""
import asyncio
import json
import os
import socket
from unittest.mock import AsyncMock

import pytest
import uvicorn
from playwright.async_api import async_playwright,expect

from personal_agent import config,browser_host as host_module
from personal_agent.app import create_app
from personal_agent.browser_tasks import BrowserTasks
from validation.browser_fixtures import FixtureBrowsers
from conftest import StubService


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Enable real Chromium tests')
async def test_reading_position_and_focus_survive_response_completion(database,users,credentials,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr(host_module,'public_url',AsyncMock())
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    user=users[0]['id'];sid=database.new_session(user);run='scroll-run'
    for i in range(24):
        database.message(user,sid,'user','Earlier question '+str(i))
        database.message(user,sid,'assistant',('Earlier answer '+str(i)+': '+('A useful explanation. '*30)))
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(run,user,sid))
    host=FixtureBrowsers(database);BrowserTasks(database,host,'sk-controlled-placeholder',None)
    database.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES('scroll-browser',?,?, 'scroll-call','Read fixture','[\"shop.example\"]',1,'completed',0)",(user,run))
    host.run_for_user[user]=run;website=(await host.context(user)).pages[0];await website.goto('https://shop.example/product')
    host.set_view(user,run,{'url':website.url,'title':await website.title()})
    service=StubService(database);service.engine.browsers=host
    server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:break
            await asyncio.sleep(.01)
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
            page=await browser.new_page(ignore_https_errors=False,viewport={'width':1200,'height':900})
            errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
            await page.goto(config.ORIGIN)
            await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
            await page.locator('#password-form').wait_for(state='visible')
            await page.locator('#new-password').fill('controlled-scroll-password');await page.locator('#new-password').press('Enter')
            await expect(page.locator('.message')).to_have_count(48)
            await expect(page.locator('#browser-frame')).not_to_have_js_property('naturalWidth',0)
            await page.locator('#input').fill('A draft that must stay here')
            await page.locator('#browser-view summary').focus()
            await page.evaluate("""() => {window.reader=document.querySelectorAll('.message')[12];window.scrollTo(0,reader.offsetTop-180);window.panelWasHidden=false;new MutationObserver(()=>{if(!document.body.classList.contains('browser-open'))window.panelWasHidden=true}).observe(document.body,{attributes:true,attributeFilter:['class']})}""")
            await asyncio.sleep(.1)
            position=await page.evaluate('scrollY');assert position>500
            focused=await page.evaluate('document.activeElement.tagName')
            database.event(run,'token','A new streamed answer.\n'+('More answer text. '*200))
            await expect(page.locator('.message.assistant').last).to_contain_text('A new streamed answer.')
            assert abs(await page.evaluate('scrollY')-position)<3
            final='A new streamed answer.\n'+('More answer text. '*200)+'\nComplete.'
            mid=database.message(user,sid,'assistant',final);database.event(run,'answer',final)
            await host.freeze_view(user,run)
            database.execute("UPDATE runs SET status='completed' WHERE id=?",(run,));database.event(run,'done',{'status':'completed'})
            await expect(page.locator('.message').last).to_have_attribute('data-message-id',str(mid))
            await expect(page.locator('#status')).to_have_text('')
            await asyncio.sleep(.15)
            assert abs(await page.evaluate('scrollY')-position)<3
            assert await page.evaluate("reader===document.querySelectorAll('.message')[12]")
            assert not await page.evaluate('panelWasHidden')
            assert await page.evaluate('document.activeElement.tagName')==focused
            await expect(page.locator('#input')).to_have_value('A draft that must stay here')
            # An explicit reload opens at the latest message; following new
            # output there should still work normally.
            await page.reload();await expect(page.locator('.message')).to_have_count(49)
            await expect(page.locator('#browser-frame')).not_to_have_js_property('naturalWidth',0)
            await page.evaluate('window.scrollTo(0,document.documentElement.scrollHeight)')
            # Feed a real event on a fresh cursor rather than simulating DOM changes.
            database.execute("UPDATE runs SET status='running' WHERE id=?",(run,))
            cursor=database.one('SELECT COALESCE(MAX(id),0) AS id FROM events')['id']
            await page.evaluate('(cursor)=>listen("scroll-run",cursor)',cursor)
            database.event(run,'token','Following at the bottom.\n'+('New output. '*300))
            await expect(page.locator('.message').last).to_contain_text('Following at the bottom.')
            await asyncio.sleep(.1)
            assert await page.evaluate('document.documentElement.scrollHeight-innerHeight-scrollY')<4
            assert not errors
            await browser.close()
    finally:
        server.should_exit=True;await task;await host.close()
