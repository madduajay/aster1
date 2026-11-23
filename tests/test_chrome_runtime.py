from pathlib import Path
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from personal_agent import chrome_runtime as module


def test_minimal_launch_keeps_native_storage_and_display(monkeypatch,tmp_path):
    monkeypatch.setattr(module,'chrome_executable',lambda:Path('/installed/Chrome'))
    args=module.chrome_arguments(tmp_path)
    assert args==['/installed/Chrome','--user-data-dir='+str(tmp_path),'--profile-directory=Default',
        '--disable-sync','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','about:blank']


def test_native_entry_is_explicit_and_never_contains_session_credentials(monkeypatch,tmp_path):
    monkeypatch.setattr(module,'chrome_executable',lambda:Path('/installed/Chrome'))
    assert module.chrome_arguments(tmp_path,initial_url='https://example.com/login')[-1]=='https://example.com/login'
    for url in ['https://example.com/login?token=private','https://user:private@example.com/login','javascript:alert(1)']:
        with pytest.raises(ValueError):module.chrome_arguments(tmp_path,initial_url=url)


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Installed Chrome native startup check')
async def test_native_entry_page_loads_before_playwright_attaches(tmp_path):
    from playwright.async_api import async_playwright
    received=asyncio.Event()
    async def serve(reader,writer):
        try:
            request=await reader.readuntil(b'\r\n\r\n')
            if request.startswith(b'GET /login '):received.set()
            body=b'<h1>Native entry fixture</h1>'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
            await writer.drain()
        finally:writer.close()
    server=await asyncio.start_server(serve,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1];profile=tmp_path/'profile';profile.mkdir();(profile/'First Run').touch()
    native=None;browser=None
    async with async_playwright() as pw:
        try:
            native=await module.launch_visible_chrome(profile,initial_url=f'http://127.0.0.1:{port}/login')
            assert received.is_set(),'Chrome did not open the entry page before attachment'
            browser=await pw.chromium.connect_over_cdp(native.endpoint)
            assert any(page.url.endswith('/login') for page in browser.contexts[0].pages)
        finally:
            if native:await native.close(browser)
            server.close();await server.wait_closed()


async def test_never_launch_over_an_in_use_profile(monkeypatch,tmp_path):
    (tmp_path/'SingletonLock').symlink_to('host-123')
    launch=AsyncMock();monkeypatch.setattr(module.asyncio,'create_subprocess_exec',launch)
    with pytest.raises(ValueError,match='already in use'):await module.launch_visible_chrome(tmp_path)
    launch.assert_not_awaited()


async def test_reopening_waits_for_new_debugging_endpoint_not_recent_stale_file(monkeypatch,tmp_path):
    port=tmp_path/'DevToolsActivePort';port.write_text('1234\n/devtools/browser/old\n')
    process=SimpleNamespace(returncode=None,pid=123,wait=AsyncMock(return_value=0),terminate=lambda:None)
    monkeypatch.setattr(module,'chrome_executable',lambda:Path('/installed/Chrome'))
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',AsyncMock(return_value=process))
    async def publish():
        await asyncio.sleep(.08);port.write_text('5678\n/devtools/browser/new\n')
    update=asyncio.create_task(publish())
    native=await module.launch_visible_chrome(tmp_path)
    await update
    assert native.endpoint=='ws://127.0.0.1:5678/devtools/browser/new'
    process.returncode=0


async def test_close_flushes_owned_chrome_and_does_not_archive_another_process_lock(tmp_path):
    process=SimpleNamespace(returncode=None,pid=123)
    async def finished():process.returncode=0;return 0
    process.wait=AsyncMock(side_effect=finished)
    client=SimpleNamespace(send=AsyncMock())
    browser=SimpleNamespace(is_connected=lambda:True,new_browser_cdp_session=AsyncMock(return_value=client))
    lock=tmp_path/'SingletonLock';lock.symlink_to('host-456')
    await module.NativeChrome(process,profile=tmp_path).close(browser)
    client.send.assert_awaited_once_with('Browser.close')
    assert lock.is_symlink()


async def test_only_stale_links_from_our_exited_process_are_archived(tmp_path):
    for name in ['SingletonCookie','SingletonSocket','SingletonLock']:(tmp_path/name).symlink_to('host-123')
    native=module.NativeChrome(SimpleNamespace(returncode=0,pid=123),profile=tmp_path)
    await native.close()
    assert not (tmp_path/'SingletonLock').is_symlink()
    assert len(list((tmp_path.parent/'.closed-chrome-locks').glob('123-*/SingletonLock')))==1
