"""Exercise the real isolated action boundary with a deterministic planner (no API calls)."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def child():
    import browser_use
    class Planner:
        def __init__(self,**kwargs):
            self.browser=kwargs['browser'];self.tools=kwargs['tools'];self.state=SimpleNamespace(n_steps=0)
        def stop(self):pass
        async def run(self,**kwargs):
            await self.browser.start()
            cdp=await self.browser.get_or_create_cdp_session()
            await cdp.cdp_client.send.Runtime.evaluate(params={'expression':"document.querySelector('main').scrollIntoView({block:'start'})",'awaitPromise':True},session_id=cdp.session_id)
            model=self.tools.registry.create_action_model()
            result=None
            for name,params in [('inspect_selector',{'selector':'label[for=s10]'}),('click_selector',{'selector':'label[for=s10]'}),('inspect_selector',{'selector':'main > button:first-of-type'}),('click_selector',{'selector':'main > button:first-of-type'})]:
                result=await self.tools.act(model.model_validate({name:params}),self.browser)
                if result.is_done:break
            return SimpleNamespace(is_successful=lambda:False,final_result=lambda:result.extracted_content)
    browser_use.Agent=Planner
    sys.path.insert(0,str(ROOT/'personal_agent'))
    from browser_use_worker import main
    await main()


async def parent():
    from personal_agent import config,browser_host as host_module,browser_tasks as task_module
    from personal_agent.db import Database
    from personal_agent.browser_tasks import BrowserTasks,GoalCheck
    from validation.browser_fixtures import FixtureBrowsers
    runtime=config.RUNTIME;original=host_module.public_url;original_task=task_module.public_url
    spawn=asyncio.create_subprocess_exec
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright')
    config.RUNTIME=Path(tempfile.mkdtemp(prefix='browser-use-boundary-',dir=runtime));config.prepare()
    db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id']
    sid=db.new_session(user);run='boundary'
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
    async def allow_fixture(url):
        from urllib.parse import urlsplit
        if urlsplit(url).hostname!='shop.example':await original(url)
    async def fixture_spawn(*args,**kwargs):
        if len(args)>1 and args[1]==str(ROOT/'personal_agent/browser_use_worker.py'):
            args=(args[0],__file__,'--child',*args[2:])
        return await spawn(*args,**kwargs)
    class Verifier:
        def with_structured_output(self,*args):return self
        async def ainvoke(self,*args):return GoalCheck(success=False,reason='Cart empty; target changed.',evidence_ids=[0])
    host_module.public_url=task_module.public_url=allow_fixture
    asyncio.create_subprocess_exec=fixture_spawn
    host=FixtureBrowsers(db,headless=True);manager=BrowserTasks(db,host,'sk-controlled-placeholder',Verifier())
    report={'passed':False,'model_calls':0,'real_site_mutations':0}
    try:
        page=(await host.context(user)).pages[0];await page.goto('https://shop.example/product')
        tid=await manager.start(user,run,'boundary-call','Select size 10 and add to cart',['shop.example'])
        end=time.monotonic()+45;approved=False
        while time.monotonic()<end:
            task=manager.owned(user,run,tid)
            if task['status'] in {'completed','interrupted'}:break
            row=db.one("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,))
            if row:
                assert not approved
                # Change real DOM after proposal but before explicit approval.
                await page.evaluate("document.querySelector('main p').textContent='Color: blue. Width: medium. Price: $81.00. Quantity: 1.'")
                await manager.decide(user,run,tid,row['id'],'approve');approved=True
            await asyncio.sleep(.05)
        task=manager.owned(user,run,tid)
        assert approved and task['status']=='completed',task
        result=json.loads(task['result']);assert result['status']=='unverified'
        assert db.one("SELECT id FROM browser_use_actions WHERE task_id=? AND status='invalidated'",(tid,))
        assert await page.evaluate('window.addCount')==0 and await page.evaluate('window.purchaseCount')==0
        report.update(passed=True,real_ipc=True,stable_target_allowed=True,changed_price_invalidates_approval=True,no_dispatch_after_change=True)
    finally:
        await manager.close();await host.close()
        asyncio.create_subprocess_exec=spawn
        host_module.public_url=original;task_module.public_url=original_task;config.RUNTIME=runtime
        (runtime/'browser-use-boundary.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
