"""Real main/Browser Use models against isolated synthetic PoC browser flows."""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from personal_agent import config,browser_host as hm,browser_tasks as tm
from personal_agent.browser_host import BrowserHost
from personal_agent.browser_tasks import BrowserTasks
from personal_agent.agent import Engine
from personal_agent.db import Database
from personal_agent.memory import Memory
from personal_agent.service import Service
from validation.navigation_acceptance import NoSummaryCalls


FORM='''<title>Application form</title><main><h1>Application form</h1><p>You can fill out this form yourself. Nothing is sent until Submit.</p>
<form method=post onsubmit="event.preventDefault();window.sent=(window.sent||0)+1;document.body.innerHTML='<h1>Contact request received: Alex</h1>'">
<label>First name<input id=first name=first required oninput=preview()></label>
<button type=button onclick="document.getElementById('sizes').hidden=false">Choose size</button>
<div id=sizes hidden><label><input type=radio name=size value=Medium onchange=preview()>Medium</label></div>
<label>Color<select id=color onchange=preview()><option value=''>Choose color</option><option>Blue</option><option>Red</option></select></label>
<label>Date<select id=date onchange=preview()><option value=''>Choose date</option><option>December 1</option><option>December 2</option></select></label>
<label>Mobile phone number<input type=tel id=phone required></label><button type=submit>Submit contact request</button></form>
<form><label>Optional account password<input type=password autocomplete=new-password></label></form>
<output id=preview>Nothing selected</output></main>
<script>window.sent=0;function preview(){document.getElementById('preview').textContent='Preview: '+document.getElementById('first').value+', '+(document.querySelector('input[name=size]:checked')?.value||'')+', '+document.getElementById('color').value+', '+document.getElementById('date').value}</script>'''
SUBMIT=FORM.replace('<input type=tel id=phone required>','<input type=tel id=phone>')


class Host(BrowserHost):
    async def context(self,user):
        fresh=user not in self.contexts;ctx=await super().context(user)
        if fresh:
            async def fixture(route):
                url=route.request.url
                if '/start' in url:body='<title>Provider entry</title><main><h1>Provider entry</h1><a href="https://provider.fixture.test/application">Open application with our provider</a></main>'
                elif '/contact' in url:body=SUBMIT
                else:body=FORM
                await route.fulfill(status=200,content_type='text/html',body=body)
            await ctx.route('https://*.fixture.test/**',fixture)
        return ctx


class CappedTasks(BrowserTasks):
    async def send(self,tid,payload):
        if 'key' in payload:payload={**payload,'model_call_limit':20,'max_steps':20}
        return await super().send(tid,payload)


async def main(only_submission=False):
    production=config.RUNTIME;oldhost=hm.public_url;oldtask=tm.public_url
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(production/'playwright')
    report={'status':'running','cases':[]};output=production/('browser-poc-submission-acceptance.json' if only_submission else 'browser-poc-acceptance.json')
    def save():output.write_text(json.dumps(report,indent=2));output.chmod(0o600)
    async def public_fixture(url):
        if urlsplit(url).hostname not in {'forms.fixture.test','provider.fixture.test'}:await oldhost(url)
    hm.public_url=tm.public_url=public_fixture;key=config.keychain_key();save()
    try:
        with tempfile.TemporaryDirectory(prefix='aster-browser-poc-') as temporary:
            config.RUNTIME=Path(temporary);db=Database();db.seed();uid=db.one("SELECT id FROM users WHERE username='user01'")['id'];host=Host(db,headless=True)
            async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'checkpoints.sqlite')) as saver:
                engine=Engine(db,host,saver,key);engine.browser_tasks=CappedTasks(db,host,key,engine.verification_model)
                service=Service(db,engine);service.memory=Memory(db,NoSummaryCalls())
                try:
                    cases=[('open-form','Open https://forms.fixture.test/application so I can fill it out myself. Do not enter personal details.','navigation'),
                           ('ordinary-controls','At https://forms.fixture.test/controls enter first name Alex, choose size Medium, color Blue and date December 2. These are the only fields to prepare. Do not submit or create an account.','preparation'),
                           ('provider-link','Open https://forms.fixture.test/start and follow its application link to the provider. Stop once the provider application form is open.','provider'),
                           ('one-submission','At https://forms.fixture.test/contact enter first name Alex and submit the contact request once. Do not fill any other fields or create an account.','submission')]
                    if only_submission:cases=[c for c in cases if c[2]=='submission']
                    for name,prompt,kind in cases:
                        sid=db.new_session(uid);started=time.monotonic();rid=(await service.submit(uid,sid,prompt))['run_id'];seen=set();approvals=[]
                        while True:
                            state=db.one('SELECT * FROM runs WHERE id=?',(rid,))
                            if state['status'] in {'completed','failed','cancelled','interrupted'}:break
                            if state['status']=='input':raise AssertionError('Unexpected input pause')
                            if state['status']=='approval' and state['approval_id'] not in seen:
                                desc=json.loads(state['approval'])['action']
                                assert kind=='submission' and desc.get('control')=='Submit contact request',desc
                                seen.add(state['approval_id']);approvals.append(desc['control'])
                                await service.resume(uid,rid,'approve',state['approval_id'])
                            if time.monotonic()-started>210:raise TimeoutError('Browser acceptance timeout')
                            await asyncio.sleep(.1)
                        task=db.one('SELECT * FROM browser_use_tasks WHERE run_id=?',(rid,));assert task,'Main did not delegate to Browser Use'
                        result=json.loads(task['result'] or '{}');assert task['status']=='completed' and result.get('status')=='verified',result
                        page=await host.view_page(uid,rid)
                        if kind=='preparation':
                            text=await page.locator('#preview').inner_text();assert text=='Preview: Alex, Medium, Blue, December 2',text
                        if kind=='provider':assert urlsplit(page.url).hostname=='provider.fixture.test'
                        if kind=='submission':assert await page.evaluate('window.sent')==1 and len(approvals)==1
                        else:assert not approvals
                        case={'case':name,'status':'passed','seconds':round(time.monotonic()-started,2),'browser_calls':task['model_calls'],'approvals':len(approvals),'verification':result['verification']}
                        report['cases'].append(case);save();print(json.dumps({k:v for k,v in case.items() if k!='verification'}),flush=True)
                    report['status']='passed';save()
                finally:await service.close();await host.close();await engine.client.close()
    except Exception as exc:report['status']='failed';report['error_class']=type(exc).__name__;save();raise
    finally:config.RUNTIME=production;hm.public_url=oldhost;tm.public_url=oldtask
    print(json.dumps({'status':report['status'],'report':str(output)}),flush=True)


if __name__=='__main__':
    import sys
    asyncio.run(main(only_submission='--submission' in sys.argv))
