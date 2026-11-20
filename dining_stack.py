"""Manage the independent Company service and persistent local Temporal server."""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parent
RUNTIME=ROOT/'.runtime'
STATE=RUNTIME/'dining-processes.json'


async def temporal_server():
    from temporalio.testing import WorkflowEnvironment
    os.umask(0o077)
    environment=await WorkflowEnvironment.start_local(download_dest_dir=str(RUNTIME/'bin'),port=7233,
        ui=True,ui_port=8233,dev_server_database_filename=str(RUNTIME/'temporal.sqlite'))
    event=asyncio.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):asyncio.get_running_loop().add_signal_handler(sig,event.set)
    try:await event.wait()
    finally:await environment.shutdown()


async def temporal_ready():
    from temporalio.client import Client
    try:
        async with asyncio.timeout(2):
            client=await Client.connect('127.0.0.1:7233')
            await client.service_client.check_health()
        return True
    except Exception:return False


def ready(kind):
    if kind=='temporal':return asyncio.run(temporal_ready())
    try:
        with urllib.request.urlopen('http://127.0.0.1:55469/health',timeout=1) as response:
            return json.load(response).get('service')=='company-dining'
    except (OSError,ValueError):return False


def start():
    os.umask(0o077);RUNTIME.mkdir(exist_ok=True)
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    for kind,args in [('temporal',[str(ROOT/'dining_stack.py'),'serve-temporal']),('company',['-m','dining_company.app'])]:
        if ready(kind):continue
        with (RUNTIME/(kind+'.log')).open('ab',buffering=0) as log:
            process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-u',*args],cwd=ROOT,
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        state[kind]=process.pid;STATE.write_text(json.dumps(state))
        for _ in range(150):
            if process.poll() is not None:raise RuntimeError(kind+' exited; inspect its local log')
            if ready(kind):break
            time.sleep(.2)
        else:raise RuntimeError(kind+' readiness not confirmed; inspect its local log')
    print('Temporal ready at 127.0.0.1:7233; Company dining ready at 127.0.0.1:55469.')


def stop():
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    for kind in ('company','temporal'):
        pid=state.get(kind)
        if not pid:continue
        result=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
        expected='dining_company.app' if kind=='company' else 'dining_stack.py serve-temporal'
        if expected not in result.stdout:continue
        os.kill(pid,signal.SIGTERM)
    print('Requested shutdown of managed dining services.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['start','stop','status','serve-temporal'])
    action=parser.parse_args().action
    if action=='start':start()
    elif action=='stop':stop()
    elif action=='status':print({kind:ready(kind) for kind in ('temporal','company')})
    else:asyncio.run(temporal_server())
