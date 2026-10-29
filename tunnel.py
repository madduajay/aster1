#!/usr/bin/env python3
"""Manual ngrok tunnel lifecycle. Secrets stay in protected local files."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

from personal_agent import config

BINARY = config.RUNTIME / 'bin/ngrok'
STATE = config.RUNTIME / 'tunnel.json'
ORIGIN_FILE = config.RUNTIME / 'public-origin.json'
NGROK_CONFIG = config.RUNTIME / 'ngrok.yml'
API = 'http://127.0.0.1:4047/api/tunnels'


def fetch_tunnels():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(API, timeout=2) as response:
        return json.load(response)['tunnels']


def owned_process(state):
    pid = int(state['pid'])
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip().startswith(str(BINARY) + ' http ')


def start():
    config.prepare()
    if STATE.exists() and owned_process(json.loads(STATE.read_text())):
        print('Tunnel already running: ' + json.loads(STATE.read_text())['origin'])
        return
    if not BINARY.is_file():
        raise RuntimeError('Install the official ngrok binary into .runtime/bin/ngrok first')
    token_file = config.RUNTIME / 'ngrok-token.txt'
    if token_file.exists():
        token_file.chmod(0o600)
        token = token_file.read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise RuntimeError('The local ngrok token is empty or invalid')
        # JSON is also YAML; no shell arguments or output contain the token.
        NGROK_CONFIG.write_text(json.dumps({'version':'2','authtoken':token,'web_addr':'127.0.0.1:4047'}))
        NGROK_CONFIG.chmod(0o600)
        token_file.unlink()
        del token
    if not NGROK_CONFIG.exists():
        raise RuntimeError('Save your ngrok authtoken in .runtime/ngrok-token.txt first')
    check = subprocess.run([str(BINARY),'config','check','--config',str(NGROK_CONFIG)], capture_output=True)
    if check.returncode:
        raise RuntimeError('Ngrok configuration validation failed')
    with (config.RUNTIME/'ngrok.log').open('ab', buffering=0) as log:
        proc = subprocess.Popen([str(BINARY),'http',config.ORIGIN,'--config',str(NGROK_CONFIG),
            '--inspect=false','--upstream-tls-verify=true','--upstream-tls-verify-cas',str(config.RUNTIME/'tls/server.pem'),
            '--log=stdout','--log-format=json','--log-level=warn'],
            cwd=config.ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError('Ngrok could not authenticate/connect; inspect .runtime/ngrok.log locally')
        try:
            tunnels = fetch_tunnels()
            matches = [t for t in tunnels if t.get('proto')=='https' and t.get('config',{}).get('addr')==config.ORIGIN]
            if matches:
                origin = matches[0]['public_url']
                parts = urlsplit(origin)
                if parts.scheme!='https' or origin!='https://'+str(parts.hostname):
                    raise RuntimeError('Unexpected tunnel URL')
                ORIGIN_FILE.write_text(json.dumps({'origin':origin}))
                ORIGIN_FILE.chmod(0o600)
                config.allowed_origins()  # Validate before app restart.
                STATE.write_text(json.dumps({'pid':proc.pid,'origin':origin}))
                STATE.chmod(0o600)
                print('Tunnel connected: '+origin)
                print('Restart the app to enable this exact public host and origin.')
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.2)
    proc.terminate()
    raise RuntimeError('Ngrok readiness was not confirmed')


def stop():
    if STATE.exists():
        state=json.loads(STATE.read_text())
        if owned_process(state):
            os.kill(state['pid'],signal.SIGTERM)
        STATE.unlink()
    ORIGIN_FILE.unlink(missing_ok=True)
    print('Tunnel stopped. Restart the app to remove the public host from its allowlist.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['start','stop','status'])
    action=parser.parse_args().action
    if action=='start':start()
    elif action=='stop':stop()
    elif STATE.exists() and owned_process(json.loads(STATE.read_text())):
        print('Tunnel running: '+json.loads(STATE.read_text())['origin'])
    else:print('Tunnel is stopped.')


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(str(exc) if isinstance(exc,RuntimeError) else 'Tunnel operation failed ('+type(exc).__name__+').',file=sys.stderr)
        sys.exit(1)
