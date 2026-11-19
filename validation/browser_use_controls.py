"""Real Browser Use/CDP control checks, no API key or paid model calls."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def child():
    sys.path.insert(0,str(ROOT/'personal_agent'))
    from browser_use_worker import selector_node,observe,preserve_application_dialogs
    from browser_policy import action_binding,classify_action
    from browser_use import Browser
    preserve_application_dialogs()
    init=json.loads(sys.stdin.readline())
    browser=Browser(cdp_url=init['endpoint'],keep_alive=True,enable_default_extensions=False,captcha_solver=False,permissions=[])
    try:
        await browser.start()
        cdp=await browser.get_or_create_cdp_session()
        await cdp.cdp_client.send.Runtime.evaluate(params={'expression':"document.querySelector('main').scrollIntoView({block:'start'})",'awaitPromise':True},session_id=cdp.session_id)
        element,node=await selector_node(browser,'label[for=s10]')
        assert node['node']['role']=='radio' and not node['node']['disabled']
        params={'selector':'label[for=s10]'}
        before=await observe(browser,params);again=await observe(browser,params)
        assert action_binding('click_selector',params,before)==action_binding('click_selector',params,again)
        await element.click()
        selected=await observe(browser,params)
        assert selected['node']['checked'] is True
        assert action_binding('click_selector',params,before)!=action_binding('click_selector',params,selected)
        _,disabled=await selector_node(browser,'label[for=s11]')
        assert disabled['node']['disabled']
        try:await selector_node(browser,'label')
        except ValueError:pass
        else:raise AssertionError('Nonunique selector accepted')
        query,_=await selector_node(browser,'#search-check')
        await query.click()
        focused=await observe(browser,{'_focused':True})
        assert focused['node']['type']=='search' and focused['node']['value']=='blue shoes'
        assert classify_action('send_keys',{'keys':'ENTER'},focused)[0]=='preparation'
        for _ in range(2):
            button,_=await selector_node(browser,'#native-confirm')
            await button.click()
        print('CONTROL_PASS',flush=True)
        await asyncio.Event().wait()  # Parent verifies hard cancellation, then closes only this profile.
    finally:await browser.stop()


async def parent():
    from personal_agent import config,browser_host as host_module
    from personal_agent.db import Database
    from personal_agent.browser_tasks import BrowserTasks,Worker
    from validation.browser_fixtures import FixtureBrowsers
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(config.RUNTIME/'playwright')
    runtime=config.RUNTIME;original=host_module.public_url
    config.RUNTIME=Path(tempfile.mkdtemp(prefix='browser-use-controls-',dir=runtime));config.prepare()
    db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id']
    sid=db.new_session(user);run='controls';tid='control-task'
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
    async def allow_fixture(url):
        from urllib.parse import urlsplit
        if urlsplit(url).hostname!='shop.example':await original(url)
    host_module.public_url=allow_fixture
    host=FixtureBrowsers(db,headless=True);manager=BrowserTasks(db,host,None,None)
    report={'passed':False,'model_calls':0,'real_site_mutations':0}
    try:
        context=await host.context(user);page=context.pages[0]
        await page.goto('https://shop.example/product')
        endpoint=await host.cdp_endpoint(user)
        from urllib.parse import urlsplit
        listener=await asyncio.create_subprocess_exec('/usr/sbin/lsof','-nP','-iTCP:'+str(urlsplit(endpoint).port),'-sTCP:LISTEN','-Fn',stdout=asyncio.subprocess.PIPE)
        sockets=(await listener.communicate())[0].decode().splitlines()
        addresses=[line[1:] for line in sockets if line.startswith('n')]
        assert addresses and all(address.startswith(('127.0.0.1:','[::1]:')) for address in addresses)
        await page.evaluate('''() => {const b=document.createElement('button');b.id='native-confirm';b.textContent='Confirm fixture';b.onclick=()=>{if(confirm('Confirm synthetic action?'))window.accepted=(window.accepted||0)+1;else window.rejected=(window.rejected||0)+1};document.querySelector('main').append(b)}''')
        await page.evaluate('''() => {const f=document.createElement('form');f.role='search';f.method='get';const q=document.createElement('input');q.type='search';q.id='search-check';q.value='blue shoes';q.setAttribute('aria-label','Search products');f.append(q);document.querySelector('main').append(f)}''')
        proc=await asyncio.create_subprocess_exec(str(runtime/'browser-use-venv/bin/python'),__file__,'--child',
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True,
            env={**os.environ,'ANONYMIZED_TELEMETRY':'false','BROWSER_USE_CLOUD_SYNC':'false','BROWSER_USE_LOGGING_LEVEL':'critical','BROWSER_USE_CONFIG_DIR':str(config.RUNTIME/'config')})
        proc.stdin.write((json.dumps({'endpoint':endpoint})+'\n').encode());await proc.stdin.drain()
        lock=asyncio.Lock();await lock.acquire()
        manager.workers[tid]=Worker(proc,'controls-nonce',user,run,lock)
        db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES(?,?,?,?,?,? ,0,'running',?)",(tid,user,run,'controls-call','Test native confirmation','["shop.example"]',time.time()))
        manager.workers[tid].monitor=asyncio.create_task(manager.watch_dialogs(tid))
        decisions=[]
        async def output():
            while line:=await proc.stdout.readline():
                if b'CONTROL_PASS' in line:return
            raise AssertionError('Library control process exited before completing: '+(await proc.stderr.read()).decode()[-2000:])
        output_task=asyncio.create_task(output())
        end=time.monotonic()+45
        while not output_task.done() and time.monotonic()<end:
            row=db.one("SELECT id FROM browser_use_actions WHERE task_id=? AND status='proposed' ORDER BY rowid LIMIT 1",(tid,))
            if row:
                # Let the native watchdog run: it must not auto-accept while the app waits.
                await asyncio.sleep(.4)
                assert not output_task.done() and page in host.dialogs
                decision='approve' if not decisions else 'reject'
                await manager.decide(user,run,tid,row['id'],decision);decisions.append(decision)
            await asyncio.sleep(.05)
        await asyncio.wait_for(output_task,1)
        assert decisions==['approve','reject']
        assert await page.evaluate('window.accepted')==1 and await page.evaluate('window.rejected')==1
        assert await page.evaluate('window.addCount')==0 and await page.evaluate('window.purchaseCount')==0
        await manager.cancel_run(user,run)
        assert proc.returncode is not None and not lock.locked() and user not in host.contexts
        assert (config.RUNTIME/'browser-profiles'/user).is_dir()
        report.update(passed=True,actor_hidden_label=True,nonunique_selector_rejected=True,native_autoaccept_disabled=True,
                      separate_native_approve_reject=True,worker_cancelled=True,profile_preserved=True,cdp_loopback_only=True,keyboard_focus_observed=True,search_is_preparation=True)
    finally:
        await manager.close();await host.close()
        host_module.public_url=original;config.RUNTIME=runtime
        (runtime/'browser-use-controls.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':asyncio.run(child() if '--child' in sys.argv else parent())
