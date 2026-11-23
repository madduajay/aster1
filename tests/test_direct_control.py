import asyncio
import json
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.app import browser_panel_closed,create_app
from personal_agent.browser_host import BrowserHost,page_viewport
from personal_agent.db import Database
from personal_agent.service import Service
from fastapi.testclient import TestClient
from conftest import authenticate
from test_agent import ScriptedModel,wait_service
from test_availability import service_for


def completed_browser(db,user):
    sid=db.new_session(user);rid='direct-'+str(time.time_ns());tid='task-'+rid
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'completed',?)",(rid,user,sid,time.time()))
    db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created,result) VALUES(?,?,?,?,?,?,0,'completed',?,?)",(tid,user,rid,tid,'Open contact form','["forms.example"]',time.time(),json.dumps({'url':'https://forms.example/application','title':'Contact form','status':'verified'})))
    return sid,rid


async def test_completed_task_control_is_owned_exclusive_and_done_does_not_resume_agent(database,users):
    uid=users[0]['id'];service=service_for(database,[AIMessage(content='Must not run')])
    host=SimpleNamespace(view_info=lambda *a:{'available':False},open_takeover=AsyncMock(),release_control=AsyncMock(),freeze_view=AsyncMock(),preview_locks={})
    service.engine.browsers=host;sid,rid=completed_browser(database,uid)
    try:
        with pytest.raises(LookupError):await service.take_browser_control(users[1]['id'],rid)
        await service.take_browser_control(uid,rid)
        run=service.active(uid);pause=json.loads(run['approval'])['action']
        assert pause['purpose']=='direct_control' and pause['task_id']=='task-'+rid
        assert await service.take_browser_control(uid,rid)=={'run_id':rid}
        assert host.open_takeover.await_count==1
        sid2,rid2=completed_browser(database,uid)
        with pytest.raises(ValueError):await service.take_browser_control(uid,rid2)
        with pytest.raises(ValueError):await service.respond_input(uid,rid,run['approval_id'],'private input must not be stored')
        result=await service.respond_input(uid,rid,run['approval_id'],'done',True)
        assert result['completed'] and database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='completed'
        assert not service.tasks
        assert 'private input must not be stored' not in str(database.rows('SELECT content FROM messages'))
        host.release_control.assert_awaited_once_with(uid,rid)
        with pytest.raises(LookupError):await service.respond_input(uid,rid,run['approval_id'],'done',True)
    finally:await service.close();await service.engine.client.close()


async def test_control_open_failure_restores_completed_state(database,users):
    service=service_for(database,[AIMessage(content='Hi')]);uid=users[0]['id'];sid,rid=completed_browser(database,uid)
    service.engine.browsers=SimpleNamespace(view_info=lambda *a:{'available':False},open_takeover=AsyncMock(side_effect=ValueError('Page unavailable')))
    try:
        with pytest.raises(ValueError):await service.take_browser_control(uid,rid)
        assert service.active(uid) is None
        assert database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='completed'
    finally:await service.close();await service.engine.client.close()


async def test_return_to_chat_keeps_required_private_step_paused_without_model_execution(database,users):
    service=service_for(database,[AIMessage(content='Hello! How can I help?')]);uid=users[0]['id'];sid,rid=completed_browser(database,uid)
    host=SimpleNamespace(release_control=AsyncMock(),freeze_view=AsyncMock(),manual_ready=AsyncMock(return_value=False))
    service.engine.browsers=host
    database.execute("UPDATE runs SET status='input',approval_id='current',approval=? WHERE id=?",(json.dumps({'action':{'kind':'manual','prompt':'Complete verification in the browser','task_id':'task-'+rid}}),rid))
    try:
        with pytest.raises(LookupError):await service.return_to_chat(users[1]['id'],rid,'current')
        with pytest.raises(LookupError):await service.return_to_chat(uid,rid,'stale')
        await service.return_to_chat(uid,rid,'current')
        assert service.active(uid)['status']=='input' and not service.tasks
        host.manual_ready.assert_not_awaited()
        host.release_control.assert_awaited_once_with(uid,rid)
        assert browser_panel_closed(database,rid)
        with pytest.raises(ValueError):await service.respond_input(uid,rid,'current','continue',True)
        assert browser_panel_closed(database,rid) and service.active(uid)['status']=='input'
        with pytest.raises(ValueError):await service.submit(uid,sid,'654321',pending_input_id='current')
        assert not database.one("SELECT id FROM messages WHERE content LIKE '%654321%'")
        database.execute("UPDATE runs SET approval_id='new-pause' WHERE id=?",(rid,))
        assert not browser_panel_closed(database,rid)
        database.execute("UPDATE runs SET approval_id='current' WHERE id=?",(rid,))
        fresh=await service.submit(uid,sid,'hello',pending_input_id='current')
        await wait_service(service,fresh['run_id'])
        assert database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='cancelled'
        assert database.one('SELECT status FROM runs WHERE id=?',(fresh['run_id'],))['status']=='completed'
    finally:await service.close();await service.engine.client.close()


def test_take_control_endpoint_requires_login_matching_origin_and_ownership(database,users,credentials):
    service=service_for(database,[AIMessage(content='Must not run')]);uid=users[0]['id'];sid,rid=completed_browser(database,uid)
    service.engine.browsers=SimpleNamespace(view_info=lambda *a:{'available':False},open_takeover=AsyncMock(),close_user=AsyncMock())
    path='/api/runs/'+rid+'/browser/take-control'
    try:
        with TestClient(create_app(database,service),base_url=config.ORIGIN,headers={'origin':config.ORIGIN}) as client:
            assert client.post(path,json={}).status_code==401
            authenticate(client,credentials,users[1]['username'])
            assert client.post(path,json={}).status_code==404
            client.post('/api/logout')
            authenticate(client,credentials)
            assert client.post(path,json={},headers={'origin':'https://untrusted.example'}).status_code==403
            assert client.post(path,json={}).status_code==200
            assert service.engine.browsers.open_takeover.await_count==1
    finally:asyncio.run(service.engine.client.close())


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real completed-task keyboard takeover check')
@pytest.mark.parametrize('browser_mode',['headless','visible'])
@pytest.mark.parametrize('screen', [{'width':1400,'height':1000},{'width':390,'height':844}])
async def test_take_control_button_types_into_completed_form_and_done_ignores_empty_phone(database,users,credentials,monkeypatch,screen,browser_mode):
    import uvicorn
    from playwright.async_api import async_playwright,expect
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr('personal_agent.browser_host.public_url',AsyncMock())
    class Host(BrowserHost):
        scroll_calls=0
        async def context(self,user):
            fresh=user not in self.contexts;ctx=await super().context(user)
            if fresh:await ctx.route('https://forms.example/**',lambda route:route.fulfill(status=200,content_type='text/html',body='<title>Login form</title><style>body{font:16px sans-serif;margin:8px}label{display:block;margin:12px 0}#scroller{height:180px;overflow:auto;background:#eef}#inside{height:1600px}#tail{height:2400px}</style><h1>Login form</h1><label>User ID<input id=name autofocus autocomplete=username></label><label>Password<input id=private type=password required autocomplete=current-password></label><div id=scroller><button onclick="window.taps=(window.taps||0)+1">Do not tap while swiping</button><div id=inside>Scrollable website area</div></div><div id=tail>More website content</div>'))
            return ctx
        async def manual_action(self,*args,**kwargs):
            if args[3]=='scroll':
                self.scroll_calls+=1
                await asyncio.sleep(.15) # Model a slow tunnel acknowledgement.
            return await super().manual_action(*args,**kwargs)
    host=Host(database,headless=browser_mode=='headless');engine=Engine(database,host,InMemorySaver(),'sk-test',model=ScriptedModel(responses=[AIMessage(content='Hello! How can I help?')]))
    service=Service(database,engine);uid=users[0]['id'];sid,rid=completed_browser(database,uid)
    database.execute('UPDATE browser_use_tasks SET result=? WHERE run_id=?',(json.dumps({'status':'verified','url':'https://forms.example/login','title':'Login form'}),rid))
    database.execute('UPDATE users SET must_change=0')
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();monkeypatch.setattr(config,'ORIGIN','https://aster.localhost:'+str(sock.getsockname()[1]))
    server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False));running=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:await asyncio.sleep(.01)
        old_page=(await host.context(uid)).pages[0]
        await old_page.goto('https://forms.example/unrelated-shopping-page')
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,chromium_sandbox=True);page=await browser.new_page(viewport=screen,has_touch=True)
            await page.add_init_script("Object.defineProperty(navigator,'platform',{get:()=> 'Linux x86_64'})")
            await page.goto(config.ORIGIN);await page.locator('#username').fill(users[0]['username']);await page.locator('#password').fill(credentials[users[0]['username']]);await page.locator('#password').press('Enter')
            await expect(page.get_by_role('button',name='Take control',exact=True)).to_be_visible()
            await page.get_by_role('button',name='Take control',exact=True).click()
            await expect(page.locator('body')).to_have_class(__import__('re').compile(r'takeover'))
            frame=page.locator('#browser-frame');await expect(frame).not_to_have_js_property('naturalWidth',0)
            await page.wait_for_function("()=>pendingInput?.control?.connected&&(pendingInput.control.native?Math.abs(document.querySelector('#browser-frame').naturalWidth/document.querySelector('#browser-frame').naturalHeight-pendingInput.control.width/pendingInput.control.height)<0.015:document.querySelector('#browser-frame').naturalWidth===pendingInput.control.width&&document.querySelector('#browser-frame').naturalHeight===pendingInput.control.height)")
            website=await host.view_page(uid,rid);field=website.locator('#name');box=await field.bounding_box();imagebox=await frame.bounding_box()
            assert website.url=='https://forms.example/login'
            assert old_page.url=='https://forms.example/unrelated-shopping-page'
            async def assert_native_window():
                if browser_mode=='visible':
                    probe=await website.context.new_cdp_session(website)
                    try:
                        window=await probe.send('Browser.getWindowForTarget')
                        assert window['bounds']['windowState']=='normal'
                    finally:await probe.detach()
            await assert_native_window()
            assert (website.viewport_size is None)==(browser_mode=='visible')
            site_size=await website.evaluate('({width:innerWidth,height:innerHeight})')
            assert site_size==await page_viewport(website)
            # The remote site autofocuses User ID, but the local document owns
            # focus. Page-level keys/paste must reach the remote field; typing
            # in the chat composer must remain local.
            await field.focus()
            await page.evaluate('()=>document.activeElement.blur()')
            await page.keyboard.type('X');await expect(field).to_have_value('X')
            await page.keyboard.press('Backspace');await expect(field).to_have_value('')
            await page.evaluate('()=>document.activeElement.blur()')
            await page.evaluate("()=>{const data=new DataTransfer();data.setData('text/plain','Y');document.body.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));}")
            await expect(field).to_have_value('Y')
            await page.keyboard.press('Backspace');await expect(field).to_have_value('')
            if screen['width']>=1050:
                panel=await page.locator('#browser-view').bounding_box()
                assert 650<=panel['width']<=900 and panel['x']>250
                await page.locator('#input').fill('local chat draft')
                await expect(field).to_have_value('');await page.locator('#input').fill('')
                site_size=await website.evaluate('({width:innerWidth,height:innerHeight})')
                box=await field.bounding_box();imagebox=await frame.bounding_box()
            scale=min(imagebox['width']/site_size['width'],imagebox['height']/site_size['height'])
            field_point={'x':(imagebox['width']-site_size['width']*scale)/2+(box['x']+8)*scale,'y':(imagebox['height']-site_size['height']*scale)/2+(box['y']+8)*scale}
            if screen['width']<600:await page.touchscreen.tap(imagebox['x']+field_point['x'],imagebox['y']+field_point['y'])
            else:await frame.click(position=field_point)
            try:await expect(page.locator('#website-keyboard')).to_be_focused()
            except AssertionError:
                raise AssertionError({'input_error':await page.locator('#error').inner_text(),
                    'geometry':await page.evaluate("()=>({width:pendingInput.control.width,height:pendingInput.control.height,frame:[document.querySelector('#browser-frame').naturalWidth,document.querySelector('#browser-frame').naturalHeight]})"),
                    'site_size':website.viewport_size,'point':field_point,'field_box':box}) from None
            previous_frame=await frame.get_attribute('src')
            await page.keyboard.type('Alex')
            await expect(field).to_have_value('Alex')
            await page.wait_for_function("old=>document.querySelector('#browser-frame').src!==old",arg=previous_frame)
            await assert_native_window()
            # Keyboard/height changes must scale the stream, not reflow fields.
            remote_size=await website.evaluate('({width:innerWidth,height:innerHeight})')
            await page.set_viewport_size({**screen,'height':screen['height']-140})
            await asyncio.sleep(.5)
            site_size=await website.evaluate('({width:innerWidth,height:innerHeight})')
            if screen['width']<600 or browser_mode=='visible':assert site_size==remote_size
            else:assert site_size['height']<remote_size['height']
            await page.set_viewport_size(screen)
            await assert_native_window()
            await frame.focus()
            await asyncio.sleep(.4)
            # A wheel burst over a nested scroller stays inside the website;
            # delayed acknowledgements produce at most one buffered scroll.
            await page.wait_for_function("()=>(pendingInput.control.native?Math.abs(document.querySelector('#browser-frame').naturalWidth/document.querySelector('#browser-frame').naturalHeight-pendingInput.control.width/pendingInput.control.height)<0.015:document.querySelector('#browser-frame').naturalWidth===pendingInput.control.width&&document.querySelector('#browser-frame').naturalHeight===pendingInput.control.height)")
            scrollbox=await website.locator('#scroller').bounding_box();imagebox=await frame.bounding_box()
            site_size=await website.evaluate('({width:innerWidth,height:innerHeight})')
            scale=min(imagebox['width']/site_size['width'],imagebox['height']/site_size['height'])
            point={'x':imagebox['x']+(imagebox['width']-site_size['width']*scale)/2+(scrollbox['x']+50)*scale,'y':imagebox['y']+(imagebox['height']-site_size['height']*scale)/2+(scrollbox['y']+100)*scale}
            chat_y=await page.evaluate('scrollY');panel_y=(await page.locator('#browser-view').bounding_box())['y']
            await page.evaluate('''p=>{const image=document.querySelector('#browser-frame');for(let i=0;i<40;i++)image.dispatchEvent(new WheelEvent('wheel',{clientX:p.x,clientY:p.y,deltaY:12,bubbles:true,cancelable:true}));}''',point)
            await page.evaluate('async()=>{await takeoverTail;}')
            await website.wait_for_function("document.querySelector('#scroller').scrollTop>200")
            assert host.scroll_calls<=2
            assert await website.evaluate('scrollY')==0
            assert abs(await page.evaluate('scrollY')-chat_y)<3
            assert abs((await page.locator('#browser-view').bounding_box())['y']-panel_y)<3
            # Real touch gestures scroll without sending a click to the site.
            await website.locator('#scroller').evaluate('(node)=>node.scrollTop=0')
            cdp=await page.context.new_cdp_session(page)
            for kind,dy in [('touchStart',0),('touchMove',-35),('touchMove',-65),('touchEnd',-65)]:
                await cdp.send('Input.dispatchTouchEvent',{'type':kind,'touchPoints':[] if kind=='touchEnd' else [{'x':point['x'],'y':point['y']+dy,'id':1}]})
            await page.evaluate('async()=>{await takeoverTail;}')
            await website.wait_for_function("document.querySelector('#scroller').scrollTop>30")
            assert await website.evaluate('window.taps||0')==0
            assert await website.evaluate('scrollY')==0
            await cdp.detach()
            await page.reload();await expect(page.get_by_role('button',name='Return to chat',exact=True)).to_be_visible();await expect(field).to_have_value('Alex')
            panel=await page.locator('#browser-view').bounding_box();surface=await page.locator('#browser-surface').bounding_box()
            assert surface['height']>panel['height']*.6
            assert panel['y']>=70 and panel['y']+panel['height']<=screen['height']
            await page.screenshot(path=str(config.ROOT/f'.runtime/direct-control-{screen["width"]}.png'))
            await page.get_by_role('button',name='Return to chat',exact=True).click()
            await expect(page.locator('#browser-view')).to_be_hidden()
            await expect(page.get_by_role('button',name='Open browser',exact=True)).to_be_visible()
            await page.reload()
            await expect(page.locator('#browser-view')).to_be_hidden()
            await expect(page.get_by_role('button',name='Open browser',exact=True)).to_be_visible()
            await page.get_by_role('button',name='Open browser',exact=True).click()
            await expect(page.get_by_role('button',name='Return to chat',exact=True)).to_be_visible()
            await expect(field).to_have_value('Alex')
            await page.get_by_role('button',name='Return to chat',exact=True).click()
            await expect(page.locator('#browser-view')).to_be_hidden()
            assert database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='completed'
            assert database.one('SELECT COUNT(*) n FROM run_usage')['n']==0
            assert 'Alex' not in str(database.rows('SELECT content FROM messages'))
            await expect(field).to_have_value('Alex')
            # Exercise the entire native login click/POST path through the
            # streamed panel, using only intercepted local fixture requests.
            await page.get_by_role('button',name='Open browser',exact=True).click()
            await expect(page.get_by_role('button',name='Return to chat',exact=True)).to_be_visible()
            await page.wait_for_function('()=>!!pendingInput?.takeover')
            login_requests=[]
            async def login_response(route):
                login_requests.append(route.request.method)
                await route.fulfill(status=200,content_type='text/html',body='<h1>Signed in</h1>')
            await website.route('https://forms.example/session',login_response)
            await website.set_content('<form action="https://forms.example/session" method=post><label>User ID<input id=login-user name=user required autofocus></label><label>Password<input id=login-password name=password type=password required></label><button id=login-submit type=submit>Log In</button></form>')
            await website.locator('#login-user').focus()
            await page.evaluate('()=>document.activeElement.blur()')
            await page.keyboard.type('local-user');await expect(website.locator('#login-user')).to_have_value('local-user')
            await page.keyboard.press('Tab');await page.keyboard.type('local-test-password')
            await expect(website.locator('#login-password')).to_have_value('local-test-password')
            pending=service.active(uid)
            diagnostics=await host.login_diagnostics(uid,rid,pending['approval_id'])
            assert diagnostics['frames'][0]['buttons'][0]['login']
            assert not diagnostics['frames'][0]['buttons'][0]['disabled']
            assert all(f['filled'] for f in diagnostics['frames'][0]['fields'])
            assert 'local-test-password' not in str(diagnostics) and 'local-user' not in str(diagnostics)
            submit=await website.locator('#login-submit').bounding_box();imagebox=await frame.bounding_box()
            site_size=await website.evaluate('({width:innerWidth,height:innerHeight})')
            scale=min(imagebox['width']/site_size['width'],imagebox['height']/site_size['height'])
            login_point={'x':(imagebox['width']-site_size['width']*scale)/2+(submit['x']+submit['width']/2)*scale,'y':(imagebox['height']-site_size['height']*scale)/2+(submit['y']+submit['height']/2)*scale}
            if screen['width']<600:await page.touchscreen.tap(imagebox['x']+login_point['x'],imagebox['y']+login_point['y'])
            else:await frame.click(position=login_point)
            await expect(website.get_by_role('heading',name='Signed in',exact=True)).to_be_visible()
            assert login_requests==['POST']
            diagnostics=await host.login_diagnostics(uid,rid,pending['approval_id'])
            assert any(e['method']=='POST' and e.get('status')==200 for e in diagnostics['network'])
            assert 'local-test-password' not in str(database.rows('SELECT * FROM messages'))+str(database.rows('SELECT * FROM events'))
            await page.get_by_role('button',name='Return to chat',exact=True).click()
            await expect(page.get_by_role('button',name='Open browser',exact=True)).to_be_visible()
            await expect(page.locator('#input')).to_be_enabled()
            await page.locator('#input').fill('hello');await page.locator('#input').press('Enter')
            await expect(page.locator('#messages')).to_contain_text('Hello! How can I help?')
            await expect(page.get_by_role('button',name='Open browser',exact=True)).to_have_count(0)
            await expect(page.locator('#browser-view')).to_be_hidden()
            await page.reload()
            await expect(page.locator('#messages')).to_contain_text('Hello! How can I help?')
            await expect(page.get_by_role('button',name='Open browser',exact=True)).to_have_count(0)
            await expect(page.locator('#browser-view')).to_be_hidden()
            await browser.close()
    finally:server.should_exit=True;await running;await service.close();await host.close();await engine.client.close()



@pytest.mark.parametrize('manual',[False,True])
async def test_explicit_cancel_closes_completed_or_manual_browser_and_cannot_reopen(database,users,manual):
    from personal_agent.browser_intent import browser_preview_run
    uid=users[0]['id'];service=service_for(database,[AIMessage(content='Must not run')])
    host=SimpleNamespace(view_info=lambda *a:{'available':False},open_takeover=AsyncMock(),release_control=AsyncMock(),
        close_user=AsyncMock(),freeze_view=AsyncMock(),preview_locks={},run_for_user={})
    service.engine.browsers=host;sid,rid=completed_browser(database,uid);host.run_for_user[uid]=rid
    try:
        if manual:await service.take_browser_control(uid,rid)
        await service.cancel(uid,rid)
        host.close_user.assert_awaited_with(uid)
        assert service.active(uid) is None
        assert database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']==('cancelled' if manual else 'completed')
        assert browser_preview_run(database,uid,sid) is None
        assert database.one("SELECT id FROM events WHERE run_id=? AND kind='browser_closed'",(rid,))
        with pytest.raises(ValueError,match='closed'):await service.take_browser_control(uid,rid)
        count=host.close_user.await_count;await service.cancel(uid,rid)
        assert host.close_user.await_count==count
    finally:await service.close();await service.engine.client.close()


async def test_cancel_old_browser_cannot_close_another_users_or_newer_session(database,users):
    uid=users[0]['id'];service=service_for(database,[AIMessage(content='Must not run')])
    host=SimpleNamespace(close_user=AsyncMock(),run_for_user={uid:'newer'})
    service.engine.browsers=host;sid,rid=completed_browser(database,uid)
    try:
        with pytest.raises(LookupError):await service.cancel(users[1]['id'],rid)
        with pytest.raises(ValueError,match='newer browser'):await service.cancel(uid,rid)
        host.close_user.assert_not_awaited()
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='browser_context_retired'",(rid,))
    finally:await service.close();await service.engine.client.close()


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real cancellation and browser shutdown')
@pytest.mark.parametrize('browser_mode',['headless','visible'])
async def test_header_cancel_closes_manual_browser_and_stays_closed_after_reload(database,users,credentials,monkeypatch,browser_mode):
    import uvicorn
    from playwright.async_api import async_playwright,expect
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr('personal_agent.browser_host.public_url',AsyncMock())
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    uid=users[0]['id'];service=service_for(database,[AIMessage(content='Must not run')])
    host=BrowserHost(database,headless=browser_mode=='headless');service.engine.browsers=host;service.engine.browser_tasks.browsers=host
    sid,rid=completed_browser(database,uid)
    mid=database.message(uid,sid,'user','Open the example application')
    stamp=database.one('SELECT created FROM runs WHERE id=?',(rid,))['created']
    database.execute('UPDATE messages SET created=? WHERE id=?',(stamp-.1,mid))
    database.execute('UPDATE users SET must_change=0 WHERE id=?',(uid,))
    host.set_scope(uid,rid,['forms.example']);host.run_for_user[uid]=rid
    context=await host.context(uid)
    await context.route('https://forms.example/**',lambda route:route.fulfill(content_type='text/html',body='<h1>Application</h1><label>Name<input id=name></label>'))
    website=context.pages[0];await website.goto('https://forms.example/application')
    await context.add_cookies([{'name':'fixture-session','value':'retained','url':'https://forms.example','expires':2000000000}])
    host.set_view(uid,rid,{'url':website.url,'title':'Application'})
    native=host.native_browsers.get(uid);attached=host.attached_browsers.get(uid);profile=host.profile_path(uid)
    server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():task.result()
            await asyncio.sleep(.01)
        async with async_playwright() as pw:
            ui=await pw.chromium.launch(headless=True,chromium_sandbox=True)
            page=await ui.new_page(viewport={'width':1400,'height':1000});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            await page.goto(config.ORIGIN);await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
            await expect(page.locator('#browser-view')).to_be_visible()
            take=page.locator('#browser-take-control');cancel=page.locator('#browser-cancel-task')
            await expect(take).to_be_visible();await expect(cancel).to_be_visible()
            assert await take.evaluate('(el)=>el.nextElementSibling.id')=='browser-cancel-task'
            await take.click();await page.wait_for_function('()=>pendingInput?.control?.connected')
            await expect(cancel).to_be_visible();await expect(take).to_be_hidden()
            await page.locator('#input').fill('Keep this draft')
            async with page.expect_response(lambda response:response.url.endswith('/runs/'+rid+'/cancel') and response.request.method=='POST') as cancelled:
                await cancel.click()
            assert (await cancelled.value).status==200
            await expect(page.locator('#browser-view')).to_be_hidden()
            async with asyncio.timeout(15):
                while uid in host.contexts:await asyncio.sleep(.05)
            assert service.active(uid) is None
            if native:assert native.process.returncode is not None and not attached.is_connected()
            else:assert website.is_closed()
            assert profile.is_dir() and (profile/'Default'/'Cookies').is_file()
            await expect(page.locator('#input')).to_have_value('Keep this draft')
            state=await page.evaluate('r=>fetch("/api/runs/"+r+"/browser").then(r=>r.json())',rid)
            assert not state['available'] and not state['can_take_control']
            await page.reload();await expect(page.locator('#messages')).to_contain_text('Browser task cancelled')
            await expect(page.locator('#browser-view')).to_be_hidden()
            assert not errors
            await ui.close()
    finally:
        server.should_exit=True;await task;await host.close();await service.close();await service.engine.client.close();sock.close()
