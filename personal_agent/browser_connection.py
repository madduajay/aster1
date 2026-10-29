"""One connection-recovery owner for manual and Browser Use automatic reconnects.

This adapter targets the pinned Browser Use runtime. It never navigates or
replays an action; the parent still owns the browser, approvals, and journal.
"""
import asyncio


class FreshObservationRequired(RuntimeError):
    """The transport recovered, but an old action target must be rediscovered."""


def connection_error(exc):
    if getattr(exc, 'browser_stage', None) == 'browser_connection':
        return True
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    message = str(exc).casefold()
    return type(exc).__name__.startswith('ConnectionClosed') or any(s in message for s in (
        'cdp client not initialized', 'root cdp client not initialized',
        'sessionmanager not initialized', 'client is not started',
        'no valid agent focus', 'websocket', 'session with given id not found',
    ))


class ConnectionRecovery:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_target = None
        self.generation = 0
        self.failed_attempts = 0

    async def probe(self, browser):
        """Check the socket AND the original page attachment, without navigation."""
        if not browser.is_cdp_connected:
            raise ConnectionError('Browser control connection is not open')
        desired = self.last_target or browser.agent_focus_target_id
        if not desired:
            raise ConnectionError('No previously observed browser tab is available')
        async with asyncio.timeout(3):
            result = await browser.cdp_client.send.Target.getTargets()
            if not any(t['targetId'] == desired and t['type'] == 'page' for t in result['targetInfos']):
                raise ConnectionError('The previously observed browser tab has closed; automatic tab substitution is disabled')
            client = await browser.get_or_create_cdp_session(target_id=desired, focus=True)
            await client.cdp_client.send.Runtime.evaluate(
                params={'expression': 'document.readyState', 'returnByValue': True},
                session_id=client.session_id)

    def invalidate_targets(self, browser):
        browser._cached_browser_state_summary = None
        browser.update_cached_selector_map({})
        if browser._dom_watchdog:
            browser._dom_watchdog.clear_cache()
        self.generation += 1

    async def restore(self, browser, reconnect):
        # Browser Use's WS callback and the application's observer both enter
        # here. A queued caller rechecks health rather than closing a new socket.
        async with self.lock:
            try:
                await self.probe(browser)
                if self.failed_attempts:
                    # A timed-out rebuild may finish attaching before the next
                    # caller probes it. Its old DOM references are still stale.
                    self.invalidate_targets(browser)
                self.failed_attempts = 0
                return
            except Exception:
                pass
            desired = self.last_target or browser.agent_focus_target_id
            if desired:
                self.last_target = desired
            if self.failed_attempts >= 2:
                raise ConnectionError('Browser reconnection attempts exhausted. Start a new browser task to retry.')
            previous_reconnecting = browser._reconnecting
            browser._reconnecting = True
            browser._reconnect_event.clear()
            try:
                while self.failed_attempts < 2:
                    self.failed_attempts += 1
                    try:
                        async with asyncio.timeout(15):
                            # Also serialize with the library's initial start().
                            async with browser._connection_lock:
                                await reconnect()
                            await self.probe(browser)
                        # DOM indices and Actor CDP attachments belong to the
                        # old transport. Never reuse them after reconnection.
                        self.invalidate_targets(browser)
                        self.failed_attempts = 0
                        browser._reconnect_pending = False
                        return
                    except Exception:
                        if self.failed_attempts >= 2:
                            raise
                        await asyncio.sleep(.25)
            finally:
                if not previous_reconnecting:
                    browser._reconnecting = False
                    browser._reconnect_event.set()


def resilient_browser(base):
    """Build the adapter only inside the isolated Browser Use environment."""
    from pydantic import PrivateAttr

    class ResilientBrowser(base):
        _transport_recovery: ConnectionRecovery = PrivateAttr(default_factory=ConnectionRecovery)

        async def reconnect(self):
            await self._transport_recovery.restore(self, super().reconnect)

    return ResilientBrowser
