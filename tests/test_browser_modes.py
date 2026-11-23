import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent.browser_host import BrowserHost
from personal_agent import browser_host as browser_module
from conftest import authenticate


def test_retiring_preview_removes_working_metadata_without_closing_browser(database,users):
    uid,other=[u['id'] for u in users[:2]];sid=database.new_session(uid)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('old',?,?,'completed',0)",(uid,sid))
    host=BrowserHost(database);context=SimpleNamespace(close=AsyncMock())
    host.contexts[uid]=context;host.run_for_user[uid]='old'
    host.set_scope(uid,'old',['example.com']);host.set_view(uid,'old',{'url':'https://example.com/benefits'})
    host.views[(uid,'old')]['frame']=b'cached image'
    with pytest.raises(LookupError):host.retire_view(other,'old')
    assert (uid,'old') in host.views
    host.retire_view(uid,'old')
    assert (uid,'old') not in host.views and (uid,'old') not in host.scopes and uid not in host.run_for_user
    assert host.contexts[uid] is context
    context.close.assert_not_awaited()


@pytest.mark.parametrize('mode',['headless','visible'])
async def test_native_networking_is_permanent_across_users_runs_and_restarts(database,users,monkeypatch,mode):
    uid,other=[u['id'] for u in users[:2]]
    for user,rid in [(uid,'first'),(uid,'next'),(other,'other')]:
        sid=database.new_session(user)
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'completed',0)",(rid,user,sid))
    def context():return SimpleNamespace(pages=[SimpleNamespace(on=lambda *a:None)],route=AsyncMock(),on=lambda *a:None,close=AsyncMock())
    contexts=[context() for _ in range(3)]
    launch=AsyncMock(side_effect=contexts)
    connect=AsyncMock(side_effect=[SimpleNamespace(contexts=[ctx]) for ctx in contexts])
    native=AsyncMock(side_effect=[SimpleNamespace(endpoint='ws://127.0.0.1:1234/devtools/browser/test',process=SimpleNamespace(returncode=None)) for _ in contexts])
    monkeypatch.setattr(browser_module,'launch_visible_chrome',native)
    monkeypatch.setattr(browser_module,'async_playwright',lambda:SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch,connect_over_cdp=connect)))))
    host=BrowserHost(database,headless=mode=='headless')
    for user,rid in [(uid,'first'),(uid,'next'),(other,'other')]:
        host.run_for_user[user]=rid;host.set_scope(user,rid,['example.com']);await host.context(user)
    assert (connect if mode=='visible' else launch).await_count==2 # A later task keeps the same browser/profile.
    host=BrowserHost(database,headless=mode=='headless') # Normal application restart, no test flag.
    host.run_for_user[uid]='next';host.set_scope(uid,'next',['example.com']);await host.context(uid)
    assert (connect if mode=='visible' else launch).await_count==3
    assert (launch if mode=='visible' else connect).await_count==0
    for ctx in contexts:ctx.route.assert_not_awaited()
    for call in launch.await_args_list:
        assert call.kwargs['viewport']=={'width':1280,'height':850}
        assert call.kwargs['service_workers']=='block'
        assert call.kwargs['headless']==(mode=='headless')
    events=database.rows("SELECT payload FROM events WHERE kind='browser_network_configuration'")
    assert len(events)==3
    assert all(json.loads(e['payload'])=={'interception':False,'persistent':True,'mode':mode} for e in events)


async def test_modes_persist_per_user_and_keep_separate_profiles(database,users):
    a,b=[u['id'] for u in users[:2]]
    host=BrowserHost(database,headless=True)
    assert host.mode_info(a)['mode']=='headless'
    old_path=host.profile_path(a)
    host.close_user=AsyncMock()
    await host.set_mode(a,'visible')
    host.close_user.assert_awaited_once_with(a)
    assert host.profile_path(a)!=old_path
    assert host.mode_info(b)['mode']=='headless'
    restored=BrowserHost(database)
    assert restored.mode_info(a)['mode']=='visible'
    await restored.set_mode(a,'headless')
    assert restored.profile_path(a)==old_path
    assert host.mode_info(b)['mode']=='headless'
    with pytest.raises(ValueError):await host.set_mode(a,'arbitrary')
    with pytest.raises(LookupError):await host.set_mode('missing-user','visible')


@pytest.mark.parametrize('status',['running','resuming','input','approval'])
async def test_switch_never_interrupts_active_task(database,users,status):
    uid=users[0]['id'];sid=database.new_session(uid)
    database.execute('INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,?,0)',('busy',uid,sid,status))
    host=BrowserHost(database,headless=True);host.close_user=AsyncMock()
    with pytest.raises(ValueError,match='Finish or cancel'):await host.set_mode(uid,'visible')
    host.close_user.assert_not_awaited()
    assert host.mode_info(uid)['mode']=='headless'
    assert (await host.set_mode(uid,'headless'))['mode']=='headless'


def test_mode_api_authentication_scope_and_origin(client,database,users,credentials):
    host=BrowserHost(database,headless=True)
    client.app.state.service=SimpleNamespace(engine=SimpleNamespace(browsers=host),user_lock=lambda user:asyncio.Lock())
    path='/api/browser/mode'
    assert client.get(path).status_code==401
    assert client.post(path,json={'mode':'visible'}).status_code==401
    authenticate(client,credentials)
    assert client.get(path).json()['mode']=='headless'
    assert client.post(path,json={'mode':'visible'},headers={'origin':'https://untrusted.example'}).status_code==403
    assert client.post(path,json={'mode':'visible'}).json()['mode']=='visible'
    assert host.mode_info(users[1]['id'])['mode']=='headless'
    assert client.post(path,json={'mode':'no-such-mode'}).status_code==422
    uid=users[0]['id'];sid=database.new_session(uid)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('busy',?,?,'input',0)",(uid,sid))
    result=client.post(path,json={'mode':'headless'})
    assert result.status_code==409 and 'Finish or cancel' in result.text
