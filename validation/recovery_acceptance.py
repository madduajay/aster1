"""Capped live model acceptance; only synthetic website mutations."""
import asyncio,json,os,tempfile,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from urllib.parse import urlsplit
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from personal_agent import config,browser_host as hm,browser_tasks as tm
from personal_agent.browser_host import BrowserHost
from personal_agent.browser_tasks import BrowserTasks
from personal_agent.db import Database
from personal_agent.agent import Engine
from validation.recovery_controls import VENUE,LOGIN

class Host(BrowserHost):
    async def context(self,user):
        fresh=user not in self.contexts;context=await super().context(user)
        if fresh:
            async def route(r):
                login=LOGIN.replace("<p>Continue your reservation</p>","<p>Continue your reservation for 2 guests on October 28, 2026 at 5:00 PM.</p><button onclick=book()>Confirm reservation</button>")
                login=login.replace('<title>Website login</title>',"<title>Website login</title><script>window.bookCount=0;function book(){window.bookCount++;document.body.innerHTML='<h1>Reservation confirmed</h1><p>Sample restaurant, 2 guests, October 28, 2026, 5:00 PM. Confirmation TEST-123.</p>'}</script>")
                await r.fulfill(content_type='text/html',body=login if 'identity.' in r.request.url else VENUE)
            await context.route('https://**.fixture.test/**',route)
        return context

class CappedTasks(BrowserTasks):
    async def send(self,tid,payload):
        if 'model_call_limit' in payload:payload['model_call_limit']=18;payload['max_steps']=18
        await super().send(tid,payload)

async def main():
    runtime=config.RUNTIME;os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright')
    original=hm.public_url;oldtask=tm.public_url;host=manager=engine=None
    report={'passed':False,'live_browser_model':config.BROWSER_MODEL,'browser_model_call_cap':18,'real_site_mutations':0}
    try:
        key=config.keychain_key();config.RUNTIME=Path(tempfile.mkdtemp(prefix='recovery-acceptance-',dir=runtime));config.prepare()
        db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id'];sid=db.new_session(user);run='recovery-acceptance'
        db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(run,user,sid))
        async def fixture_url(url):
            if urlsplit(url).hostname not in {'booking.fixture.test','identity.fixture.test'}:await original(url)
        hm.public_url=tm.public_url=fixture_url;host=Host(db)
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'graph.sqlite')) as saver:
            engine=Engine(db,host,saver,key);manager=CappedTasks(db,host,key,engine.model);del key
            task='Open https://booking.fixture.test/venue and complete the synthetic reservation at Sample restaurant for 2 guests on October 28, 2026 at 5:00 PM. The booking flow is inside the visible login iframe. Request the missing phone number from the user using request_user; sending the verification SMS needs application approval. Use request_user(kind=manual) for OTP verification, never ask for or type the code yourself. After the user finishes login, continue the same goal and request application approval before confirming the reservation. Verify the actual confirmation. Do not infer failure from background messages.'
            tid=await manager.start(user,run,'acceptance-call',task,['booking.fixture.test','identity.fixture.test'])
            end=time.monotonic()+240;inputs=[];approvals=[]
            while time.monotonic()<end:
                state=manager.owned(user,run,tid)
                if state['status'] in {'completed','interrupted','cancelled'}:break
                for row in db.rows("SELECT * FROM browser_use_actions WHERE task_id=? AND status='proposed' ORDER BY seq",(tid,)):
                    desc=json.loads(row['description'])
                    if row['category']=='input':
                        inputs.append(desc['kind']);print('input',desc['kind'],flush=True)
                        if desc['kind']=='manual':
                            db.execute("UPDATE runs SET status='input',approval_id='acceptance-manual',approval=? WHERE id=?",(json.dumps({'type':'browser_input','action':desc}),run))
                            await host.open_takeover(user,run,'acceptance-manual')
                            page=host.contexts[user].pages[0];await page.frame_locator('iframe').locator('#otp').focus()
                            await host.manual_action(user,run,'acceptance-manual','type',text='654321')
                            box=await page.frame_locator('iframe').locator('#verify').bounding_box();await host.manual_action(user,run,'acceptance-manual','click',x=box['x']+box['width']/2,y=box['y']+box['height']/2)
                            assert await host.manual_ready(user,run)
                            db.execute("UPDATE runs SET status='running' WHERE id=?",(run,));answer='Completed manual website step.'
                        else:answer='+1 202 555 0100'
                        await manager.answer_input(user,run,tid,row['id'],{'type':'browser_input','text':answer})
                    else:
                        approvals.append(desc['control']);print('approval',desc['control'],flush=True)
                        assert any(t in desc['control'].lower() for t in ['send code','confirm reservation','phone','verification']),desc['control']
                        await manager.decide(user,run,tid,row['id'],'approve')
                await asyncio.sleep(.15)
            state=manager.owned(user,run,tid);result=json.loads(state['result'] or '{}');report['result']=result
            page=host.contexts[user].pages[0];frame=page.frames[1]
            report.update(inputs=inputs,approvals=approvals,browser_model_calls=state['model_calls'],independent_verifier_calls=1,booking_count=await frame.evaluate('window.bookCount'),task_status=state['status'])
            assert state['status']=='completed' and result['status']=='verified'
            assert inputs==['information','manual'] and report['booking_count']==1 and state['model_calls']<=18
            assert not db.one("SELECT id FROM browser_use_actions WHERE status='unknown'")
            report['passed']=True
    except Exception as e:report['error_class']=type(e).__name__
    finally:
        if manager:await manager.close()
        if host:await host.close()
        if engine:await engine.client.close()
        config.RUNTIME=runtime;hm.public_url=original;tm.public_url=oldtask
        (runtime/'recovery-acceptance.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='result'}))
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':asyncio.run(main())
