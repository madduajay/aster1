"""Actual Browser Use cross-origin observation and private takeover, zero provider calls."""
import asyncio,json,os,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

VENUE='''<title>Reservation fixture</title><main><h1>Sample restaurant</h1><p>Unfortunately we cannot accommodate that reservation</p><iframe src="https://identity.fixture.test/login" style="width:650px;height:500px"></iframe></main>'''
LOGIN='''<title>Website login</title><body><h1>Continue with phone</h1><input type=tel aria-label="Mobile phone" id=phone><button onclick="document.body.innerHTML='<h1>Verify phone</h1><input autocomplete=one-time-code id=otp aria-label=Verification><button id=verify>Verify</button>';document.querySelector('#verify').onclick=()=>{if(document.querySelector('#otp').value==='654321')document.body.innerHTML='<h1>Signed in successfully</h1><p>Continue your reservation</p>'}">Send code</button></body>'''

async def child():
    sys.path.insert(0,str(ROOT/'personal_agent'))
    from browser_use import Browser
    from browser_use_worker import observe,preserve_application_dialogs
    from browser_recovery import input_need
    preserve_application_dialogs()
    init=json.loads(sys.stdin.readline());browser=Browser(cdp_url=init['endpoint'],keep_alive=True,cross_origin_iframes=True,enable_default_extensions=False,captcha_solver=False,permissions=[])
    try:
        await browser.start()
        for expected in ['information','manual',None]:
            state=await observe(browser);need=input_need(state)
            assert (need or {}).get('kind')==expected, json.dumps({'expected':expected,'state':state})[:5000]
            assert 'cannot accommodate' not in state['text'],state['text']
            assert state.get('active_frame_url')=='https://identity.fixture.test/login'
            print('OBSERVED '+json.dumps({'kind':expected,'text':state['text'],'frames':len(state.get('frames',[]))}),flush=True)
            if expected is not None:await asyncio.to_thread(sys.stdin.readline)
    finally:await browser.stop()

async def parent():
    from personal_agent import config,browser_host as host_module
    from personal_agent.db import Database
    from personal_agent.browser_host import BrowserHost
    from personal_agent.browser_tasks import BrowserTasks
    from personal_agent.browser_policy import action_binding
    from unittest.mock import AsyncMock
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(config.RUNTIME/'playwright')
    runtime=config.RUNTIME;config.RUNTIME=Path(tempfile.mkdtemp(prefix='recovery-controls-',dir=runtime));config.prepare()
    db=Database();db.seed();users=db.rows('SELECT id FROM users ORDER BY username');user=users[0]['id'];sid=db.new_session(user);run='recovery-control';tid='recovery-task'
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(run,user,sid))
    original=host_module.public_url
    async def fixture_only(url):
        from urllib.parse import urlsplit
        if urlsplit(url).hostname not in {'booking.fixture.test','identity.fixture.test'}:await original(url)
    host_module.public_url=fixture_only
    host=BrowserHost(db);manager=BrowserTasks(db,host,None,None);proc=None
    report={'passed':False,'model_calls':0,'real_site_mutations':0}
    try:
        host.set_scope(user,run,['booking.fixture.test','identity.fixture.test']);host.run_for_user[user]=run
        context=await host.context(user)
        async def route(r):await r.fulfill(content_type='text/html',body=LOGIN if 'identity.' in r.request.url else VENUE)
        await context.route('https://**.fixture.test/**',route)
        page=context.pages[0];await page.goto('https://booking.fixture.test/venue');await page.frame_locator('iframe').locator('#phone').wait_for()
        await page.frame_locator('iframe').locator('#phone').focus()
        client=await context.new_cdp_session(page)
        target=(await client.send('Target.getTargetInfo'))['targetInfo']['targetId'];await client.detach()
        host.set_view(user,run,{'url':page.url,'target_id':target})
        db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES(?,?,?,?,?,?,0,'input',0)",(tid,user,run,'fixture-call','Complete the reservation','["booking.fixture.test","identity.fixture.test"]'))
        observed=await host.snapshot(user,run)
        params={'kind':'manual','prompt':'Complete website login'};aid='private-manual'
        description={'kind':'manual','prompt':'Complete website login','task_id':tid,'action_id':aid,'url':page.url}
        db.execute("INSERT INTO browser_use_actions(id,task_id,seq,operation,category,status,description,fingerprint,duplicate_key,before_state) VALUES(?,?,1,'request_user','input','proposed',?,'','','{}')",(aid,tid,json.dumps(description)))
        db.execute('UPDATE browser_use_actions SET before_state=? WHERE id=?',(json.dumps(observed),aid))
        db.execute("UPDATE runs SET status='input',approval_id='manual-id',approval=? WHERE id=?",(json.dumps({'type':'browser_input','action':description}),run))
        proc=await asyncio.create_subprocess_exec(str(runtime/'browser-use-venv/bin/python'),__file__,'--child',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env={**os.environ,'ANONYMIZED_TELEMETRY':'false','BROWSER_USE_CLOUD_SYNC':'false','BROWSER_USE_LOGGING_LEVEL':'critical','BROWSER_USE_CONFIG_DIR':str(config.RUNTIME/'config')})
        proc.stdin.write((json.dumps({'endpoint':await host.cdp_endpoint(user)})+'\n').encode());await proc.stdin.drain()
        async def read():
            async with asyncio.timeout(35):
                while line:=await proc.stdout.readline():
                    if line.startswith(b'OBSERVED '):return json.loads(line[9:])
                raise AssertionError((await proc.stderr.read()).decode()[-6000:])
        assert (await read())['kind']=='information'
        await page.frame_locator('iframe').locator('#phone').fill('+1 202 555 0100');await page.frame_locator('iframe').get_by_role('button',name='Send code').click()
        await page.frame_locator('iframe').locator('#otp').wait_for();await page.frame_locator('iframe').locator('#otp').focus()
        proc.stdin.write(b'next\n');await proc.stdin.drain();assert (await read())['kind']=='manual'
        assert not await host.manual_ready(user,run)
        for wronguser,wrongid in [(users[1]['id'],'manual-id'),(user,'old-id')]:
            try:await host.manual_action(wronguser,run,wrongid,'type',text='654321')
            except LookupError:pass
            else:raise AssertionError('Unowned or stale takeover was permitted')
        await host.manual_action(user,run,'manual-id','type',text='654321')
        assert await page.frame_locator('iframe').locator('#otp').input_value()=='654321'
        box=await page.frame_locator('iframe').locator('#verify').bounding_box()
        await host.manual_action(user,run,'manual-id','click',x=box['x']+box['width']/2,y=box['y']+box['height']/2)
        assert await host.manual_ready(user,run)
        proc.stdin.write(b'next\n');await proc.stdin.drain();assert (await read())['kind'] is None
        await asyncio.wait_for(proc.wait(),10);assert proc.returncode==0
        dump=' '.join(r['payload'] for r in db.rows('SELECT payload FROM events'))+' '.join(r['content'] for r in db.rows('SELECT content FROM messages'))+' '.join(str(r) for r in db.rows('SELECT * FROM browser_use_actions'))
        assert '654321' not in dump
        db.execute("UPDATE runs SET status='completed' WHERE id=?",(run,))
        try:await host.manual_action(user,run,'manual-id','key',key='Enter')
        except LookupError:pass
        else:raise AssertionError('Manual control remained available after pause')
        report.update(passed=True,cross_origin_frames=True,active_frame_not_background=True,phone_detected=True,otp_manual_handoff=True,direct_private_entry=True,secret_not_persisted=True,stale_and_cross_user_rejected=True,manual_controls_revoked=True)
    finally:
        if proc and proc.returncode is None:proc.kill();await proc.wait()
        await host.close();host_module.public_url=original;config.RUNTIME=runtime
        (runtime/'recovery-controls.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))

if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
