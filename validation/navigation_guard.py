"""Actual Browser Use/IPC navigation with legacy receipts, no model calls or real sites."""
import asyncio,ast,json,os,sys,tempfile,time
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
HTML='''<nav><button type=button id=categories aria-expanded=false aria-controls=menu onclick="this.setAttribute('aria-expanded',this.getAttribute('aria-expanded')==='false'?'true':'false')">Categories</button><div id=menu role=menu><a id=catalog href="/catalog">Catalog</a></div></nav>
<main><h1>Catalog</h1><button type=button id=confirm onclick="fetch('/submit',{method:'POST'})">Confirm booking</button><label>Search<input id=query type=search></label></main>'''

async def child():
    import browser_use
    class Planner:
        def __init__(self,**kw):self.browser=kw['browser'];self.tools=kw['tools'];self.state=SimpleNamespace(n_steps=0)
        def stop(self):pass
        async def run(self,**kw):
            await self.browser.start();model=self.tools.registry.create_action_model()
            async def act(name,params):return await self.tools.act(model.model_validate({name:params}),self.browser)
            async def click(selector):
                inspected=await act('inspect_selector',{'selector':selector});assert not inspected.error,inspected.error
                return await act('click_selector',{'selector':selector})
            for _ in range(2):
                result=await click('#categories');assert not result.error,result.error
            # A true uncertain booking must block its repeat and restrict writes.
            result=await click('#confirm');assert result.error and 'matching submission' in result.error,result
            # This lock must still permit proven navigation and fresh inspection.
            result=await click('#categories');assert not result.error,result.error
            result=await click('#catalog');assert not result.error,result.error
            result=await act('navigate',{'url':'https://navigation.fixture.test/overview'});assert not result.error,result.error
            await self.browser.get_browser_state_summary(include_screenshot=False)
            index=next(i for i,n in (await self.browser.get_selector_map()).items() if (n.attributes or {}).get('id')=='query')
            result=await act('input',{'index':index,'text':'must not be typed'});assert result.error and 'uncertain submission' in result.error
            return SimpleNamespace(is_successful=lambda:False,final_result=lambda:'Navigation inspected; uncertain booking not repeated.')
    browser_use.Agent=Planner
    sys.path.insert(0,str(ROOT/'personal_agent'));from browser_use_worker import main
    await main()

async def parent():
    from personal_agent import config,browser_host as hm,browser_tasks as tm
    from personal_agent.browser_host import BrowserHost
    from personal_agent.browser_tasks import BrowserTasks,GoalCheck
    from personal_agent.browser_policy import duplicate_key
    from personal_agent.db import Database
    runtime=config.RUNTIME;old_url=hm.public_url;old_task_url=tm.public_url;spawn=asyncio.create_subprocess_exec
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright')
    config.RUNTIME=Path(tempfile.mkdtemp(prefix='navigation-guard-',dir=runtime));config.prepare()
    db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id'];sid=db.new_session(user)
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('navigation',?,?,'running',0)",(user,sid))
    async def allow(url):
        from urllib.parse import urlsplit
        if urlsplit(url).hostname!='navigation.fixture.test':await old_url(url)
    async def fixture_spawn(*args,**kwargs):
        if len(args)>1 and args[1]==str(ROOT/'personal_agent/browser_use_worker.py'):args=(args[0],__file__,'--child',*args[2:])
        return await spawn(*args,**kwargs)
    hm.public_url=tm.public_url=allow;asyncio.create_subprocess_exec=fixture_spawn
    class Host(BrowserHost):
        submits=0
        async def context(self,user):
            fresh=user not in self.contexts;ctx=await super().context(user)
            if fresh:
                async def route(r):
                    if r.request.method=='POST':self.submits+=1
                    await r.fulfill(content_type='text/html',body=HTML)
                await ctx.route('https://navigation.fixture.test/**',route)
            return ctx
    class Verifier:
        def with_structured_output(self,*a):return self
        async def ainvoke(self,*a):return GoalCheck(success=False,reason='Inspection only; no booking repeated.',evidence_ids=[0])
    host=Host(db);manager=BrowserTasks(db,host,'sk-controlled-placeholder',Verifier())
    report={'passed':False,'model_calls':0,'real_site_mutations':0}
    try:
        host.set_scope(user,'navigation',['navigation.fixture.test']);host.run_for_user[user]='navigation'
        page=(await host.context(user)).pages[0];await page.goto('https://navigation.fixture.test/');host.set_view(user,'navigation',{'url':page.url})
        module=ast.parse((ROOT/'personal_agent/browser_use_worker.py').read_text())
        observer=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='OBSERVE_NODE' for t in n.targets))
        db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('legacy',?,?,'cancelled',0)",(user,sid))
        db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES('legacy-task',?,'legacy','legacy-call','Old task','[\"navigation.fixture.test\"]',0,'cancelled',0)",(user,))
        for seq,selector,status in [(1,'#categories','executed'),(2,'#confirm','unknown')]:
            state=await host.snapshot(user,'navigation');node=(await page.locator(selector).evaluate('e=>('+observer+').call(e)'))['node']
            state['node']={k:v for k,v in node.items() if k not in {'navigation_context','has_popup','expanded','controls_navigation','details_toggle'}}
            operation='click_selector';params={'selector':selector}
            db.execute("INSERT INTO browser_use_actions(id,task_id,seq,operation,category,status,description,fingerprint,duplicate_key,before_state) VALUES(?,'legacy-task',?,?,'approval',?,'{}','legacy',?,?)",('legacy-'+str(seq),seq,operation,status,duplicate_key(operation,params,state),json.dumps(state)))
        tid=await manager.start(user,'navigation','navigation-call','Inspect navigation without repeating the earlier booking',['navigation.fixture.test'])
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            task=manager.owned(user,'navigation',tid)
            assert task['status']!='approval','Routine navigation incorrectly requested approval'
            if task['status'] in {'completed','interrupted'}:break
            await asyncio.sleep(.05)
        assert task['status']=='completed',task
        result=json.loads(task['result']);assert not result.get('diagnostics'),result.get('diagnostics')
        actions=db.rows('SELECT operation,category,status,description FROM browser_use_actions WHERE task_id=? ORDER BY seq',(tid,))
        menu=[a for a in actions if a['operation']=='click_selector' and json.loads(a['description']).get('control')=='Categories']
        assert len(menu)==3 and all(a['category']=='read' and a['status']=='completed' for a in menu),menu
        blocked=[a for a in actions if a['status']=='blocked'];assert len(blocked)==1 and 'matching submission' in json.loads(blocked[0]['description'])['blocked_reason']
        assert db.one("SELECT status FROM browser_use_actions WHERE id='legacy-1'")['status']=='executed'
        assert db.one("SELECT status FROM browser_use_actions WHERE id='legacy-2'")['status']=='unknown'
        assert page.url=='https://navigation.fixture.test/overview' and host.submits==0
        report.update(passed=True,actual_browser_use_worker=True,legacy_navigation_repeated_without_approval=True,
                      uncertain_booking_repeat_blocked=True,navigation_allowed_after_uncertainty=True,unsafe_edit_blocked=True,
                      legacy_receipts_preserved=True,submissions=host.submits)
    finally:
        await manager.close();await host.close();hm.public_url=old_url;tm.public_url=old_task_url;asyncio.create_subprocess_exec=spawn;config.RUNTIME=runtime
        (runtime/'navigation-guard.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))

if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
