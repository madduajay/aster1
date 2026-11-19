"""Real guarded Browser Use recovery, changed approvals, uncertain dispatch, no paid calls."""
import asyncio,functools,json,os,sys,tempfile,time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
HTML='''<main><h1>Controlled booking</h1><p id=price>Price: $80</p><button id=confirm onclick="window.submits++;document.querySelector('output').textContent='Reservation confirmed at '+document.querySelector('#price').textContent">Confirm booking</button><output>No booking yet</output></main><script>window.submits=0</script>'''

async def child():
    import browser_use
    class Planner:
        def __init__(self,**kw):self.browser=kw['browser'];self.tools=kw['tools'];self.mode=kw['task'].split()[0];self.state=SimpleNamespace(n_steps=0)
        def stop(self):pass
        async def run(self,**kw):
            await self.browser.start();model=self.tools.registry.create_action_model()
            async def act(name,params):return await self.tools.act(model.model_validate({name:params}),self.browser)
            if self.mode=='wait':
                import browser_use_worker as worker
                from browser_diagnostics import diagnostic_stage
                original_observe=worker.observe
                async def fail_snapshot(*args,**kwargs):
                    with diagnostic_stage('page_snapshot'):
                        raise TypeError('Controlled snapshot exception')
                worker.observe=fail_snapshot
                try:result=await act('wait',{'seconds':0})
                finally:worker.observe=original_observe
                assert result.error and 'TypeError' in result.error and 'stale target' not in result.error
                result=await act('wait',{'seconds':0});assert not result.error and not result.is_done
                return SimpleNamespace(is_successful=lambda:False,final_result=lambda:'Read recovered without submitting.')
            if self.mode=='limit':
                for i in range(3):
                    result=await act('inspect_selector',{'selector':'#absent'})
                    assert bool(result.is_done)==(i==2)
                return SimpleNamespace(is_successful=lambda:False,final_result=lambda:'Stopped after repeated stale-control recovery.')
            result=await act('inspect_selector',{'selector':'#absent'});assert result.error and not result.is_done
            await act('inspect_selector',{'selector':'#confirm'})
            if self.mode=='changed':
                result=await act('click_selector',{'selector':'#confirm'});assert result.error and not result.is_done
                await act('inspect_selector',{'selector':'#confirm'})
                result=await act('click_selector',{'selector':'#confirm'});assert not result.error
            else:
                registered=self.tools.registry.registry.actions['click_selector'];original=registered.function
                @functools.wraps(original)
                async def fail_after(*a,**kw):
                    await original(*a,**kw)
                    raise ConnectionError('Synthetic transport loss after submission')
                registered.function=fail_after
                result=await act('click_selector',{'selector':'#confirm'});assert result.error and not result.is_done
                result=await act('click_selector',{'selector':'#confirm'});assert result.error and not result.is_done
                registered.function=original
                result=await act('inspect_selector',{'selector':'output'});assert not result.error
            return SimpleNamespace(is_successful=lambda:True,final_result=lambda:'Observed confirmation after recovery.')
    browser_use.Agent=Planner;sys.path.insert(0,str(ROOT/'personal_agent'));from browser_use_worker import main
    await main()

async def parent():
    from personal_agent import config,browser_host as hm,browser_tasks as tm
    from personal_agent.browser_host import BrowserHost
    from personal_agent.browser_tasks import BrowserTasks,GoalCheck
    from personal_agent.db import Database
    runtime=config.RUNTIME;original=hm.public_url;oldt=tm.public_url;spawn=asyncio.create_subprocess_exec
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright');config.RUNTIME=Path(tempfile.mkdtemp(prefix='recovery-boundary-',dir=runtime));config.prepare()
    db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id'];sid=db.new_session(user)
    async def allow(url):
        if urlsplit(url).hostname!='booking.fixture.test':await original(url)
    async def child_spawn(*args,**kw):
        if len(args)>1 and args[1]==str(ROOT/'personal_agent/browser_use_worker.py'):args=(args[0],__file__,'--child',*args[2:])
        return await spawn(*args,**kw)
    class Host(BrowserHost):
        async def context(self,user):
            fresh=user not in self.contexts;c=await super().context(user)
            if fresh:
                async def route(r):await r.fulfill(content_type='text/html',body=HTML)
                await c.route('https://booking.fixture.test/**',route)
            return c
    class Verifier:
        def with_structured_output(self,*a,**kw):return self
        async def ainvoke(self,messages):
            data=json.loads(messages[-1].content);success='Reservation confirmed' in data['page']['text']
            return GoalCheck(success=success,reason='Observed confirmation.' if success else 'No booking was submitted.',evidence_ids=[0])
    hm.public_url=tm.public_url=allow;asyncio.create_subprocess_exec=child_spawn;host=Host(db);manager=BrowserTasks(db,host,'sk-controlled',Verifier());report={'passed':False,'model_calls':0,'real_site_mutations':0}
    try:
        results={}
        for mode in ['changed','uncertain','limit','wait']:
            run=mode;db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(run,user,sid))
            page=(await host.context(user)).pages[0];await page.goto('https://booking.fixture.test/venue')
            tid=await manager.start(user,run,'call-'+mode,mode+' Complete the controlled booking',['booking.fixture.test']);approvals=0
            end=time.monotonic()+60
            while time.monotonic()<end:
                row=manager.owned(user,run,tid)
                if row['status'] in {'completed','interrupted'} and tid not in manager.workers:break
                for proposal in db.rows("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,)):
                    approvals+=1
                    if mode=='changed' and approvals==1:await page.locator('#price').evaluate("e=>e.textContent='Price: $90'")
                    await manager.decide(user,run,tid,proposal['id'],'approve')
                await asyncio.sleep(.05)
            row=manager.owned(user,run,tid);result=json.loads(row['result'] or '{}')
            assert row['status']=='completed',row
            count=await page.evaluate('window.submits');results[mode]={'submissions':count,'approvals':approvals,'status':result['status'],'recovery_attempts':len(result.get('recovery_attempts',[]))}
            if mode=='changed':
                assert count==1 and approvals==2 and result['status']=='verified',results[mode]
                assert db.one("SELECT id FROM browser_use_actions WHERE task_id=? AND status='invalidated'",(tid,))
            elif mode=='uncertain':
                assert count==1 and approvals==1 and result['status']=='verified',results[mode]
                assert db.one("SELECT id FROM browser_use_actions WHERE task_id=? AND category='approval' AND status='verified'",(tid,))
            elif mode=='limit':assert count==0 and approvals==0 and result['status']=='unverified' and len(result['recovery_attempts'])==3,results[mode]
            else:
                assert count==0 and approvals==0 and result['status']=='unverified' and len(result['recovery_attempts'])==1,results[mode]
                diagnostic=result['diagnostics'][0]
                assert diagnostic['operation']=='wait' and diagnostic['error_class']=='TypeError' and diagnostic['observation_stage']=='page_snapshot'
                assert diagnostic['message']=='Controlled snapshot exception' and not diagnostic['dispatched']
            assert result.get('diagnostics')
            assert db.one("SELECT id FROM events WHERE run_id=? AND kind='diagnostic'",(run,))
            db.execute("UPDATE runs SET status='completed' WHERE id=?",(run,))
        report.update(passed=True,scenarios=results,stale_target_recovered=True,price_change_reapproved=True,uncertain_submission_not_repeated=True,uncertain_outcome_resolved_by_evidence=True,recovery_limit_enforced=True,exception_details_persisted=True,wait_error_classified_correctly=True)
    finally:
        await manager.close();await host.close();hm.public_url=original;tm.public_url=oldt;asyncio.create_subprocess_exec=spawn;config.RUNTIME=runtime
        (runtime/'recovery-boundary.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))

if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
