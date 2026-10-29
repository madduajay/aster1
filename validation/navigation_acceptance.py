"""Capped live HTTPS UI -> chat -> Browser Use -> real public homepage checks.

Isolated accounts/databases/profiles. No mutations are approved. Production
accounts, chats and browser profiles are never used.
"""
import asyncio,json,os,secrets,socket,sys,tempfile,time
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import async_playwright,expect
import uvicorn

from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.browser_host import BrowserHost
from personal_agent.browser_tasks import BrowserTasks
from personal_agent.browser_diagnostics import clean_message
from personal_agent.db import Database
from personal_agent.memory import Memory
from personal_agent.service import Service
from personal_agent.sources import allowed_url


class CappedEngine(Engine):
    def graph(self,user,run,context,max_calls=24,max_searches=3):
        return super().graph(user,run,context,max_calls=6,max_searches=0)


class CappedTasks(BrowserTasks):
    async def send(self,tid,payload):
        if 'key' in payload:payload={**payload,'model_call_limit':8,'max_steps':8}
        return await super().send(tid,payload)


class NoSummaryCalls:
    def with_structured_output(self,*args,**kwargs):return self
    async def ainvoke(self,*args,**kwargs):raise RuntimeError('Live summary calls disabled for navigation acceptance')


async def until(check,seconds=25):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if check():return
        await asyncio.sleep(.1)
    raise TimeoutError('Navigation acceptance condition timed out')


async def main():
    runtime,origin=config.RUNTIME,config.ORIGIN
    report={'passed':False,'chat_model':config.MODEL,'browser_model':config.BROWSER_MODEL,
        'chat_call_limit_per_request':6,'browser_call_limit_per_request':8,'cases':[],
        'real_mutations_approved':0,'summary_calls':0}
    service=host=engine=server=server_task=None
    try:
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright')
        key=config.keychain_key()
        config.RUNTIME=Path(tempfile.mkdtemp(prefix='navigation-live-',dir=runtime));config.prepare()
        db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id']
        credentials=dict(line.split() for line in (config.RUNTIME/'initial-accounts.txt').read_text().splitlines())
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
        config.ORIGIN=f'https://aster.localhost:{sock.getsockname()[1]}'
        host=BrowserHost(db,headless=True)
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'graph.sqlite')) as saver:
            engine=CappedEngine(db,host,saver,key)
            engine.browser_tasks=CappedTasks(db,host,key,engine.model);del key
            service=Service(db,engine);service.memory=Memory(db,NoSummaryCalls())
            server=uvicorn.Server(uvicorn.Config(create_app(db,service),ssl_certfile=str(runtime/'tls/server.pem'),ssl_keyfile=str(runtime/'tls/server.key'),log_level='error',access_log=False))
            server_task=asyncio.create_task(server.serve(sockets=[sock]));await until(lambda:server.started)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                page=await browser.new_page(ignore_https_errors=False,viewport={'width':1400,'height':1000})
                errors=[];page.on('pageerror',lambda error:errors.append(type(error).__name__))
                await page.goto(config.ORIGIN)
                await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
                await page.locator('#password-form').wait_for(state='visible');await page.locator('#new-password').fill(secrets.token_urlsafe(24));await page.locator('#new-password').press('Enter')
                await page.locator('#composer').wait_for(state='visible')
                for label,request,domain in [('target','can you show me the main page of target','target.com'),('python','Please open https://www.python.org in the browser panel','python.org')]:
                    if report['cases']:await page.get_by_role('button',name='New Chat',exact=True).click()
                    previous=db.one('SELECT count(*) n FROM runs')['n']
                    await page.locator('#input').fill(request);await page.locator('#input').press('Enter')
                    await until(lambda:db.one('SELECT count(*) n FROM runs')['n']>previous)
                    run=db.one('SELECT id FROM runs ORDER BY created DESC LIMIT 1')['id']
                    end=time.monotonic()+170
                    while time.monotonic()<end:
                        for row in db.rows("SELECT a.id,t.id tid FROM browser_use_actions a JOIN browser_use_tasks t ON t.id=a.task_id WHERE t.run_id=? AND a.status='proposed' AND a.category='approval'",(run,)):
                            await engine.browser_tasks.decide(user,run,row['tid'],row['id'],'reject')
                        if db.one('SELECT status FROM runs WHERE id=?',(run,))['status']!='running' and run not in service.tasks:break
                        await asyncio.sleep(.1)
                    task=db.one('SELECT * FROM browser_use_tasks WHERE run_id=?',(run,))
                    assert task,'The agent did not delegate to Browser Use'
                    result=json.loads(task['result'] or '{}')
                    view=host.view_info(user,run)
                    usage=db.one('SELECT * FROM run_usage WHERE run_id=?',(run,))
                    pending=db.one("SELECT before_state FROM browser_use_actions WHERE task_id=? AND operation='request_user' AND status='proposed' ORDER BY seq DESC LIMIT 1",(task['id'],))
                    observed=json.loads(pending['before_state']) if pending else {}
                    manual=task['status']=='input' and 'human' in observed.get('text','').lower() and 'hold' in observed.get('text','').lower()
                    case={'site':label,'task_status':task['status'],'verified':result.get('status')=='verified','manual_human_verification':manual,'url':urlsplit(view.get('url',''))._replace(query='',fragment='').geturl(),'chat_calls':usage['model_calls'],'searches':usage['searches'],'browser_calls':task['model_calls']}
                    report['cases'].append(case)
                    assert task['status']=='completed' and result.get('status')=='verified' or manual,case
                    assert allowed_url(view['url'],[domain]) and usage['searches']==0 and task['model_calls']<=8
                    await expect(page.locator('#browser-view')).to_be_visible()
                    await expect(page.locator('#browser-url')).to_contain_text(domain)
                    await expect(page.locator('#browser-frame')).not_to_have_js_property('naturalWidth',0)
                    assert not errors
                    await page.screenshot(path=str(runtime/f'navigation-{label}-live.png'))
                    print(json.dumps({'case_completed':case}),flush=True)
                    if manual:await service.cancel(user,run)
                await browser.close()
            report['passed']=True
    except Exception as exc:
        report.update(error_class=type(exc).__name__,error=clean_message(exc))
    finally:
        if service:await service.close()
        if engine:await engine.browser_tasks.close();await engine.client.close()
        if host:await host.close()
        if server:server.should_exit=True
        if server_task:await server_task
        config.RUNTIME,config.ORIGIN=runtime,origin
        (runtime/'navigation-acceptance.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
