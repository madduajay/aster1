"""Own a minimal Chrome process; Playwright attaches after native startup."""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

import httpx


def chrome_executable():
    for path in (Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
                 Path.home()/'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'):
        if path.is_file():return path
    raise ValueError('Install Google Chrome to use Visible mode')


def available_debug_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def chrome_arguments(profile,disable_http2=False,initial_url='about:blank',*,debug_port=None):
    if initial_url!='about:blank':
        p=urlsplit(initial_url)
        if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('Native startup requires a public entry URL without session parameters')
    debug_port=available_debug_port() if debug_port is None else debug_port
    if type(debug_port) is not int or not 1<=debug_port<=65535:raise ValueError('Invalid local debugging port')
    args=[str(chrome_executable()),'--user-data-dir='+str(profile),'--profile-directory=Default',
          '--disable-sync','--remote-debugging-address=127.0.0.1','--remote-debugging-port='+str(debug_port)]
    if disable_http2:args.append('--disable-http2')
    return args+[initial_url]


async def debugging_endpoint(port):
    async with httpx.AsyncClient(trust_env=False,timeout=.5) as client:
        response=await client.get(f'http://127.0.0.1:{port}/json/version')
        response.raise_for_status()
        endpoint=response.json().get('webSocketDebuggerUrl','')
    parsed=urlsplit(endpoint)
    if parsed.scheme!='ws' or parsed.hostname!='127.0.0.1' or parsed.port!=port or not parsed.path.startswith('/devtools/browser/') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Chrome returned an invalid local debugging endpoint')
    return endpoint


@dataclass
class NativeChrome:
    process: object
    endpoint: str=''
    profile: Path | None=None

    async def close(self,browser=None):
        if self.process.returncode is None:
            if browser is not None and browser.is_connected():
                try:
                    async with asyncio.timeout(5):
                        client=await browser.new_browser_cdp_session()
                        await client.send('Browser.close')
                except Exception:pass # Chrome may disconnect before acknowledging close.
            try:await asyncio.wait_for(self.process.wait(),10)
            except TimeoutError:
                if self.process.returncode is None:self.process.terminate()
                await asyncio.wait_for(self.process.wait(),5)
        if self.profile:
            lock=self.profile/'SingletonLock'
            if lock.is_symlink() and os.readlink(lock).rsplit('-',1)[-1]==str(self.process.pid):
                # SIGTERM can leave singleton links behind. Preserve them for
                # inspection only after our own process has actually exited.
                archive=self.profile.parent/'.closed-chrome-locks'/f'{self.process.pid}-{time.time_ns()}'
                archive.mkdir(parents=True,mode=0o700)
                for name in ('SingletonCookie','SingletonSocket','SingletonLock'):
                    source=self.profile/name
                    if source.is_symlink():source.rename(archive/name)


async def launch_visible_chrome(profile,disable_http2=False,initial_url='about:blank'):
    profile=Path(profile).resolve();profile.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=profile/'SingletonLock'
    if lock.exists() or lock.is_symlink():
        raise ValueError('This Visible browser profile is already in use. Close its Chrome window before reopening it.')
    port_file=profile/'DevToolsActivePort'
    debug_port=available_debug_port()
    env={k:v for k,v in os.environ.items() if not k.endswith('API_KEY') and k not in {
        'PERSONAL_AGENT_KEY_FD','OPENAI_BASE_URL','OPENAI_API_BASE','OPENAI_PROXY','NGROK_AUTHTOKEN'}}
    process=await asyncio.create_subprocess_exec(*chrome_arguments(profile,disable_http2,initial_url,debug_port=debug_port),env=env,
        stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    native=NativeChrome(process,profile=profile)
    try:
        async with asyncio.timeout(20):
            while True:
                if process.returncode is not None:raise ValueError('Chrome closed before its connection was ready')
                try:endpoint=await debugging_endpoint(debug_port)
                except (httpx.HTTPError,ValueError,OSError):endpoint=None
                if endpoint and process.returncode is None:
                    native.endpoint=endpoint
                    # Explicit ports do not create this file in Chrome. Keep
                    # the existing worker discovery contract, after validation.
                    port_file.write_text(str(debug_port)+'\n'+urlsplit(endpoint).path+'\n')
                    port_file.chmod(0o600)
                    if initial_url!='about:blank':await asyncio.sleep(2)
                    return native
                await asyncio.sleep(.05)
    except BaseException:
        await native.close()
        raise
