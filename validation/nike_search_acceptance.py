"""Capped read-only Nike search check in a fresh headless profile. Never approve a mutation."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.db import Database
from personal_agent.browser_host import BrowserHost


async def main():
    runtime=config.RUNTIME;host=engine=manager=None
    report={'passed':False,'real_site':'nike.com','model':config.BROWSER_MODEL,'real_site_mutations_approved':0}
    try:
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright');key=config.keychain_key()
        config.RUNTIME=Path(tempfile.mkdtemp(prefix='nike-search-',dir=runtime));config.prepare()
        db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id'];sid=db.new_session(user);run='nike-read-only'
        db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
        host=BrowserHost(db)
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'checkpoints.sqlite')) as saver:
            engine=Engine(db,host,saver,key);del key;manager=engine.browser_tasks
            send=manager.send
            async def capped(tid,payload):
                if 'nonce' in payload:payload={**payload,'max_steps':12,'model_call_limit':14}
                await send(tid,payload)
            manager.send=capped
            task="Open https://www.nike.com and use its catalog search for Nike Streakfly 2 men's running shoes. Read the results and report an observed product link if present. This is read-only research: do not add to cart, checkout, buy, send messages, log in, or change accounts."
            tid=await manager.start(user,run,'nike-search-check',task,['nike.com'])
            end=time.monotonic()+220
            while time.monotonic()<end:
                row=manager.owned(user,run,tid)
                if row['status'] in {'completed','interrupted'} and tid not in manager.workers:break
                for pending in db.rows("SELECT id FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,)):
                    await manager.decide(user,run,tid,pending['id'],'reject')
                await asyncio.sleep(.2)
            row=manager.owned(user,run,tid);result=json.loads(row['result'] or '{}')
            actions=db.rows('SELECT * FROM browser_use_actions WHERE task_id=? ORDER BY seq',(tid,))
            submits=[a for a in actions if a['operation']=='send_keys' and a['category']=='preparation' and a['status']=='completed' and json.loads(a['before_state']).get('node',{}).get('type')=='search']
            assert submits,'No completed focused search submission'
            assert all(a['decision']!='approve' for a in actions)
            assert 'streakfly' in json.loads(submits[-1]['before_state'])['node']['value'].casefold()
            frame=await host.preview(user,run)
            if frame:(runtime/'nike-search-preview.jpg').write_bytes(frame)
            report.update(passed=True,status=result.get('status'),url=result.get('url'),headless=host.headless,model_calls=row['model_calls'],
                          search_submitted_without_transaction_approval=True,preview_available=bool(frame),actions=[{'operation':a['operation'],'category':a['category'],'status':a['status']} for a in actions],result=result)
    except Exception as exc:report.update(error_class=type(exc).__name__,error_detail=str(exc).splitlines()[0][:200])
    finally:
        if manager:await manager.close()
        if host:await host.close()
        if engine:await engine.client.close()
        config.RUNTIME=runtime;(runtime/'nike-search-acceptance.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k not in {'result','actions'}}))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
