import asyncio,json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from personal_agent import config
from personal_agent.browser_control import BrowserControl,ControlCommand
from personal_agent.browser_host import BrowserHost
from conftest import authenticate


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
              {'id':3,'operation':'click','x':30,'y':40,'width':640,'height':600}]
    socket=SimpleNamespace(receive_text=AsyncMock(side_effect=[*[json.dumps(m) for m in messages],WebSocketDisconnect()]),send_json=AsyncMock())
    host=SimpleNamespace(pending_manual=lambda *a:None,preview_locks={},views={('user','run'):{'captured':1}},
        manual_action=AsyncMock(),input_focus=AsyncMock(return_value={'editable':True}))
    control=BrowserControl(host,socket,'user','run','input',lambda:True)
    control.page=SimpleNamespace(viewport_size={'width':1280,'height':850})
    async def resize(size):control.page.viewport_size=size
    control.page.set_viewport_size=AsyncMock(side_effect=resize)
    with pytest.raises(WebSocketDisconnect):await control.commands()
    control.page.set_viewport_size.assert_awaited_once_with({'width':640,'height':600})
    assert host.manual_action.await_count==1
    replies=[call.args[0] for call in socket.send_json.call_args_list]
    assert replies[0]['width']==640 and replies[1]['error'] and 'error' not in replies[2]


async def test_resume_waits_for_control_to_close_and_restore_viewport():
    host=BrowserHost(SimpleNamespace(execute=lambda *a:None))
    control=SimpleNamespace(run='run',stop=asyncio.Event(),closed=asyncio.Event(),restore_failed=False)
    host.controls['user']=control
    release=asyncio.create_task(host.release_control('user','run'))
    await control.stop.wait()
    assert not release.done()
    control.closed.set();await release


async def test_cancelled_controller_releases_held_mouse_off_page():
    control=BrowserControl(None,None,'user','run','input',lambda:True)
    control.page=SimpleNamespace(is_closed=lambda:False,mouse=SimpleNamespace(move=AsyncMock(),up=AsyncMock()))
    control.pointer_down=True
    await control.cancel_pointer();await control.cancel_pointer()
    control.page.mouse.move.assert_awaited_once_with(-1,-1)
    control.page.mouse.up.assert_awaited_once()
    assert not control.pointer_down
