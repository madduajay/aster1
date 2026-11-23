import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent.browser_connection import ConnectionRecovery, FreshObservationRequired, connection_error, manual_handoff
from personal_agent import browser_use_worker as worker
from personal_agent import config
from personal_agent.browser_host import BrowserHost


def fake_browser():
    return SimpleNamespace(is_cdp_connected=False,agent_focus_target_id='owned-tab',
        _reconnecting=False,_reconnect_pending=False,_reconnect_event=asyncio.Event(),
        _connection_lock=asyncio.Lock(),_cached_browser_state_summary='old',
        update_cached_selector_map=lambda value:None,_dom_watchdog=None)


async def test_switch_observation_reads_destination_metadata_without_page_script():
    info=AsyncMock(side_effect=[{'targetInfo':{'url':'https://example.com/benefits'}},
                               {'targetInfo':{'url':'https://previous.example/login'}}])
    cdp=SimpleNamespace(target_id='old-tab',cdp_client=SimpleNamespace(send=SimpleNamespace(
        Target=SimpleNamespace(getTargetInfo=info))))
    browser=SimpleNamespace(get_or_create_cdp_session=AsyncMock(return_value=cdp),
                            get_target_id_from_tab_id=AsyncMock(return_value='new-tab'))
    result=await worker._observe(browser,{'_switch_tab':'-tab'})
    assert result=={'url':'https://previous.example/login','target_id':'old-tab',
                    'switch_target':{'target_id':'new-tab','url':'https://example.com/benefits'}}
    assert info.call_args_list[0].kwargs=={'params':{'targetId':'new-tab'}}


@pytest.mark.parametrize('detach',[False,True])
async def test_manual_handoff_disconnects_before_user_input_and_returns_to_same_tab(detach):
    browser=fake_browser();calls=[];browser.browser_profile=SimpleNamespace(keep_alive=True)
    browser._transport_recovery=ConnectionRecovery()
    async def stop():calls.append('stop')
    async def start():calls.append('start')
    async def receive():calls.append('input');return {'decision':'allow'}
    browser.stop=stop;browser.start=start
    browser.cdp_client=SimpleNamespace(send=SimpleNamespace(Target=SimpleNamespace(getTargets=AsyncMock(return_value={'targetInfos':[{'targetId':'owned-tab','type':'page'}]}))))
    browser.get_or_create_cdp_session=AsyncMock()
    assert await manual_handoff(browser,receive,lambda:calls.append('publish'),detach=detach)=={'decision':'allow'}
    assert calls==(['stop','publish','input','start'] if detach else ['publish','input'])
    assert browser._transport_recovery.generation==int(detach)
    if detach:browser.get_or_create_cdp_session.assert_awaited_once_with(target_id='owned-tab',focus=True)


async def test_cancelled_manual_handoff_does_not_reconnect_or_choose_another_tab():
    browser=fake_browser();browser.browser_profile=SimpleNamespace(keep_alive=True)
    browser.stop=AsyncMock();browser.start=AsyncMock()
    await manual_handoff(browser,AsyncMock(return_value={'decision':'cancel'}),lambda:None,detach=True)
    browser.stop.assert_awaited_once();browser.start.assert_not_awaited()


async def test_connected_manual_pause_waits_without_stopping_or_reconnecting_and_refreshes_on_release():
    browser=fake_browser();browser._transport_recovery=ConnectionRecovery()
    browser.stop=AsyncMock();browser.start=AsyncMock();published=asyncio.Event();reply=asyncio.Future()
    async def receive():return await reply
    task=asyncio.create_task(manual_handoff(browser,receive,published.set,refresh_on_release=True))
    await published.wait()
    assert not task.done() and browser._cached_browser_state_summary=='old'
    browser.stop.assert_not_awaited();browser.start.assert_not_awaited()
    reply.set_result({'decision':'allow'});await task
    assert browser._cached_browser_state_summary is None and browser._transport_recovery.generation==1
    browser.stop.assert_not_awaited();browser.start.assert_not_awaited()


async def test_manual_handoff_rejects_nonpersistent_browser_before_stopping():
    browser=fake_browser();browser.browser_profile=SimpleNamespace(keep_alive=False);browser.stop=AsyncMock()
    with pytest.raises(RuntimeError):await manual_handoff(browser,AsyncMock(),lambda:None,detach=True)
    browser.stop.assert_not_awaited()


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Installed Chrome and isolated Browser Use runtime')
@pytest.mark.parametrize('mode',['headless','visible'])
async def test_manual_handoff_preserves_window_page_fields_and_viewport(database,users,monkeypatch,mode):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr('personal_agent.browser_host.public_url',AsyncMock())
    uid=users[0]['id'];sid=database.new_session(uid);rid='manual-attachment-check'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'input',0)",(rid,uid,sid))
    host=BrowserHost(database,headless=mode=='headless');host.run_for_user[uid]=rid;host.set_scope(uid,rid,['handoff.fixture.test'])
    child=None
    program=r'''
import asyncio,json,logging,sys
sys.path.insert(0,sys.argv[2])
from browser_use import Browser
from browser_connection import resilient_browser,manual_handoff
logging.disable(logging.CRITICAL)
async def main():
 mode=sys.argv[3]
 browser=resilient_browser(Browser)(cdp_url=sys.argv[1],keep_alive=True,enable_default_extensions=False,highlight_elements=False,permissions=[])
 try:
  await browser.start()
  target=browser.agent_focus_target_id
  async def receive():
   value=await asyncio.to_thread(sys.stdin.readline)
   return json.loads(value)
  def published():
   assert browser.is_cdp_connected==(mode=='visible')
   print('CHECK:paused',flush=True)
  await manual_handoff(browser,receive,published,detach=mode!='visible',refresh_on_release=mode=='visible')
  assert browser.agent_focus_target_id==target
  print('CHECK:resumed',flush=True)
 finally:await browser.stop()
asyncio.run(main())
'''
    async def checkpoint(expected):
        async with asyncio.timeout(35):
            while True:
                line=await child.stdout.readline()
                if not line:raise AssertionError('Browser Use check exited before '+expected)
                if line.startswith(b'CHECK:'):
                    assert line.decode().strip()=='CHECK:'+expected
                    return
    try:
        ctx=await host.context(uid)
        await ctx.route('https://handoff.fixture.test/**',lambda route:route.fulfill(content_type='text/html',body='<input id=name><input id=secret type=password><script>window.resizes=0;addEventListener("resize",()=>resizes++)</script>'))
        page=ctx.pages[0];await page.goto('https://handoff.fixture.test/')
        size=page.viewport_size;profile=host.profile_path(uid)
        child=await asyncio.create_subprocess_exec(str(config.ROOT/'.runtime/browser-use-venv/bin/python'),'-c',program,await host.cdp_endpoint(uid),str(config.ROOT/'personal_agent'),mode,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ,'ANONYMIZED_TELEMETRY':'false','BROWSER_USE_LOGGING_LEVEL':'critical','BROWSER_USE_CONFIG_DIR':str(config.RUNTIME/'bu-config')})
        await checkpoint('paused')
        assert not page.is_closed() and host.contexts[uid] is ctx
        await page.evaluate('window.resizes=0')
        await page.locator('#name').fill('fixture user');await page.locator('#secret').fill('fixture secret')
        child.stdin.write(b'{"decision":"allow"}\n');await child.stdin.drain()
        await checkpoint('resumed');await asyncio.wait_for(child.wait(),15)
        assert child.returncode==0
        assert host.profile_path(uid)==profile and host.contexts[uid] is ctx
        assert ctx.pages==[page] and page.viewport_size==size
        assert await page.locator('#name').input_value()=='fixture user'
        assert await page.locator('#secret').input_value()=='fixture secret'
        assert await page.evaluate('window.resizes')==0
    finally:
        if child and child.returncode is None:child.terminate();await child.wait()
        await host.close()




async def test_concurrent_reconnects_replace_transport_once_and_keep_tab(monkeypatch):
    recovery=ConnectionRecovery();browser=fake_browser();replacements=[]
    async def probe(browser):
        if not browser.is_cdp_connected:raise ConnectionError('closed')
    async def reconnect():
        replacements.append(1);await asyncio.sleep(.02);browser.is_cdp_connected=True
    monkeypatch.setattr(recovery,'probe',probe)
    await asyncio.gather(*(recovery.restore(browser,reconnect) for _ in range(5)))
    assert len(replacements)==1 and recovery.generation==1
    assert recovery.last_target=='owned-tab' and browser._cached_browser_state_summary is None
    assert not browser._reconnecting and browser._reconnect_event.is_set()


async def test_failed_first_reconnect_retries_within_bound_and_cancellation_propagates(monkeypatch):
    recovery=ConnectionRecovery();browser=fake_browser();attempts=[]
    async def probe(browser):
        if not browser.is_cdp_connected:raise ConnectionError('closed')
    async def reconnect():
        attempts.append(1)
        if len(attempts)==1:raise RuntimeError('Client is not started')
        browser.is_cdp_connected=True
    monkeypatch.setattr(recovery,'probe',probe)
    await recovery.restore(browser,reconnect)
    assert len(attempts)==2
    browser.is_cdp_connected=False
    async def cancel():raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):await recovery.restore(browser,cancel)
    assert not recovery.lock.locked() and not browser._reconnecting


async def test_closed_original_tab_never_substitutes_another_tab():
    recovery=ConnectionRecovery();recovery.last_target='original'
    browser=fake_browser();browser.is_cdp_connected=True
    browser.cdp_client=SimpleNamespace(send=SimpleNamespace(Target=SimpleNamespace(getTargets=AsyncMock(
        return_value={'targetInfos':[{'targetId':'another','type':'page'}]}))))
    with pytest.raises(ConnectionError,match='tab has closed'):await recovery.probe(browser)


async def test_failed_recovery_budget_is_shared_by_automatic_and_manual_callers(monkeypatch):
    recovery=ConnectionRecovery();browser=fake_browser();attempts=[]
    monkeypatch.setattr(recovery,'probe',AsyncMock(side_effect=ConnectionError('closed')))
    async def reconnect():
        attempts.append(1)
        raise ConnectionError('Endpoint unavailable')
    results=await asyncio.gather(*(recovery.restore(browser,reconnect) for _ in range(4)),return_exceptions=True)
    assert len(attempts)==2 and all(isinstance(result,ConnectionError) for result in results)
    assert recovery.failed_attempts==2 and not recovery.lock.locked()


@pytest.mark.parametrize('message',['No valid agent focus available','CDP client not initialized','Client is not started'])
async def test_reconnected_click_requires_fresh_target_and_never_replays(monkeypatch,message):
    reconnect=AsyncMock();browser=SimpleNamespace(reconnect=reconnect)
    calls=[]
    async def observe(browser,params=None):
        calls.append(params)
        if len(calls)==1:raise RuntimeError(message)
        return {'target_id':'same-tab','url':'https://example.com/item'}
    monkeypatch.setattr(worker,'_observe',observe);monkeypatch.setattr(worker,'emit',lambda e:None)
    with pytest.raises(worker.FreshObservationRequired):await worker.observe(browser,{'index':123})
    assert reconnect.await_count==1 and calls==[{'index':123},None]


async def test_stale_selector_does_not_reconnect(monkeypatch):
    browser=SimpleNamespace(reconnect=AsyncMock())
    monkeypatch.setattr(worker,'_observe',AsyncMock(side_effect=ValueError('Selector must identify exactly one observed element')))
    with pytest.raises(ValueError):await worker.observe(browser,{'selector':'#missing'})
    browser.reconnect.assert_not_called()


async def test_automatic_reconnect_during_observation_invalidates_target(monkeypatch):
    recovery=ConnectionRecovery();browser=SimpleNamespace(_transport_recovery=recovery)
    async def observe(browser,params=None):
        recovery.generation+=1
        return {'target_id':'owned-tab'}
    monkeypatch.setattr(worker,'_observe',observe)
    with pytest.raises(worker.FreshObservationRequired):await worker.observe(browser,{'selector':'#buy'})
    assert recovery.last_target=='owned-tab'


def test_focus_value_error_is_connection_error_but_stale_control_is_not():
    error=ValueError('focus failed');error.browser_stage='browser_connection'
    assert connection_error(error)
    assert not connection_error(ValueError('Target detached during resolve_dom_node'))


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Browser Use tab switching')
async def test_real_switch_from_previous_site_reads_requested_page(database,users,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    host=BrowserHost(database,headless=True)
    child=None
    program=r'''
import asyncio,logging,sys
sys.path.insert(0,sys.argv[2])
from browser_use import Browser
from browser_use.browser.events import SwitchTabEvent
from browser_use_worker import _observe
logging.disable(logging.CRITICAL)
async def main():
 browser=Browser(cdp_url=sys.argv[1],keep_alive=True,enable_default_extensions=False,highlight_elements=False,permissions=[])
 try:
  await browser.start()
  tabs=await browser.get_tabs()
  old=next(t for t in tabs if 'previous.fixture.test' in t.url)
  new=next(t for t in tabs if 'benefits.fixture.test' in t.url)
  event=browser.event_bus.dispatch(SwitchTabEvent(target_id=old.target_id));await event;await event.event_result(raise_if_any=True)
  state=await _observe(browser,{'_switch_tab':new.target_id[-4:]})
  assert state['url']=='https://previous.fixture.test/'
  assert state['switch_target']=={'target_id':new.target_id,'url':'https://benefits.fixture.test/'}
  assert 'text' not in state
  event=browser.event_bus.dispatch(SwitchTabEvent(target_id=new.target_id));await event;await event.event_result(raise_if_any=True)
  state=await _observe(browser)
  assert state['url']=='https://benefits.fixture.test/' and 'Fixture card benefits' in state['text']
  print('CHECK:destination resolved, switched and read benefits',flush=True)
 finally:await browser.stop()
asyncio.run(main())
'''
    try:
        uid=users[0]['id'];context=await host.context(uid)
        await context.route('https://*.fixture.test/**',lambda route:route.fulfill(content_type='text/html',body='<main><h1>Fixture card benefits</h1><p>Travel protection and rewards.</p></main>'))
        await context.pages[0].goto('https://previous.fixture.test/')
        page=await context.new_page();await page.goto('https://benefits.fixture.test/')
        child=await asyncio.create_subprocess_exec(str(config.ROOT/'.runtime/browser-use-venv/bin/python'),'-c',program,await host.cdp_endpoint(uid),str(config.ROOT/'personal_agent'),
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
            env={**os.environ,'BROWSER_USE_LOGGING_LEVEL':'critical','ANONYMIZED_TELEMETRY':'false','BROWSER_USE_CONFIG_DIR':str(config.RUNTIME/'switch-config')})
        out,err=await asyncio.wait_for(child.communicate(),45)
        assert child.returncode==0,err.decode()[-1500:]
        assert b'CHECK:destination resolved, switched and read benefits' in out
    finally:
        if child and child.returncode is None:child.terminate();await child.wait()
        await host.close()
