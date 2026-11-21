"""Live UI/DeepAgent/MCP checks for date ranges, option counts and previews."""
import asyncio,json,logging,os,socket,tempfile,time
from pathlib import Path
import uvicorn
from openai import AsyncOpenAI
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from temporalio.testing import WorkflowEnvironment
from playwright.async_api import async_playwright
from availability.company import create_company
from availability.subagent import create_subagent
from availability.contracts import AGENT_PORTS,COMPANY_PORTS,leaves
from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.db import Database
from personal_agent.service import Service
from personal_agent.app import create_app
from personal_agent.browser_host import BrowserHost
from .travel_matrix import serve

async def main(only_products=False):
    for name in ('httpx','mcp','deepagents.profiles.harness.harness_profiles'):logging.getLogger(name).setLevel(logging.ERROR)
    production=config.RUNTIME;origin=config.ORIGIN;address=config.TEMPORAL_ADDRESS;agents=dict(AGENT_PORTS);companies=dict(COMPANY_PORTS)
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(production/'playwright'));key=config.keychain_key()
    report={'status':'running','cases':[]};output=production/('visual-products-evaluation.json' if only_products else 'visual-recommendations-evaluation.json');artifacts=production/('visual-products-artifacts' if only_products else 'visual-recommendations-artifacts');artifacts.mkdir(exist_ok=True)
    def save():output.write_text(json.dumps(report,indent=2))
    with tempfile.TemporaryDirectory(prefix='aster-visual-acceptance-') as temp:
        config.RUNTIME=Path(temp);db=Database(config.RUNTIME/'main.sqlite');db.seed();db.execute('UPDATE users SET must_change=0')
        user=db.one("SELECT * FROM users WHERE username='user01'");password=dict(line.split() for line in (config.RUNTIME/'initial-accounts.txt').read_text().splitlines())[user['username']]
        env=await WorkflowEnvironment.start_local(download_dest_dir=str(production/'bin'),dev_server_database_filename=str(config.RUNTIME/'temporal.sqlite'));config.TEMPORAL_ADDRESS=env.client.service_client.config.target_host
        servers=[];service=None;engine=None;host=BrowserHost(db,headless=True);search=AsyncOpenAI(api_key=key,max_retries=2,timeout=30)
        try:
            for domain in COMPANY_PORTS:
                server,task,port=await serve(create_company(domain,config.RUNTIME));servers.append((server,task));COMPANY_PORTS[domain]=port
            for role in AGENT_PORTS:
                model=ChatOpenAI(model=config.MODEL,api_key=key,max_retries=2,timeout=45,reasoning_effort='low',use_responses_api=True,store=False)
                server,task,port=await serve(create_subagent(role,config.RUNTIME,model,env.client,search));servers.append((server,task));AGENT_PORTS[role]=port
            async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'checkpoints.sqlite')) as saver:
                engine=Engine(db,host,saver,key);service=Service(db,engine);await service.availability.start()
                actual=engine.browser_tasks.delegate
                async def readonly(*args,**kwargs):
                    if not kwargs.get('inspect_only',False):return {'status':'blocked','message':'Read-only recommendation test; no purchases or bookings.'}
                    return await actual(*args,**kwargs)
                engine.browser_tasks.delegate=readonly
                sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();config.ORIGIN=f'https://aster.localhost:{sock.getsockname()[1]}'
                server=uvicorn.Server(uvicorn.Config(create_app(db,service),ssl_certfile=str(production/'tls/server.pem'),ssl_keyfile=str(production/'tls/server.key'),log_level='error',access_log=False));task=asyncio.create_task(server.serve(sockets=[sock]));servers.append((server,task))
                while not server.started:await asyncio.sleep(.01)
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(headless=True,chromium_sandbox=True);page=await browser.new_page(viewport={'width':1200,'height':950});await page.goto(config.ORIGIN)
                    await page.locator('#username').fill(user['username']);await page.locator('#password').fill(password);await page.locator('#password').press('Enter');await page.locator('#composer').wait_for(state='visible')
                    async def turn(name,sid,prompt,check):
                        await page.evaluate('sid=>openSession(sid)',sid);entry={'name':name,'prompt':prompt,'status':'running'};report['cases'].append(entry);save();began=time.monotonic()
                        async with page.expect_response(lambda r:r.url.endswith('/api/runs') and r.request.method=='POST') as pending:
                            await page.locator('#input').fill(prompt);await page.locator('#input').press('Enter')
                        rid=(await (await pending.value).json())['run_id'];entry['run_id']=rid;save()
                        await asyncio.wait_for(asyncio.shield(service.tasks[rid]),260)
                        answer=db.one("SELECT payload FROM events WHERE run_id=? AND kind='answer' ORDER BY id DESC LIMIT 1",(rid,));entry['reply']=json.loads(answer['payload']) if answer else ''
                        entry['seconds']=round(time.monotonic()-began,2);entry['results']=[r for row in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='availability_result' ORDER BY id",(rid,)) for r in json.loads(row['payload'])['results']]
                        entry['option_counts']=[json.loads(r['payload']) for r in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='option_counts'",(rid,))]
                        entry['dining_ranges']=[json.loads(r['payload']) for r in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='dining_range'",(rid,))]
                        entry['media']=[json.loads(r['value']) for r in service.availability.store.rows('SELECT value FROM venue_metadata')]
                        try:check(entry);entry['status']='passed'
                        except Exception as exc:entry.update(status='failed',failure=str(exc) or type(exc).__name__)
                        await page.reload();await page.locator('#composer').wait_for(state='visible');await page.evaluate('sid=>openSession(sid)',sid)
                        await page.wait_for_timeout(1500)
                        node=page.locator('.message.assistant').last
                        entry['preview_rows']=await node.locator('.venue-preview').count();entry['photos_loaded']=await node.locator('.venue-photo').evaluate_all('(images)=>images.filter(i=>i.complete&&i.naturalWidth>0).length');entry['placeholders']=await node.locator('.venue-photo-placeholder').count()
                        entry['linked_titles']=await node.locator('.venue-name a').count()
                        if not entry['preview_rows'] or entry['linked_titles']!=entry['preview_rows']:entry.update(status='failed',failure='Preview rows lack clickable initial names')
                        if name!='paris-full-dining-range' and not entry['photos_loaded']:entry.update(status='failed',failure='No actual source photo loaded')
                        entry['screenshot']=str(artifacts/(name+'.png'));await page.screenshot(path=entry['screenshot'],full_page=True)
                        save();print(json.dumps({k:entry.get(k) for k in ('name','status','seconds','failure','preview_rows','photos_loaded','placeholders')}),flush=True)
                    def counts(entry):
                        p=entry['option_counts'][-1];assert p['hotels']['count']==2 and p['flights']['count']==4
                        flat=leaves(entry['results'])
                        for domain,target in [('hotels',2),('flights',4)]:
                            options=[o for r in flat if r['domain']==domain and r.get('available') for o in r.get('options',[])]
                            assert 0<len(options)<=target,(domain,len(options))
                            if len(options)<target:assert 'you asked for '+str(target) in entry['reply']
                    def date_range(entry):
                        flat=[r for r in leaves(entry['results']) if r['domain']=='dining' and r.get('status')=='completed']
                        expected={('2026-12-'+str(i).zfill(2),meal) for i in range(3,22) for meal in ('lunch','dinner')}
                        assert {(r['criteria']['date'],r['criteria'].get('meal')) for r in flat}==expected,'Missing requested dates/meals'
                        assert all(r['criteria']['party_size']==2 for r in flat)
                        assert 'Breakfast' not in entry['reply']
                        assert all(r.get('available') and r.get('provenance')=='Company dining' for r in flat)
                    def random_counts(entry):
                        p=entry['option_counts'][-1];assert all(4<=v['count']<=7 and not v['explicit'] for v in p.values())
                        assert all(any(r['domain']==domain and r.get('available') for r in leaves(entry['results'])) for domain in ('hotels','flights'))
                    def product(entry):
                        import re
                        assert '/api/venue-photos/' in entry['reply'],'No source-backed item previews'
                        assert 'target.com/p/' in entry['reply'],'No specific Target item links'
                        links=set(re.findall(r'\]\((https://(?:www\.)?target.com/p/[^)]+)\)',entry['reply']))
                        assert len(links)==2,'Requested two specific product pages'
                    sid=db.new_session(user['id'])
                    if not only_products:
                        await turn('requested-counts',sid,'Show me 2 hotels and 4 flight options from Los Angeles to Paris December 3 to 21, 2026, business class, for 2 people. Do not book anything.',counts)
                        await turn('paris-full-dining-range',sid,'show me some dining venues in paris between those days - for lunch and dinner',date_range)
                        await turn('random-default-counts',db.new_session(user['id']),'Find hotels and business flight options from Los Angeles to Paris December 3 to 21, 2026 for 2 people. Do not book anything.',random_counts)
                    await turn('target-shirts',db.new_session(user['id']),"Show me 2 men's linen shirts from Target with a short description. Do not buy anything.",product)
                    await browser.close()
                report['status']='passed' if all(c['status']=='passed' for c in report['cases']) else 'issues_found';save()
        finally:
            if service:await service.close()
            if engine:await engine.browser_tasks.close();await engine.client.close()
            await host.close()
            for server,task in reversed(servers):server.should_exit=True;await task
            await search.close();await env.shutdown();AGENT_PORTS.update(agents);COMPANY_PORTS.update(companies);config.RUNTIME=production;config.ORIGIN=origin;config.TEMPORAL_ADDRESS=address
    print(json.dumps({'status':report['status'],'report':str(output)}),flush=True)

if __name__=='__main__':
    import sys
    asyncio.run(main(only_products='--products' in sys.argv))
