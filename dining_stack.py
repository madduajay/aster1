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
SERVICES={'company':('company-dining',55469,['-m','dining_company.app']),
    'company-hotels':('company-hotels',55470,['-m','availability.company','hotels']),
    'company-flights':('company-flights',55471,['-m','availability.company','flights']),
    'dining-agent':('dining-subagent',55472,['-m','availability.subagent','dining']),
    'travel-agent':('travel-subagent',55473,['-m','availability.subagent','travel'])}


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
        service,port,_=SERVICES[kind]
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=1) as response:
            health=json.load(response)
            return health.get('service')==service and health.get('ready',True)
    except (OSError,ValueError):return False


def start():
    os.umask(0o077);RUNTIME.mkdir(exist_ok=True)
    from availability.security import Security
    Security(RUNTIME)
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    for kind,args in [('temporal',[str(ROOT/'dining_stack.py'),'serve-temporal']),*[(k,v[2]) for k,v in SERVICES.items()]]:
        if ready(kind):continue
        read_fd=write_fd=None;env=os.environ.copy();fds=()
        for name in ('OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_API_BASE','OPENAI_PROXY','LANGCHAIN_API_KEY','LANGSMITH_API_KEY'):env.pop(name,None)
        env['LANGSMITH_TRACING']='false';env['LANGCHAIN_TRACING_V2']='false'
        if kind.endswith('-agent'):
            from personal_agent.config import keychain_key
            key=keychain_key();read_fd,write_fd=os.pipe();env['PERSONAL_AGENT_KEY_FD']=str(read_fd);fds=(read_fd,)
        with (RUNTIME/(kind+'.log')).open('ab',buffering=0) as log:
            process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-u',*args],cwd=ROOT,
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,env=env,pass_fds=fds)
        if read_fd is not None:
            os.close(read_fd)
            with os.fdopen(write_fd,'w') as pipe:pipe.write(key)
            del key
        state[kind]=process.pid;STATE.write_text(json.dumps(state))
        for _ in range(150):
            if process.poll() is not None:raise RuntimeError(kind+' exited; inspect its local log')
            if ready(kind):break
            time.sleep(.2)
        else:raise RuntimeError(kind+' readiness not confirmed; inspect its local log')
    print('Temporal ready on 7233; Company dining/hotels/flights on 55469–55471; Dining/Travel agents on 55472–55473.')


def stop():
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    for kind in (*reversed(SERVICES),'temporal'):
        pid=state.get(kind)
        if not pid:continue
        result=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
        expected=' '.join(SERVICES[kind][2][1:]) if kind in SERVICES else 'dining_stack.py serve-temporal'
        if expected not in result.stdout:continue
        os.kill(pid,signal.SIGTERM)
    print('Requested shutdown of managed availability services.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['start','stop','status','serve-temporal'])
    action=parser.parse_args().action
    if action=='start':start()
    elif action=='stop':stop()
    elif action=='status':print({kind:ready(kind) for kind in ('temporal',*SERVICES)})
    else:asyncio.run(temporal_server())
