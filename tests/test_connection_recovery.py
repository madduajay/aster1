import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_agent.browser_connection import ConnectionRecovery, FreshObservationRequired, connection_error
from personal_agent import browser_use_worker as worker


def fake_browser():
    return SimpleNamespace(is_cdp_connected=False,agent_focus_target_id='owned-tab',
        _reconnecting=False,_reconnect_pending=False,_reconnect_event=asyncio.Event(),
        _connection_lock=asyncio.Lock(),_cached_browser_state_summary='old',
        update_cached_selector_map=lambda value:None,_dom_watchdog=None)


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
