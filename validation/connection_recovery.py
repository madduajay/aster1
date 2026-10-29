"""Disconnect the real worker CDP WebSocket, recover, and submit a fixture once.

Uses isolated profiles/databases and a deterministic planner: no OpenAI calls,
real websites, SMS, purchases, or production browser profiles.
"""
import asyncio,json,os,sys,tempfile,time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
HTML='''<main><h1>Controlled shop</h1><p>Price: $80</p><button id=confirm onclick="window.submits++;document.querySelector('output').textContent='Order confirmed at $80'">Place order</button><output>No order yet</output></main><script>window.submits=0</script>'''


async def child():
    import browser_use
    sys.path.insert(0,str(ROOT/'personal_agent'))
    import browser_use_worker as worker
    class Planner:
        def __init__(self,**kw):self.browser=kw['browser'];self.tools=kw['tools'];self.mode=kw['task'].split()[0];self.state=SimpleNamespace(n_steps=0)
        def stop(self):pass
        async def run(self,**kw):
            await self.browser.start();model=self.tools.registry.create_action_model()
            async def act(name,params):return await self.tools.act(model.model_validate({name:params}),self.browser)
            await worker.observe(self.browser)
            original_target=self.browser.agent_focus_target_id
            await act('inspect_selector',{'selector':'#confirm'})
            if self.mode=='final':
                async def fail_final(*args,**kw):raise ConnectionError('Controlled unavailable final inspection')
                worker.observe=fail_final
                return SimpleNamespace(is_successful=lambda:False,final_result=lambda:'Final observation unavailable.')
            if self.mode=='focus':
                self.browser.agent_focus_target_id=None
                # Model the observed failed focus recovery while the socket is
                # still live. The replacement manager must restore the old tab.
                original_focus=self.browser.session_manager.ensure_valid_focus
                failed=False
                async def fail_focus_once(*args,**kwargs):
                    nonlocal failed
                    if not failed:failed=True;return False
                    return await original_focus(*args,**kwargs)
                self.browser.session_manager.ensure_valid_focus=fail_focus_once
                result=await act('click_selector',{'selector':'#confirm'})
                assert result.error and 'FreshObservationRequired' in result.error,result
            elif self.mode in {'approved','before'}:
                # Lose transport after approval, before the dispatch handshake.
                original=worker._observe;injected=False;calls=0
                async def drop_before_dispatch(browser,params=None):
                    nonlocal injected,calls
                    if params and params.get('selector')=='#confirm':
                        calls+=1
                        if calls==(2 if self.mode=='approved' else 1) and not injected:
                            injected=True
                            await browser.cdp_client.ws.close()
                            raise ConnectionError('Controlled socket loss after approval, before dispatch')
                    return await original(browser,params)
                worker._observe=drop_before_dispatch
                try:result=await act('click_selector',{'selector':'#confirm'})
                finally:worker._observe=original
                assert injected and result.error and 'FreshObservationRequired' in result.error,result
            else:
                # The actual WebSocket is closed. Invoke automatic and manual
                # recovery concurrently, as happened in the original failure.
                await self.browser.cdp_client.ws.close()
                await asyncio.gather(self.browser.reconnect(),self.browser.reconnect())
                assert self.browser._transport_recovery.generation==1
            assert self.browser.agent_focus_target_id==original_target
            await act('inspect_selector',{'selector':'#confirm'})
            if self.mode=='uncertain':
                registered=self.tools.registry.registry.actions['click_selector'];original=registered.function
                import functools
                @functools.wraps(original)
                async def execute_then_disconnect(*args,**kw):
                    await original(*args,**kw)
                    await self.browser.cdp_client.ws.close()
                    raise ConnectionError('Controlled loss after order submission')
                registered.function=execute_then_disconnect
                try:result=await act('click_selector',{'selector':'#confirm'})
                finally:registered.function=original
                assert result.error
                assert self.tools.uncertain  # Approval category must reach the worker.
                # Reattach and read, then demonstrate duplicate submission is
                # still forbidden despite the now-healthy connection.
                await worker.observe(self.browser)
                result=await act('click_selector',{'selector':'#confirm'})
                assert result.error and 'uncertain' in result.error,result
            elif self.mode=='reject':
                result=await act('click_selector',{'selector':'#confirm'})
                assert result.is_done and not result.success
            else:
                result=await act('click_selector',{'selector':'#confirm'})
                assert not result.error,result
            return SimpleNamespace(is_successful=lambda:False,final_result=lambda:'Inspect the fixture outcome independently.')
    browser_use.Agent=Planner
    await worker.main()


async def parent():
    from personal_agent import config,browser_host as hm,browser_tasks as tm
    from personal_agent.browser_host import BrowserHost
    from personal_agent.browser_tasks import BrowserTasks,GoalCheck
    from personal_agent.db import Database
    runtime=config.RUNTIME;original=hm.public_url;oldt=tm.public_url;spawn=asyncio.create_subprocess_exec
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright')
    config.RUNTIME=Path(tempfile.mkdtemp(prefix='connection-recovery-',dir=runtime));config.prepare()
    db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id'];sid=db.new_session(user)
    async def allow(url):
        if urlsplit(url).hostname!='shop.fixture.test':await original(url)
    async def child_spawn(*args,**kw):
        if len(args)>1 and args[1]==str(ROOT/'personal_agent/browser_use_worker.py'):
            args=(args[0],__file__,'--child',*args[2:])
            with (config.RUNTIME/'fixture-worker.log').open('ab') as log:
                kw['stderr']=log
                return await spawn(*args,**kw)
        return await spawn(*args,**kw)
    class Host(BrowserHost):
        async def context(self,user):
            fresh=user not in self.contexts;c=await super().context(user)
            if fresh:
                async def route(r):await r.fulfill(content_type='text/html',body=HTML)
                await c.route('https://shop.fixture.test/**',route)
            return c
    class Verifier:
        def with_structured_output(self,*a,**kw):return self
        async def ainvoke(self,messages):
            data=json.loads(messages[-1].content);success='Order confirmed' in data['page']['text']
            return GoalCheck(success=success,reason='Observed confirmation.' if success else 'No order was submitted.',evidence_ids=[0])
    hm.public_url=tm.public_url=allow;asyncio.create_subprocess_exec=child_spawn
    host=Host(db);manager=BrowserTasks(db,host,'sk-controlled',Verifier())
    report={'passed':False,'model_calls':0,'real_site_mutations':0,'scenarios':{}}
    try:
        for mode in ['disconnect','before','focus','approved','uncertain','reject','final']:
            db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(mode,user,sid))
            page=(await host.context(user)).pages[0];await page.goto('https://shop.fixture.test/item')
            tid=await manager.start(user,mode,'call-'+mode,mode+' Complete the controlled order',['shop.fixture.test']);approvals=0
            end=time.monotonic()+95
            while time.monotonic()<end:
                row=manager.owned(user,mode,tid)
                if row['status'] in {'completed','interrupted'} and tid not in manager.workers:break
                for proposal in db.rows("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed'",(tid,)):
                    approvals+=1
                    await manager.decide(user,mode,tid,proposal['id'],'reject' if mode=='reject' else 'approve')
                await asyncio.sleep(.05)
            row=manager.owned(user,mode,tid);result=json.loads(row['result'] or '{}')
            count=await page.evaluate('window.submits')
            report['scenarios'][mode]={'task_status':row['status'],'submissions':count,'approvals':approvals,'result_status':result.get('status')}
            assert row['status']=='completed',report['scenarios'][mode]
            if mode in {'final','reject'}:
                assert count==0 and result['status']==('rejected' if mode=='reject' else 'unverified'),report['scenarios'][mode]
            else:
                assert count==1 and result['status']=='verified',report['scenarios'][mode]
                assert approvals==(2 if mode=='approved' else 1),report['scenarios'][mode]
            if mode=='approved':assert db.one("SELECT id FROM browser_use_actions WHERE task_id=? AND status='invalidated'",(tid,))
            if mode=='final':assert result['diagnostics'][-1]['stage']=='final_observation'
            db.execute("UPDATE runs SET status='completed' WHERE id=?",(mode,))
        report['passed']=True
    finally:
        await manager.close();await host.close();hm.public_url=original;tm.public_url=oldt;asyncio.create_subprocess_exec=spawn;config.RUNTIME=runtime
        (runtime/'connection-recovery.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
