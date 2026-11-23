"""Own a minimal Chrome process; Playwright attaches after native startup."""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import time
from urllib.parse import urlsplit


def chrome_executable():
    for path in (Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
                 Path.home()/'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'):
        if path.is_file():return path
    raise ValueError('Install Google Chrome to use Visible mode')


def chrome_arguments(profile,disable_http2=False,initial_url='about:blank'):
    if initial_url!='about:blank':
        p=urlsplit(initial_url)
        if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('Native startup requires a public entry URL without session parameters')
    args=[str(chrome_executable()),'--user-data-dir='+str(profile),'--profile-directory=Default',
          '--disable-sync','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0']
    if disable_http2:args.append('--disable-http2')
    return args+[initial_url]


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
    previous_port=port_file.read_bytes() if port_file.exists() else None
    started=time.time()
    env={k:v for k,v in os.environ.items() if not k.endswith('API_KEY') and k not in {
        'PERSONAL_AGENT_KEY_FD','OPENAI_BASE_URL','OPENAI_API_BASE','OPENAI_PROXY','NGROK_AUTHTOKEN'}}
    process=await asyncio.create_subprocess_exec(*chrome_arguments(profile,disable_http2,initial_url),env=env,
        stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    native=NativeChrome(process,profile=profile)
    try:
        async with asyncio.timeout(20):
            while True:
                if process.returncode is not None:raise ValueError('Chrome closed before its connection was ready')
                if port_file.exists() and port_file.stat().st_mtime>=started-1:
                    try:current_port=port_file.read_bytes()
                    except OSError:
                        await asyncio.sleep(.05);continue
                    if current_port==previous_port:
                        await asyncio.sleep(.05);continue
                    lines=current_port.decode().splitlines()
                    if len(lines)>=2 and lines[0].isdigit() and 0<int(lines[0])<65536 and lines[1].startswith('/devtools/browser/'):
                        native.endpoint='ws://127.0.0.1:'+lines[0]+lines[1]
                        # Match the tested sequence: Chrome starts the provider
                        # page itself, with a brief uninstrumented startup period.
                        if initial_url!='about:blank':await asyncio.sleep(2)
                        return native
                await asyncio.sleep(.05)
    except BaseException:
        await native.close()
        raise
