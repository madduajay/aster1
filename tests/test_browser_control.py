import asyncio,json,os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from personal_agent import config
from personal_agent.browser_control import BrowserControl,ControlCommand
from personal_agent.browser_host import BrowserHost,network_error_code
from conftest import authenticate


def test_network_error_codes_preserve_protocol_digits_without_echoing_urls():
    assert network_error_code('net::ERR_HTTP2_PROTOCOL_ERROR at https://example.com/?token=secret')=='net::ERR_HTTP2_PROTOCOL_ERROR'
    assert network_error_code('net::ERR_FAILED')=='net::ERR_FAILED'
    assert network_error_code('https://example.com/?password=secret')=='request_failed'


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real embedded-document network check')
@pytest.mark.parametrize('browser_mode',['headless','visible'])
async def test_login_support_frame_post_loads_without_granting_agent_access(database,users,monkeypatch,browser_mode):
    from playwright.async_api import Route,expect
    from personal_agent.browser_dom import FOCUS,inspect_dom
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setenv('PERSONAL_AGENT_BROWSER_POLICY','poc')
    monkeypatch.setattr('personal_agent.browser_host.public_url',AsyncMock())
    original=Route.continue_
    async def fixture_network(route,**kwargs):
        if route.request.url.startswith('https://login.example/'):
            await route.fulfill(status=200,content_type='text/html',body='<iframe name=support></iframe><form id=bootstrap target=support action="https://support.example/bootstrap" method=post></form><script>document.querySelector("#bootstrap").submit()</script>')
        elif route.request.url.startswith('https://support.example/'):
            assert route.request.method=='POST'
            await route.fulfill(status=200,content_type='text/html',body='<h1 id=ready>Login support ready</h1>')
        else:await original(route,**kwargs)
    monkeypatch.setattr(Route,'continue_',fixture_network)
    uid=users[0]['id'];sid=database.new_session(uid);rid='embedded-login'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(rid,uid,sid))
    host=BrowserHost(database,headless=browser_mode=='headless');host.set_scope(uid,rid,['login.example']);host.run_for_user[uid]=rid
    try:
        context=await host.context(uid)
        await context.route('**/*',fixture_network)
        page=context.pages[0];await page.goto('https://login.example/')
        await expect(page.frame_locator('iframe').locator('#ready')).to_be_visible()
        assert host.scopes[(uid,rid)]==['login.example']
        observed=await inspect_dom(page,['login.example'],FOCUS)
        assert any(not frame['allowed'] and 'support.example' in frame['url'] for frame in observed)
        assert any(event['host']=='support.example' and event['method']=='POST' and event.get('status')==200 for event in host.network_events[uid])
    finally:await host.close()


def test_network_diagnostics_omit_credentials_headers_paths_and_queries():
    host=BrowserHost(SimpleNamespace(execute=lambda *a:None))
    host.run_for_user['user']='run'
    request=SimpleNamespace(resource_type='fetch',method='POST',url='https://private-user:private-password@auth.example/session?token=private-token',post_data='private-body')
    for _ in range(45):host.record_network('user',request,'response',status=403)
    records=list(host.network_events['user'])
    assert len(records)==40 and records[0]['host']=='auth.example' and records[0]['status']==403
    assert 'private-' not in json.dumps(records) and 'session' not in json.dumps(records)


def test_login_diagnostics_require_login_owner_and_current_pause(client,database,users,credentials):
    uid=users[0]['id'];sid=database.new_session(uid)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created,approval_id,approval) VALUES('diagnostics-run',?,?,'input',0,'current',?)",(uid,sid,json.dumps({'action':{'kind':'manual'}})))
    path='/api/runs/diagnostics-run/browser/diagnostics?input_id=stale'
    assert client.get(path).status_code==401
    authenticate(client,credentials,users[1]['username']);assert client.get(path).status_code==404
    client.post('/api/logout');authenticate(client,credentials)
    client.app.state.service.engine.browsers=BrowserHost(database)
    assert client.get(path).status_code==404
    host=client.app.state.service.engine.browsers
    host.run_for_user[uid]='diagnostics-run'
    request=SimpleNamespace(resource_type='fetch',method='POST',url='https://auth.example/login?credential=private-token')
    for _ in range(105):host.record_network(uid,request,'response',status=403)
    assert database.one("SELECT COUNT(*) n FROM events WHERE run_id='diagnostics-run' AND kind='browser_network'")['n']==100
    database.execute("UPDATE runs SET status='cancelled',approval=NULL,approval_id=NULL WHERE id='diagnostics-run'")
    # A new host has no in-memory events: terminal history must survive restart.
    client.app.state.service.engine.browsers=BrowserHost(database)
    result=client.get('/api/runs/diagnostics-run/browser/diagnostics')
    assert result.status_code==200 and not result.json()['live']
    assert result.json()['network_summary']['post_statuses']==[{'host':'auth.example','status':403}]
    assert len(result.json()['network'])==100 and not result.json()['frames']
    assert 'private-token' not in result.text and '/login' not in result.text


def test_live_control_requires_login_origin_owner_and_pending_input(client,database,users,credentials):
    url='wss://aster.localhost:55468/api/runs/control-run/browser/control?input_id=current-id'
    def denied(origin=config.ORIGIN):
        with pytest.raises(WebSocketDisconnect) as failure:
            with client.websocket_connect(url,headers={'origin':origin}):pass
        assert failure.value.code==4403
    denied()
    authenticate(client,credentials,'user01')
    sid=database.new_session(users[1]['id'])
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('control-run',?,?,'input',0)",(users[1]['id'],sid))
    denied();denied('https://untrusted.example')
    database.execute('UPDATE runs SET user_id=? WHERE id=?',(users[0]['id'],'control-run'))
    client.app.state.service.engine.browsers=BrowserHost(database)
    pending=json.dumps({'action':{'kind':'information'}})
    database.execute("UPDATE runs SET approval_id='different-id',approval=? WHERE id='control-run'",(pending,))
    denied()
    database.execute("UPDATE runs SET approval_id='current-id',status='completed' WHERE id='control-run'")
    denied()


async def test_live_input_is_ordered_once_and_validation_never_echoes_values():
    messages=[json.dumps({'id':1,'operation':'click','x':2,'y':3,'width':640,'height':600}),
        json.dumps({'id':1,'operation':'click','x':2,'y':3,'width':640,'height':600}),
        json.dumps({'id':2,'operation':'type','text':'sensitive-value'*200})]
    socket=SimpleNamespace(receive_text=AsyncMock(side_effect=[*messages,WebSocketDisconnect()]),send_json=AsyncMock())
    host=SimpleNamespace(pending_manual=lambda *a:None,manual_action=AsyncMock(),input_focus=AsyncMock(return_value={'editable':True,'input_mode':'text'}))
    control=BrowserControl(host,socket,'user','run','input',lambda:True)
    control.page=SimpleNamespace(viewport_size={'width':640,'height':600})
    with pytest.raises(WebSocketDisconnect):await control.commands()
    assert host.manual_action.await_count==1
    replies=[call.args[0] for call in socket.send_json.call_args_list]
    assert replies[0]['id']==1 and replies[1]['error'] and replies[2]['id']==2
    assert 'sensitive-value' not in json.dumps(replies)


async def test_control_revocation_rejects_the_next_command():
    socket=SimpleNamespace(receive_text=AsyncMock(return_value='{"id":1,"operation":"type","text":"not sent"}'))
    host=SimpleNamespace(pending_manual=lambda *a:None,manual_action=AsyncMock())
    control=BrowserControl(host,socket,'user','run','input',lambda:False)
    with pytest.raises(LookupError):await control.commands()
    host.manual_action.assert_not_called()


def test_control_exposes_only_bounded_browser_input():
    for payload in [{'id':1,'operation':'evaluate','text':'arbitrary code'},
                    {'id':1,'operation':'click','x':float('nan')},
                    {'id':1,'operation':'type','text':'x'*2001},
                    {'id':1,'operation':'resize','width':999999,'height':600},
                    {'id':1,'operation':'scroll','pointer_x':float('nan'),'pointer_y':2},
                    {'id':1,'operation':'resize','width':640,'height':0}]:
        with pytest.raises(ValueError):ControlCommand.model_validate(payload)


async def test_live_display_keeps_the_latest_frame_instead_of_dropping_final_input():
    import base64
    control=BrowserControl(None,None,'user','run','input',lambda:True)
    control.client=SimpleNamespace(send=AsyncMock())
    for number in range(3):
        await control.on_frame({'sessionId':number,'data':base64.b64encode(('frame'+str(number)).encode()).decode()})
    assert control.frames.qsize()==1 and await control.frames.get()==b'frame2'
    assert control.client.send.await_count==3


async def test_live_resize_preserves_page_and_rejects_clicks_on_old_geometry():
    messages=[{'id':1,'operation':'resize','width':640,'height':600},
              {'id':2,'operation':'click','x':30,'y':40,'width':1280,'height':850},
              {'id':3,'operation':'click','x':30,'y':40,'width':640,'height':600},
              {'id':4,'operation':'scroll','x':0,'y':100,'pointer_x':30,'pointer_y':40,'width':1280,'height':850},
              {'id':5,'operation':'scroll','x':0,'y':100,'pointer_x':30,'pointer_y':40,'width':640,'height':600}]
    socket=SimpleNamespace(receive_text=AsyncMock(side_effect=[*[json.dumps(m) for m in messages],WebSocketDisconnect()]),send_json=AsyncMock())
    host=SimpleNamespace(pending_manual=lambda *a:None,preview_locks={},views={('user','run'):{'captured':1}},
        manual_action=AsyncMock(),input_focus=AsyncMock(return_value={'editable':True}))
    control=BrowserControl(host,socket,'user','run','input',lambda:True)
    control.page=SimpleNamespace(viewport_size={'width':1280,'height':850})
    async def resize(size):control.page.viewport_size=size
    control.page.set_viewport_size=AsyncMock(side_effect=resize)
    with pytest.raises(WebSocketDisconnect):await control.commands()
    control.page.set_viewport_size.assert_awaited_once_with({'width':640,'height':600})
    assert host.manual_action.await_count==2
    assert host.manual_action.await_args_list[-1].kwargs=={'pointer_x':30,'pointer_y':40}
    replies=[call.args[0] for call in socket.send_json.call_args_list]
    assert replies[0]['width']==640 and replies[1]['error'] and 'error' not in replies[2]
    assert replies[3]['error'] and 'error' not in replies[4]


async def test_resume_waits_for_control_to_close_and_restore_viewport():
    host=BrowserHost(SimpleNamespace(execute=lambda *a:None))
    control=SimpleNamespace(run='run',stop=asyncio.Event(),closed=asyncio.Event(),restore_failed=False)
    host.controls['user']=control
    release=asyncio.create_task(host.release_control('user','run'))
    await control.stop.wait()
    assert not release.done()
    control.closed.set();await release


async def test_visible_sidebar_resize_leaves_the_website_and_capture_unchanged():
    host=SimpleNamespace(pending_manual=lambda *a:None)
    control=BrowserControl(host,None,'user','run','input',lambda:True);control.fit_only=True
    control.page=SimpleNamespace(viewport_size={'width':1280,'height':850},set_viewport_size=AsyncMock())
    control.client=SimpleNamespace(send=AsyncMock())
    await control.resize(390,600);await control.resize(900,700)
    assert control.viewport=={'width':1280,'height':850}
    control.page.set_viewport_size.assert_not_awaited();control.client.send.assert_not_awaited()


async def test_natural_window_dimensions_are_read_without_eval_or_emulation():
    host=SimpleNamespace(pending_manual=lambda *a:None)
    control=BrowserControl(host,None,'user','run','input',lambda:True);control.fit_only=True
    control.page=SimpleNamespace(viewport_size=None,set_viewport_size=AsyncMock())
    control.client=SimpleNamespace(send=AsyncMock(return_value={'cssLayoutViewport':{'clientWidth':1511.99999999,'clientHeight':859.99999999}}))
    await control.resize(390,600)
    assert control.viewport=={'width':1512,'height':860}
    control.client.send.assert_awaited_once_with('Page.getLayoutMetrics')
    control.page.set_viewport_size.assert_not_awaited()


async def test_cancelled_controller_releases_held_mouse_off_page():
    control=BrowserControl(None,None,'user','run','input',lambda:True)
    control.page=SimpleNamespace(is_closed=lambda:False,mouse=SimpleNamespace(move=AsyncMock(),up=AsyncMock()))
    control.pointer_down=True
    await control.cancel_pointer();await control.cancel_pointer()
    control.page.mouse.move.assert_awaited_once_with(-1,-1)
    control.page.mouse.up.assert_awaited_once()
    assert not control.pointer_down


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chrome streaming stability check')
@pytest.mark.parametrize('headless',[True,False])
async def test_regular_stream_updates_without_repeated_native_page_resizes(database,users,monkeypatch,headless):
    import hashlib
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr('personal_agent.browser_host.public_url',AsyncMock())
    uid=users[0]['id'];sid=database.new_session(uid);rid='stable-stream'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created,approval_id,approval) VALUES(?,?,?,'input',0,'input-id',?)",(rid,uid,sid,json.dumps({'action':{'kind':'manual'}})))
    host=BrowserHost(database,headless=headless);host.run_for_user[uid]=rid;host.set_scope(uid,rid,['stream.fixture.test'])
    frames=[];first_frame=asyncio.Event()
    async def frame(data):frames.append(hashlib.sha256(data).hexdigest());first_frame.set()
    async def receive():await asyncio.Event().wait()
    socket=SimpleNamespace(send_json=AsyncMock(),send_bytes=frame,receive_text=receive)
    control=BrowserControl(host,socket,uid,rid,'input-id',lambda:True,{'width':640,'height':600});job=None
    try:
        ctx=await host.context(uid)
        await ctx.route('https://stream.fixture.test/**',lambda r:r.fulfill(content_type='text/html',body='<div id=t></div><script>let n=0;setInterval(()=>document.querySelector("#t").textContent=++n,80)</script>'))
        page=ctx.pages[0];await page.goto('https://stream.fixture.test/');host.set_view(uid,rid,{'url':page.url})
        viewport_writes=AsyncMock(wraps=page.set_viewport_size);page.set_viewport_size=viewport_writes
        ordinary_screenshot=AsyncMock(wraps=page.screenshot);page.screenshot=ordinary_screenshot
        job=asyncio.create_task(control.run_control());await asyncio.wait_for(first_frame.wait(),5)
        await page.evaluate('window.resizeCount=0;addEventListener("resize",()=>resizeCount++)')
        await asyncio.sleep(1) # Observe repeated frame delivery after initial sizing.
        assert len(set(frames))>=2
        assert await page.evaluate('resizeCount')==0
        if not headless:
            await control.resize(390,700);await control.resize(900,600)
            host.views[(uid,rid)]['captured']=0
            assert await host.preview(uid,rid)
            assert await page.evaluate('resizeCount')==0
            ordinary_screenshot.assert_not_awaited()
            original=dict(control.viewport)
            window=await control.client.send('Browser.getWindowForTarget')
            await control.client.send('Browser.setWindowBounds',{'windowId':window['windowId'],'bounds':{'width':1100,'height':800}})
            for _ in range(50):
                if control.viewport!=original:break
                await asyncio.sleep(.05)
            assert control.viewport!=original
            assert any(c.args[0].get('type')=='viewport' for c in socket.send_json.await_args_list)
    finally:
        control.stop.set()
        if job:await asyncio.gather(job,return_exceptions=True)
        await host.close()
    if not headless:viewport_writes.assert_not_awaited()
