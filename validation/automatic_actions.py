"""Verify automatic cart preparation and one final purchase approval through the real worker."""
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
            for name,params in [('inspect_selector',{'selector':'label[for=s10]'}),('click_selector',{'selector':'label[for=s10]'}),('inspect_selector',{'selector':'main > button:first-of-type'}),('click_selector',{'selector':'main > button:first-of-type'}),('inspect_selector',{'selector':'main > button:last-of-type'}),('click_selector',{'selector':'main > button:last-of-type'})]:
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
    config.RUNTIME=Path(tempfile.mkdtemp(prefix='automatic-actions-',dir=runtime));config.prepare()
    db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id']
    sid=db.new_session(user);run='automatic-actions'
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
        async def ainvoke(self,*args):return GoalCheck(success=False,reason='The purchase was rejected.',evidence_ids=[0])
    host_module.public_url=task_module.public_url=allow_fixture
    asyncio.create_subprocess_exec=fixture_spawn
    host=FixtureBrowsers(db,headless=True);manager=BrowserTasks(db,host,'sk-controlled-placeholder',Verifier())
    report={'passed':False,'model_calls':0,'real_site_mutations':0}
    try:
        page=(await host.context(user)).pages[0];await page.goto('https://shop.example/product')
        tid=await manager.start(user,run,'boundary-call','Select size 10 and add to cart',['shop.example'])
        end=time.monotonic()+45;approvals=[]
        while time.monotonic()<end:
            task=manager.owned(user,run,tid)
            if task['status'] in {'completed','interrupted'}:break
            row=db.one("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,))
            if row:
                description=json.loads(row['description'])
                assert description['control']=='Place order',description
                assert await page.evaluate('window.addCount')==1
                assert await page.evaluate('window.purchaseCount')==0
                approvals.append(description['control'])
                await manager.decide(user,run,tid,row['id'],'reject')
            await asyncio.sleep(.05)
        task=manager.owned(user,run,tid)
        assert approvals==['Place order'] and task['status']=='completed',task
        result=json.loads(task['result']);assert result['status']=='rejected'
        assert await page.evaluate('window.addCount')==1 and await page.evaluate('window.purchaseCount')==0
        cart=db.one("SELECT category,status FROM browser_use_actions WHERE task_id=? AND operation='click_selector' AND json_extract(description,'$.control')='Add to Cart'",(tid,))
        assert cart['category']=='mutation' and cart['status']=='executed',cart
        report.update(passed=True,real_ipc=True,automatic_size_selection=True,automatic_cart_addition=True,
                      final_purchase_approvals=approvals,cart_additions=1,purchases=0,mutation_receipt_preserved=True)
    finally:
        await manager.close();await host.close()
        asyncio.create_subprocess_exec=spawn
        host_module.public_url=original;task_module.public_url=original_task;config.RUNTIME=runtime
        (runtime/'automatic-actions.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
