"""Reproduce stale search receipts through the real guarded Browser Use worker, no paid calls."""
import asyncio
import html
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
SEARCH='''<main><h1>Search store</h1><search aria-label="Search products"><button id=opener type=button onclick="document.querySelector('form').hidden=false">Search</button><form hidden role=search method=get action=/results><label>Search products<input id=query name=q type=search></label><button type=submit>Search</button></form></search></main>'''


async def child():
    import browser_use
    class Planner:
        def __init__(self,**kw):self.browser=kw['browser'];self.tools=kw['tools'];self.goal=kw['task'];self.state=SimpleNamespace(n_steps=0)
        def stop(self):pass
        async def run(self,**kw):
            await self.browser.start();model=self.tools.registry.create_action_model()
            async def act(name,params):
                result=await self.tools.act(model.model_validate({name:params}),self.browser)
                if result.is_done or result.error:raise RuntimeError('Search was blocked: '+str(result.extracted_content or result.error))
            await act('inspect_selector',{'selector':'#opener'});await act('click_selector',{'selector':'#opener'})
            await self.browser.get_browser_state_summary(include_screenshot=False)
            index=next(i for i,n in (await self.browser.get_selector_map()).items() if (n.attributes or {}).get('id')=='query')
            await act('input',{'index':index,'text':self.goal.removeprefix('Search for ').split('\n')[0]})
            await act('send_keys',{'keys':'ENTER'})
            return SimpleNamespace(is_successful=lambda:True,final_result=lambda:'Observed the requested search results.')
    browser_use.Agent=Planner
    sys.path.insert(0,str(ROOT/'personal_agent'));from browser_use_worker import main
    await main()


async def parent():
    from personal_agent import config,browser_host as host_module,browser_tasks as task_module
    from personal_agent.browser_host import BrowserHost
    from personal_agent.browser_tasks import BrowserTasks,GoalCheck
    from personal_agent.browser_policy import legacy_duplicate_key
    from personal_agent.db import Database
    runtime=config.RUNTIME;original=host_module.public_url;original_task=task_module.public_url;spawn=asyncio.create_subprocess_exec
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright')
    config.RUNTIME=Path(tempfile.mkdtemp(prefix='search-boundary-',dir=runtime));config.prepare()
    db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id'];sid=db.new_session(user)
    async def allow(url):
        if urlsplit(url).hostname!='shop.example':await original(url)
    async def fixture_spawn(*args,**kwargs):
        if len(args)>1 and args[1]==str(ROOT/'personal_agent/browser_use_worker.py'):args=(args[0],__file__,'--child',*args[2:])
        return await spawn(*args,**kwargs)
    class Host(BrowserHost):
        async def context(self,user):
            fresh=user not in self.contexts;c=await super().context(user)
            if fresh:
                async def route(r):
                    p=urlsplit(r.request.url);q=parse_qs(p.query).get('q',[''])[0]
                    body='<main><h1>Results</h1><p>Search results for '+html.escape(q)+'</p></main>' if p.path=='/results' else SEARCH
                    await r.fulfill(status=200,content_type='text/html; charset=utf-8',body=body)
                await c.route('https://shop.example/**',route)
            return c
    class Verifier:
        def with_structured_output(self,*a,**kw):return self
        async def ainvoke(self,messages):
            data=json.loads(messages[-1].content)
            return GoalCheck(success='/results' in data['page']['url'],reason='Requested results observed.',evidence_ids=[0])
    host_module.public_url=task_module.public_url=allow;asyncio.create_subprocess_exec=fixture_spawn
    host=Host(db);manager=BrowserTasks(db,host,'sk-controlled-placeholder',Verifier())
    report={'passed':False,'model_calls':0,'real_site_mutations':0}
    try:
        db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('old-search',?,?,'completed',0)",(user,sid))
        db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES('old-task',?,'old-search','old-call','Earlier search','[\"shop.example\"]',0,'completed',0)",(user,))
        old={'url':'https://shop.example/search'};key=legacy_duplicate_key('send_keys',{'keys':'ENTER'},old)
        db.execute("INSERT INTO browser_use_actions(id,task_id,seq,operation,category,status,description,fingerprint,duplicate_key,before_state) VALUES('old-enter','old-task',1,'send_keys','approval','executed','{}','legacy',?,?)",(key,json.dumps(old)))
        for i,query in enumerate(['Blue shoes','Red shoes']):
            run='search-'+str(i);db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
            page=(await host.context(user)).pages[0];await page.goto('https://shop.example/search')
            tid=await manager.start(user,run,'call-'+run,'Search for '+query,['shop.example'])
            end=time.monotonic()+45
            while time.monotonic()<end:
                row=manager.owned(user,run,tid)
                if row['status'] in {'completed','interrupted'} and tid not in manager.workers:break
                assert not db.one("SELECT id FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,)),'Search must not ask transaction approval'
                await asyncio.sleep(.1)
            row=manager.owned(user,run,tid);assert row['status']=='completed',row
            result=json.loads(row['result']);assert result['status']=='verified',result
            entered=db.one("SELECT * FROM browser_use_actions WHERE task_id=? AND operation='send_keys'",(tid,))
            assert entered['category']=='preparation' and entered['status']=='completed'
            node=json.loads(entered['before_state'])['node'];assert node['value']==query and node['type']=='search'
            assert parse_qs(urlsplit(page.url).query)['q']==[query]
            assert await host.preview(user,run)  # Final browser image remains available after worker completion.
            db.execute("UPDATE runs SET status='completed' WHERE id=?",(run,))
        assert db.one("SELECT status FROM browser_use_actions WHERE id='old-enter'")['status']=='executed'
        report.update(passed=True,searches_completed=2,old_receipt_preserved=True,no_transaction_approvals=True,focused_query_bound=True,headless=host.headless,frozen_preview=True)
    finally:
        await manager.close();await host.close();asyncio.create_subprocess_exec=spawn
        host_module.public_url=original;task_module.public_url=original_task;config.RUNTIME=runtime
        (runtime/'search-boundary.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
