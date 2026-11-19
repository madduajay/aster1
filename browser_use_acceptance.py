"""Capped live Browser Use acceptance on isolated synthetic pages, never a real cart."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from personal_agent import config,browser_host as browser_module
from personal_agent.agent import Engine
from personal_agent.browser_tasks import BrowserTasks
from personal_agent import browser_tasks as tasks_module
from personal_agent.db import Database
from validation.browser_fixtures import FixtureBrowsers


async def main():
    root=config.RUNTIME;os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(root/'playwright')
    original=browser_module.public_url;original_task_url=tasks_module.public_url;manager=browsers=engine=None
    report={'passed':False,'browser_use_version':'0.13.14','real_browser_model':config.BROWSER_MODEL,'real_website_mutations':0}
    try:
        key=config.keychain_key()
        config.RUNTIME=Path(tempfile.mkdtemp(prefix='browser-use-acceptance-',dir=root));config.prepare()
        db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id']
        sid=db.new_session(user);run='browser-use-cart'
        db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
        async def fixture_public(url):
            if urlsplit(url).hostname=='shop.example':return
            await original(url)
        browser_module.public_url=fixture_public
        tasks_module.public_url=fixture_public
        browsers=FixtureBrowsers(db,headless=True)
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'checkpoints.sqlite')) as saver:
            engine=Engine(db,browsers,saver,key)
            manager=BrowserTasks(db,browsers,key,engine.model);del key
            task="Open https://shop.example/product, select the blue men's running shoes size 10, quantity 1 at $80.00, add them to cart using the application's inline approval, and verify the cart item, size, quantity and price. Do not checkout or place an order."
            tid=await manager.start(user,run,'test-cart',task,['shop.example'])
            end=time.monotonic()+240;approvals=[]
            while time.monotonic()<end:
                state=manager.owned(user,run,tid)
                if state['status'] in {'completed','interrupted','cancelled'}:break
                for row in db.rows("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed' ORDER BY seq",(tid,)):
                    desc=json.loads(row['description']);print(json.dumps({'approval':desc['control']}),flush=True)
                    assert desc['control']!='Place order'
                    approvals.append(desc['control'])
                    await manager.decide(user,run,tid,row['id'],'approve')
                await asyncio.sleep(.2)
            state=manager.owned(user,run,tid);print('worker state',state['status'],flush=True)
            result=json.loads(state['result'] or '{}');report['result']=result
            page=(await browsers.context(user)).pages[0]
            report.update(actual_cart_count=await page.evaluate('window.addCount'),actual_purchase_count=await page.evaluate('window.purchaseCount'))
            assert state['status']=='completed' and result['status']=='verified'
            assert await page.evaluate('window.addCount')==1
            assert await page.evaluate('window.purchaseCount')==0
            assert 'Add to Cart' in approvals
            assert not db.one("SELECT id FROM browser_use_actions WHERE task_id=? AND status='unknown'",(tid,))
            report.update(passed=True,cart_added_once=True,purchase_attempted=False,approvals=approvals,model_calls=state['model_calls'])
    except Exception as exc:
        report['error_class']=type(exc).__name__
    finally:
        if manager:await manager.close()
        if browsers:await browsers.close()
        if engine:await engine.client.close()
        config.RUNTIME=root;browser_module.public_url=original
        tasks_module.public_url=original_task_url
        (root/'browser-use-acceptance.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='result'}))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
