import asyncio
import os
import socket
import time
import json
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.async_api import async_playwright,expect

from personal_agent import config
from personal_agent.app import create_app
from personal_agent.service import Service
from conftest import StubBrowsers
from test_background import QUERY
from dining_test_helpers import start_dining_runtime,agent_service,lookup_messages


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Enable real Chromium tests')
async def test_consolidated_availability_tables_render_and_survive_reload(database,users,credentials,monkeypatch):
    from langchain_core.messages import AIMessage
    from test_availability import service_for
    from availability.company import Stock
    from availability.presentation import availability_text
    from availability.security import Security
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    service=service_for(database,[AIMessage(content='Hi')]);user=users[0]
    database.execute('UPDATE users SET must_change=0 WHERE id=?',(user['id'],))
    sid=database.new_session(user['id']);security=Security(config.RUNTIME);results=[]
    for domain,c in [('dining',{'venue':'Venue Alpha','location':'Dubai','date':'2099-12-02','party_size':2}),
                     ('dining',{'venue':'Venue Alpha','location':'Dubai','date':'2099-12-03','party_size':2}),
                     ('hotels',{'hotel':'Hotel Alpha','destination':'Dubai','check_in':'2099-12-01','check_out':'2099-12-10','guests':2,'rooms':1})]:
        results.append(Stock(config.RUNTIME/(domain+'.sqlite'),domain,security).search('ui-'+str(len(results)),c))
    database.message(user['id'],sid,'assistant','**Hotels**\n\n'+availability_text([{'results':results}]))
    server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),
        ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:break
            await asyncio.sleep(.01)
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
            page=await browser.new_page(viewport={'width':1100,'height':900});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            await page.goto(config.ORIGIN)
            await page.locator('#username').fill(user['username']);await page.locator('#password').fill(credentials[user['username']]);await page.locator('#password').press('Enter')
            for reload in (False,True):
                if reload:await page.reload()
                await expect(page.locator('.response-table table')).to_have_count(2)
                await expect(page.locator('.message.assistant > p')).to_have_count(0)
                await expect(page.get_by_role('cell',name='Venue Alpha',exact=True)).to_have_count(1)
                await expect(page.get_by_role('cell',name='2099-12-02, 2099-12-03',exact=True)).to_be_visible()
                await expect(page.locator('.message.assistant')).not_to_contain_text('| --- |')
            await page.evaluate("message('assistant','- Older venue one\\n\\n- Older venue two',false)")
            await expect(page.locator('.message.assistant').last.locator('ul')).to_have_count(1)
            await expect(page.locator('.message.assistant').last.locator('li')).to_have_count(2)
            assert not errors
            await page.screenshot(path=str(config.ROOT/'.runtime/consolidated-availability.png'),full_page=True)
            await browser.close()
    finally:
        server.should_exit=True;await task;await service.close();await service.engine.client.close()


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Enable real Chromium tests')
async def test_company_alert_clear_login_persistence_and_watch_form(database,credentials,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    service=agent_service(database,lookup_messages()+lookup_messages(location='',date='',party_size=0,reply='Please provide location, date, party_size.')+lookup_messages(location='Miami',date='2099-12-02',party_size=3))
    dining_cleanup=await start_dining_runtime(service,database.path.parent)
    service.background.start()
    server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),
        ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:break
            await asyncio.sleep(.01)
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
            page=await browser.new_page(viewport={'width':1100,'height':900})
            requests=[];errors=[];page.on('request',lambda r:requests.append(r.url));page.on('pageerror',lambda e:errors.append(str(e)))
            await page.goto(config.ORIGIN)
            await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
            await page.locator('#password-form').wait_for(state='visible')
            await page.locator('#new-password').fill('controlled-background-demo-password');await page.locator('#new-password').press('Enter')
            await expect(page.locator('#composer')).to_be_visible()
            await page.locator('#input').fill(QUERY);started=time.monotonic();await page.locator('#input').press('Enter')
            await expect(page.locator('.message.assistant')).to_contain_text('currently unavailable')
            await page.locator('#input').fill('Unsent draft')
            await expect(page.locator('#background-toast')).to_be_visible(timeout=14000)
            elapsed=time.monotonic()-started;assert 9.5<=elapsed<14
            await expect(page.locator('#background-toast')).to_contain_text('A table has opened up')
            await expect(page.locator('#alerts')).to_contain_text('1')
            await expect(page.locator('.message.assistant').last).to_contain_text('A table has opened up at Carbone')
            await expect(page.locator('.message.assistant').last).to_contain_text('19:00')
            await expect(page.locator('#input')).to_have_value('Unsent draft')
            await page.evaluate('refreshBackground()')
            await expect(page.locator('.message.assistant').filter(has_text='A table has opened up')).to_have_count(1)
            await page.locator('#alerts').click()
            await expect(page.locator('#alerts')).to_be_hidden()
            await expect(page.locator('#background-toast')).to_be_hidden()
            await expect(page.locator('#background-panel')).to_be_hidden()
            await expect(page.locator('.message.assistant').filter(has_text='A table has opened up')).to_have_count(1)
            assert database.one('SELECT read_at FROM notifications')['read_at'] is not None
            assert database.one('SELECT COUNT(*) n FROM runs')['n']==1
            await page.reload()
            await expect(page.locator('#composer')).to_be_visible()
            await expect(page.locator('#alerts')).to_be_hidden()
            await expect(page.locator('#background-toast')).to_be_hidden()
            await page.locator('#auth').click()
            await expect(page.locator('#login-form')).to_be_visible()
            await page.locator('#username').fill('user01')
            await page.locator('#password').fill('controlled-background-demo-password')
            await page.locator('#password').press('Enter')
            await expect(page.locator('#composer')).to_be_visible()
            await expect(page.locator('#alerts')).to_be_hidden()
            await expect(page.locator('#background-toast')).to_be_hidden()
            assert not (await page.evaluate("fetch('/api/background').then(r=>r.json())"))['notifications']
            await expect(page.locator('.message.assistant').filter(has_text='A table has opened up')).to_have_count(1)
            # Clicking the popup also persists its dismissal.
            database.execute('UPDATE notifications SET read_at=NULL')
            await page.reload()
            await expect(page.locator('#background-toast')).to_be_visible()
            await page.locator('#background-toast').click()
            await expect(page.locator('#alerts')).to_be_hidden()
            await expect(page.locator('#background-toast')).to_be_hidden()
            await page.locator('#input').fill('Is Carbone available?');await page.locator('#input').press('Enter')
            await expect(page.locator('.message.assistant').last).to_contain_text('city, dining date, party size')
            await page.locator('#alerts').click()
            await expect(page.get_by_label('Location / city')).to_be_visible()
            await page.get_by_label('Location / city').fill('Miami');await page.get_by_label('Date',exact=True).fill('2099-12-02');await page.get_by_label('Party size',exact=True).fill('3')
            await page.get_by_role('button',name='Notify me when available',exact=True).click()
            await expect(page.locator('.message.assistant').last).to_contain_text('for 3 in Miami')
            await page.get_by_role('button',name='Cancel dining watch',exact=True).click()
            await expect(page.locator('#background-panel')).to_contain_text('Carbone watch · cancelled')
            assert database.one("SELECT COUNT(*) n FROM dining_watches WHERE status='active'")['n']==0
            # The new monitor projection shares this panel without reviving
            # dismissed alerts or touching the composer's unsent draft.
            uid=database.one("SELECT id FROM users WHERE username='user01'")['id']
            sid=database.one('SELECT id FROM sessions WHERE user_id=? ORDER BY created LIMIT 1',(uid,))['id']
            now=time.time()
            service.availability.store.execute('INSERT INTO monitors(id,user_id,session_id,envelope,expires,state,last_check,next_check,schedule_id) VALUES(?,?,?,?,?,?,?,?,?)',
                ('ui-monitor',uid,sid,json.dumps({'queries':[{'domain':'hotels','criteria':{'hotel':'The Fifth Avenue Hotel','check_in':'2099-12-01'}}],'preferences':{'monitor_interval_seconds':600}}),now+1200,'active',now,now+600,'ui-schedule'))
            await page.locator('#input').fill('Keep this monitor draft')
            await page.evaluate('refreshBackground()')
            await expect(page.locator('#background-panel')).to_contain_text('The Fifth Avenue Hotel')
            await expect(page.locator('#background-panel')).to_contain_text('Last check:')
            await expect(page.locator('#background-panel')).to_contain_text('Next check:')
            await expect(page.locator('#background-panel')).to_contain_text('Every 10 minutes')
            await page.get_by_role('button',name='Cancel monitor',exact=True).click()
            await expect(page.locator('#background-panel')).to_contain_text('Availability monitor · cancelled')
            await expect(page.locator('#input')).to_have_value('Keep this monitor draft')
            assert service.availability.store.one("SELECT state FROM monitors WHERE id='ui-monitor'")['state']=='cancelled'
            await page.set_viewport_size({'width':390,'height':844})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(config.ROOT/'.runtime/maintenance-20261119/background-demo-mobile.png'))
            assert all(url.startswith(config.ORIGIN) for url in requests)
            assert not errors
            await browser.close()
    finally:
        await service.close();server.should_exit=True;await task;await dining_cleanup()
