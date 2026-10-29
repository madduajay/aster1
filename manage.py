#!/usr/bin/env python3
"""Manual lifecycle for the local personal agent. Never prints credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import time
import urllib.request

from personal_agent import config
from personal_agent.db import Database

OLD_ROOT = Path('/private/tmp/aster-verification-20261025')
TLS_SOURCE = OLD_ROOT / '.runtime/secrets'
PID_FILE = config.RUNTIME / 'process.json'


def command(args):
    return subprocess.run(args, capture_output=True, text=True, check=False)


def owner():
    result = command(['/usr/sbin/lsof', '-nP', '-iTCP:55468', '-sTCP:LISTEN', '-t'])
    pids = set(result.stdout.split())
    if not pids:
        return None
    if len(pids) != 1:
        raise RuntimeError('Ambiguous port ownership; no process stopped')
    pid = int(pids.pop())
    result = command(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'])
    paths = [line[1:] for line in result.stdout.splitlines() if line.startswith('n')]
    if len(paths) != 1:
        raise RuntimeError('Cannot verify port owner; no process stopped')
    return pid, Path(paths[0]).resolve()


def setup():
    config.prepare()
    count = Database().seed()
    directory = config.RUNTIME / 'tls'
    directory.mkdir(mode=0o700, exist_ok=True)
    manifest = directory / 'fingerprints.json'
    expected = json.loads(manifest.read_text()) if manifest.exists() else None
    fingerprints = {}
    for suffix in ('pem', 'key'):
        source = TLS_SOURCE / f'aster-named-tls.{suffix}'
        target = directory / f'server.{suffix}'
        if not target.exists():
            target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        fingerprint = hashlib.sha256(target.read_bytes()).hexdigest()
        original = expected[suffix] if expected else hashlib.sha256(source.read_bytes()).hexdigest()
        if fingerprint != original:
            raise RuntimeError('TLS copy verification failed')
        fingerprints[suffix] = fingerprint
    if not expected:
        manifest.write_text(json.dumps(fingerprints))
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(directory / 'server.pem', directory / 'server.key')
    print(f'Setup ready. Newly provisioned accounts: {count}. Credentials: {config.RUNTIME / "initial-accounts.txt"}')


def stop(allow_old=False):
    current = owner()
    if not current:
        print('Port 55468 is free.')
        return
    pid, directory = current
    allowed = {config.ROOT.resolve()}
    if allow_old:
        allowed.add(OLD_ROOT.resolve())
    if directory not in allowed:
        raise RuntimeError('Port belongs to another workspace; no process stopped')
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if owner() is None:
            print('Previous application stopped; port 55468 is free.')
            return
        time.sleep(0.1)
    raise RuntimeError('Graceful shutdown not confirmed; no replacement started')


def start():
    setup()
    current = owner()
    if current and current[1] == config.ROOT.resolve():
        print('Personal agent already owns port 55468.'); return
    # Resolve native credential access before stopping the old application.
    key = config.keychain_key()
    stop(allow_old=True)
    read_fd, write_fd = os.pipe()
    env = os.environ.copy()
    for name in ['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_API_BASE', 'OPENAI_PROXY', 'LANGCHAIN_API_KEY', 'LANGSMITH_API_KEY']:
        env.pop(name, None)
    env['PERSONAL_AGENT_KEY_FD'] = str(read_fd)
    env['LANGSMITH_TRACING'] = 'false'
    env['LANGCHAIN_TRACING_V2'] = 'false'
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(config.RUNTIME / 'playwright')
    with (config.RUNTIME / 'server.log').open('ab', buffering=0) as log:
        try:
            process = subprocess.Popen([str(config.ROOT / '.venv/bin/python'), '-u', str(config.ROOT / 'manage.py'), 'serve'],
                                       cwd=config.ROOT, env=env, pass_fds=(read_fd,), stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        finally:
            os.close(read_fd)
        with os.fdopen(write_fd, 'w') as pipe:
            pipe.write(key)
        del key
    PID_FILE.write_text(json.dumps({'pid': process.pid, 'url': config.ORIGIN}))
    context = ssl.create_default_context(cafile=str(config.RUNTIME / 'tls/server.pem'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('Application exited before readiness; inspect .runtime/server.log')
        try:
            with opener.open(config.ORIGIN + '/api/health', timeout=1) as response:
                if json.load(response).get('application') == 'aster1-personal-agent':
                    print(f'Personal agent ready at {config.ORIGIN} (PID {process.pid}).')
                    return
        except (OSError, ValueError):
            time.sleep(0.2)
    raise RuntimeError('Readiness not confirmed; inspect status before retrying')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['setup', 'start', 'stop', 'status', 'serve'])
    action = parser.parse_args().action
    if action == 'setup':
        setup()
    elif action == 'start':
        start()
    elif action == 'stop':
        stop()
    elif action == 'status':
        current = owner()
        print('No listener.' if not current else f'Port owner PID {current[0]}, workspace {current[1]}')
    else:
        import uvicorn
        uvicorn.run('personal_agent.app:app', host='127.0.0.1', port=55468, workers=1,
                    ssl_certfile=str(config.RUNTIME / 'tls/server.pem'), ssl_keyfile=str(config.RUNTIME / 'tls/server.key'),
                    access_log=False, log_level='warning', server_header=False)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No SDK/Keychain arguments or captured output may enter the console.
        print(f'Operation failed ({type(exc).__name__}). Check local permissions and sanitized logs.', file=sys.stderr)
        sys.exit(1)
