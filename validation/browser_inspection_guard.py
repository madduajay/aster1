"""Exercise relaxed inspection/navigation and retained approval through real worker IPC."""
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
SECRET_MARKER='CONTROLLED_PRIVATE_ATTRIBUTE_CONTENT'


async def child():
    import browser_use
    class Planner:
        def __init__(self,**kwargs):
            self.browser,self.tools=kwargs['browser'],kwargs['tools'];self.state=SimpleNamespace(n_steps=0)
        def stop(self):pass
        async def run(self,**kwargs):
            await self.browser.start()
            model=self.tools.registry.create_action_model()
            actions=[('find_elements',{'selector':'#reservation-entry','attributes':[
                'href','target','rel','data-testid','value','onclick','data-access-token']}),
                ('inspect_selector',{'selector':'#reservation-entry'}),
                ('click_selector',{'selector':'#reservation-entry'}),
                ('inspect_selector',{'selector':'main > button:last-of-type'}),
                ('click_selector',{'selector':'main > button:last-of-type'})]
            for name,params in actions:
                result=await self.tools.act(model.model_validate({name:params}),self.browser)
                if name=='find_elements':
                    assert not result.is_done and not result.error
                    assert SECRET_MARKER not in result.extracted_content
                    assert 'href=' in result.extracted_content and 'reservation-entry' in result.extracted_content
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
    report={'passed':False,'real_site_actions':0,'model_calls':0}
    with tempfile.TemporaryDirectory(prefix='inspection-guard-',dir=runtime) as temporary:
        config.RUNTIME=Path(temporary);config.prepare()
        db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id']
        sid=db.new_session(user);run='inspection-guard'
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
            async def ainvoke(self,*args):return GoalCheck(success=False,reason='Final submission intentionally rejected.',evidence_ids=[0])
        host_module.public_url=task_module.public_url=allow_fixture
        asyncio.create_subprocess_exec=fixture_spawn
        host=FixtureBrowsers(db,headless=True);manager=BrowserTasks(db,host,'sk-controlled-placeholder',Verifier())
        try:
            page=(await host.context(user)).pages[0];await page.goto('https://shop.example/product')
            await page.evaluate('''marker=>{
                const link=document.createElement('a');link.id='reservation-entry';link.textContent='Reserve a table';
                link.href='https://shop.example/reservations';link.target='_self';link.rel='noopener';
                link.setAttribute('data-testid','reservation-entry');link.setAttribute('value',marker);
                link.setAttribute('data-access-token',marker);document.querySelector('main').prepend(link);
            }''',SECRET_MARKER)
            tid=await manager.start(user,run,'inspection-guard-call','Open reservation flow, inspect final submission',['shop.example'])
            end=time.monotonic()+45;rejected=False
            while time.monotonic()<end:
                task=manager.owned(user,run,tid)
                if task['status'] in {'completed','interrupted'}:break
                row=db.one("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,))
                if row:
                    description=json.loads(row['description'])
                    assert description['control']=='Place order',description['control']
                    await manager.decide(user,run,tid,row['id'],'reject');rejected=True
                await asyncio.sleep(.05)
            task=manager.owned(user,run,tid)
            assert rejected and task['status']=='completed',task['status']
            inspection=db.one("SELECT category,status FROM browser_use_actions WHERE task_id=? AND operation='find_elements'",(tid,))
            assert inspection=={'category':'read','status':'completed'},inspection
            entry=db.one("SELECT category,status FROM browser_use_actions WHERE task_id=? AND operation='click_selector' ORDER BY seq LIMIT 1",(tid,))
            assert entry=={'category':'read','status':'completed'},entry
            assert await page.evaluate('window.purchaseCount')==0
            report.update(passed=True,public_attributes_read=True,restricted_attributes_omitted=True,
                reservation_entry_without_approval=True,final_submission_required_approval=True,
                final_submission_not_dispatched=True,real_worker_ipc=True)
        finally:
            await manager.close();await host.close()
            asyncio.create_subprocess_exec=spawn
            host_module.public_url=original;task_module.public_url=original_task;config.RUNTIME=runtime
    (runtime/'maintenance-20261119/inspection-guard-proof.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
