"""Capped GPT-6.1 Terra browser planning against an isolated fixture shop."""
import asyncio,json,os,sys,tempfile,time
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


async def main():
    from personal_agent import config,browser_host as hm,browser_tasks as tm
    from personal_agent.browser_tasks import BrowserTasks
    from personal_agent.db import Database
    from personal_agent.agent import Engine
    from validation.browser_fixtures import FixtureBrowsers
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    root=config.RUNTIME;original=hm.public_url;oldt=tm.public_url
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(root/'playwright')
    report={'passed':False,'model':config.BROWSER_MODEL,'browser_call_limit':10,'real_site_mutations':0}
    manager=host=engine=None
    class CappedTasks(BrowserTasks):
        async def send(self,tid,payload):
            if 'model' in payload:payload={**payload,'model_call_limit':10,'max_steps':10}
            return await super().send(tid,payload)
    try:
        key=config.keychain_key()
        config.RUNTIME=Path(tempfile.mkdtemp(prefix='terra-acceptance-',dir=root));config.prepare()
        db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id'];sid=db.new_session(user)
        run='terra-fixture';db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(run,user,sid))
        async def allowed(url):
            if urlsplit(url).hostname!='shop.example':await original(url)
        hm.public_url=tm.public_url=allowed;host=FixtureBrowsers(db,headless=True)
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'graph.sqlite')) as saver:
            engine=Engine(db,host,saver,key);manager=CappedTasks(db,host,key,engine.model);del key
            tid=await manager.start(user,run,'terra-call','Open https://shop.example/product. Select blue shoes size 10, quantity 1, $80.00. Add to cart with application approval and verify the cart. Do not checkout or place an order.',['shop.example'])
            end=time.monotonic()+210;approvals=[]
            while time.monotonic()<end:
                state=manager.owned(user,run,tid)
                if state['status'] in {'completed','interrupted','cancelled'}:break
                for row in db.rows("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,)):
                    label=json.loads(row['description'])['control']
                    assert label=='Add to Cart',label
                    approvals.append(label);await manager.decide(user,run,tid,row['id'],'approve')
                await asyncio.sleep(.1)
            state=manager.owned(user,run,tid);result=json.loads(state['result'] or '{}')
            page=(await host.context(user)).pages[0]
            report.update(status=result.get('status'),model_calls=state['model_calls'],cart_count=await page.evaluate('window.addCount'),purchase_count=await page.evaluate('window.purchaseCount'),approvals=approvals)
            assert state['status']=='completed' and result['status']=='verified',report
            assert report['cart_count']==1 and report['purchase_count']==0 and approvals==['Add to Cart']
            assert state['model_calls']<=10
            report['passed']=True
    except Exception as exc:
        report['error_class']=type(exc).__name__
    finally:
        if manager:await manager.close()
        if host:await host.close()
        if engine:await engine.client.close()
        config.RUNTIME=root;hm.public_url=original;tm.public_url=oldt
        (root/'terra-acceptance.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
