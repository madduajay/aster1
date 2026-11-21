"""Browser-driven, real-model worldwide itinerary audit; no production chats.

Run: .venv/bin/python -m validation.world_itineraries
Optional --cities Paris Tokyo limits the audit. Company inventory stays local;
public web research uses the same models and harnesses as the running app.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime,timedelta,date
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import time
from zoneinfo import ZoneInfo

import uvicorn
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from openai import AsyncOpenAI
from playwright.async_api import async_playwright
from temporalio.testing import WorkflowEnvironment

from availability.company import create_company
from availability.contracts import AGENT_PORTS,COMPANY_PORTS,identity,leaves
from availability.subagent import create_subagent
from availability.targets import hotel_key
from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.browser_host import BrowserHost
from personal_agent.db import Database
from personal_agent.service import Service
from .travel_matrix import serve


CASES=[
    dict(city='Paris',origin='New York (JFK)',origin_aliases=['new york','jfk','nyc'],airports=['CDG','ORY','PAR'],days=5,party=2,rooms=1,cabin='business',budget=15000,currency='EUR',stars=4),
    dict(city='Tokyo',origin='San Francisco (SFO)',origin_aliases=['san francisco','sfo'],airports=['HND','NRT','TYO'],days=6,party=4,rooms=2,cabin='premium economy',budget=16000,currency='USD',stars=4),
    dict(city='Sydney',origin='Los Angeles (LAX)',origin_aliases=['los angeles','lax'],airports=['SYD'],days=6,party=2,rooms=1,cabin='business',budget=18000,currency='USD',stars=4),
    dict(city='Cape Town',origin='London (LHR)',origin_aliases=['london','lhr'],airports=['CPT'],days=5,party=2,rooms=1,cabin='business',budget=15000,currency='USD',stars=4),
    dict(city='Rio de Janeiro',origin='Miami (MIA)',origin_aliases=['miami','mia'],airports=['GIG','SDU','RIO'],days=5,party=2,rooms=1,cabin='economy',budget=10000,currency='USD',stars=4),
    dict(city='New York',origin='Phoenix (PHX)',origin_aliases=['phoenix','phx'],airports=['JFK','EWR','LGA','NYC'],days=4,party=2,rooms=1,cabin='business',budget=14000,currency='USD',hotel='OYO time square hotel New York'),
]


def analyze(case,run,groups,plan,reply):
    issues=[];checks={}
    def check(name,ok,detail,severity='error'):
        checks[name]=bool(ok)
        if not ok:issues.append({'severity':severity,'issue':detail})
    check('run_completed',run['status']=='completed','The request did not finish normally; status: '+run['status'])
    check('itinerary_created',bool(plan),'No complete day-by-day itinerary was produced.')
    flat=leaves([r for group in groups for r in group.get('results',[])])
    for domain in ('hotels','flights','dining'):
        results=[r for r in flat if r['domain']==domain and r.get('status')=='completed']
        check(domain+'_company_results',any(r.get('available') and r.get('provenance')=='Company '+domain for r in results),'No available '+domain+' result from Company was retained.')
    publishers={'flightroutes','flight routes','flightconnections','flight connections','google flights','kayak','skyscanner','expedia'}
    invalid_carriers=sorted({o.get('carrier','') for r in flat if r['domain']=='flights' for o in r.get('options',[]) if o.get('carrier','').casefold() in publishers})
    check('airline_entities',not invalid_carriers,'Research publishers were presented as airlines: '+', '.join(invalid_carriers)+'.')
    check('no_technical_errors',not any(s in reply for s in ('Invalid isoformat','Traceback','RuntimeError','OpenAIAuthenticationError','Please provide Invalid')),'A technical error appeared in the customer reply.')
    if not plan:return checks,issues
    days=plan.get('days',[]);expected=[(date.fromisoformat(case['start'])+timedelta(days=i)).isoformat() for i in range(case['days'])]
    check('all_dates',[d['date'] for d in days]==expected,'Trip dates were omitted or changed.')
    for label in ('Breakfast','Lunch','Dinner','Entertainment'):
        check('visible_'+label.casefold(),reply.count('**'+label+':**')==case['days'],label+' is missing from one or more displayed days.')
    hotel=next((r for r in plan['hotels'] if r['query_id']==plan['selected_hotel']),None)
    flight=next((r for r in plan['flights'] if r['query_id']==plan['selected_flight']),None)
    if not hotel or not flight:
        check('selected_options',False,'The selected hotel or flight was not among the checked options.');return checks,issues
    hc=hotel['criteria'];fc=flight['criteria'];fo=flight['options'][0]
    check('hotel_party_rooms',hc.get('guests')==case['party'] and hc.get('rooms')==case['rooms'],'Hotel guest or room count changed.')
    expected_check_in=max(case['start'],fo.get('arrival',case['start'])[:10])
    check('hotel_dates',hc.get('check_in')==expected_check_in and hc.get('check_out')==case['end'],'Hotel stay does not match destination arrival and requested checkout.')
    check('flight_dates',fc.get('departure_date')==case['start'] and fc.get('return_date')==case['end'],'Outbound or return date changed or was dropped.')
    check('flight_party_cabin',fc.get('passengers')==case['party'] and case['cabin'] in fc.get('cabin','').casefold(),'Flight passenger count or cabin changed.')
    check('flight_origin',any(a in fc.get('origin','').casefold() for a in case['origin_aliases']),'Flight origin changed.')
    destination=fc.get('destination','')
    check('flight_destination',case['city'].casefold() in destination.casefold() or destination.upper() in case['airports'],'Flight destination changed.')
    check('hotel_destination',case['city'].casefold() in hc.get('destination','').casefold(),'Hotel destination changed.')
    if case.get('stars'):check('hotel_stars',all(o.get('stars')==case['stars'] for o in hotel['options']),'Requested hotel star rating was not retained.')
    if case.get('hotel'):check('named_property',hotel_key(hc.get('hotel'))==hotel_key(case['hotel']),'The named hotel was replaced with another property.')
    else:check('hotel_discovery_evidence',bool(hotel.get('sources') and hotel.get('evidence_quote')),'The hotel row is not backed by a sourced hotel candidate; instructions or other text may have been treated as a property.')
    check('budget_currency',plan['budget'].get('currency')==case['currency'],'Requested '+case['currency']+' budget was represented as '+plan['budget'].get('currency','unknown')+'.')
    check('budget_limit',plan['budget'].get('limit')==case['budget'],'The requested numeric budget limit was not retained.')
    check('within_budget',plan['budget']['total']<=case['budget'],'The displayed total exceeds the requested numeric budget.')
    arrival=fo.get('arrival','');returning=fo.get('return_departure','');visits=[];meal_count=0;activity_count=0
    for day in days:
        meals=day.get('dining',[]);activity_count+=len(day.get('activities',[]))
        full_day=(not arrival or day['date']>arrival[:10]) and (not returning or day['date']<returning[:10])
        if full_day:check('three_meals_'+day['date'],{m['criteria'].get('meal') for m in meals}=={'breakfast','lunch','dinner'},'Breakfast, lunch or dinner is missing on '+day['date']+'.')
        late_arrival=arrival and day['date']==arrival[:10] and int(arrival[11:13])>=18
        if day.get('transit') not in {'outbound','return'} and not late_arrival:check('entertainment_'+day['date'],bool(day.get('activities')),'No entertainment was provided for '+day['date']+'.')
        generic=[]
        for activity in day.get('activities',[]):
            name=activity.get('name','').casefold().strip()
            if re.fullmatch(r'(?:things to do(?: in .+)?|top attractions(?: in .+)?|destinations|activities|food\s*(?:&|and)\s*drink|fine dining|official .+ tourism sites)',name):generic.append(activity['name'])
        check('specific_entertainment_'+day['date'],not generic,'Generic research/category links were presented as entertainment on '+day['date']+': '+', '.join(generic)+'.')
        for meal in meals:
            meal_count+=1;mc=meal['criteria']
            check('meal_scope_'+day['date']+'_'+mc.get('meal','unknown'),mc.get('date')==day['date'] and mc.get('party_size')==case['party'],'Meal date or party size differs from the trip on '+day['date']+'.')
            check('meal_confirmed_'+str(meal_count),meal.get('available') is True and meal.get('provenance')=='Company dining','An unconfirmed dining option appeared as available.')
            for option in meal.get('options',[]):
                visits.append(identity('dining',{'venue':option['name']})['venue'])
                times=option.get('times',[])
                check('meal_slots_'+str(meal_count),bool(times),'Meal availability times were omitted.')
                bounds={'breakfast':(5,11),'lunch':(11,17),'dinner':(17,24)}.get(mc.get('meal'),(0,24))
                check('meal_time_window_'+str(meal_count),all(bounds[0]<=int(t[:2])<bounds[1] for t in times),'Meal slots are in the wrong part of the day.')
                if arrival and day['date']==arrival[:10]:check('meal_after_arrival_'+str(meal_count),all(t>=arrival[11:16] for t in times),'Dining is scheduled before the flight arrives.')
                if returning and day['date']==returning[:10]:check('meal_before_departure_'+str(meal_count),all(t<returning[11:16] for t in times),'Dining is scheduled after flight departure.')
    counts=Counter(visits)
    check('dining_diversity',bool(counts) and max(counts.values())<=2,'A dining venue appears more than twice.')
    if arrival:check('hotel_arrival_alignment',hc['check_in']>=arrival[:10],'Hotel check-in is earlier than flight arrival, adding a potentially unused hotel night.','warning')
    check('flight_timezones',bool(fo.get('departure','')[-6:-5] in {'+','-'} and arrival[-6:-5] in {'+','-'}),'Flight times have no timezone offsets, so worldwide chronology cannot be independently verified.','warning')
    plan['_audit_summary']={'meals':meal_count,'unique_venues':len(counts),'max_visits':max(counts.values(),default=0),'activities':activity_count}
    return checks,issues


async def main(cities=None,report_name='world-itinerary-evaluation.json'):
    for name in ('httpx','mcp','deepagents.profiles.harness.harness_profiles'):logging.getLogger(name).setLevel(logging.ERROR)
    production=config.RUNTIME;original_origin=config.ORIGIN;original_address=config.TEMPORAL_ADDRESS
    original_agents=dict(AGENT_PORTS);original_companies=dict(COMPANY_PORTS)
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(production/'playwright'))
    selected=[dict(c) for c in CASES if not cities or c['city'] in cities]
    if not selected:raise ValueError('Select at least one configured city')
    report={'status':'running','cases':[],'scope':'Actual browser UI, main and subagent models, web search, authenticated subagent APIs, Company MCP, and Temporal; isolated accounts and storage.'}
    output=production/report_name
    artifacts=production/('world-itinerary-artifacts' if report_name=='world-itinerary-evaluation.json' else Path(report_name).stem+'-artifacts');artifacts.mkdir(exist_ok=True)
    def save():output.write_text(json.dumps(report,indent=2))
    base=datetime.now(ZoneInfo(config.TIMEZONE)).date()+timedelta(days=21)
    for c in selected:
        i=next(i for i,item in enumerate(CASES) if item['city']==c['city'])
        c['start']=(base+timedelta(days=i)).isoformat();c['end']=(date.fromisoformat(c['start'])+timedelta(days=c['days']-1)).isoformat()
    save();key=config.keychain_key()
    with tempfile.TemporaryDirectory(prefix='aster-world-itineraries-') as directory:
        config.RUNTIME=Path(directory);database=Database(config.RUNTIME/'main.sqlite');database.seed()
        database.execute('UPDATE users SET must_change=0')
        users=database.rows('SELECT id,username FROM users ORDER BY username')
        credentials=dict(line.split() for line in (config.RUNTIME/'initial-accounts.txt').read_text().splitlines())
        environment=await WorkflowEnvironment.start_local(download_dest_dir=str(production/'bin'),dev_server_database_filename=str(config.RUNTIME/'temporal.sqlite'))
        config.TEMPORAL_ADDRESS=environment.client.service_client.config.target_host
        servers=[];service=None;engine=None;host=BrowserHost(database,headless=True)
        search_client=AsyncOpenAI(api_key=key,base_url='https://api.openai.com/v1',max_retries=2,timeout=25)
        try:
            for domain in COMPANY_PORTS:
                server,task,port=await serve(create_company(domain,config.RUNTIME));servers.append((server,task));COMPANY_PORTS[domain]=port
            for role in AGENT_PORTS:
                model=ChatOpenAI(model=config.MODEL,api_key=key,base_url='https://api.openai.com/v1',max_retries=2,timeout=45,reasoning_effort='low',use_responses_api=True,store=False)
                server,task,port=await serve(create_subagent(role,config.RUNTIME,model,environment.client,search_client));servers.append((server,task));AGENT_PORTS[role]=port
            async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'main-checkpoints.sqlite')) as saver:
                engine=Engine(database,host,saver,key);service=Service(database,engine);await service.availability.start()
                actual_browser=engine.browser_tasks.delegate;browser_attempts={}
                async def read_browser(user,run,call_id,task,domains,inspect_only=False):
                    browser_attempts.setdefault(run,[]).append({'inspect_only':inspect_only,'domains':domains})
                    if not inspect_only:return {'status':'blocked','message':'This request is planning only. Continue with read-only research and do not book or submit anything.'}
                    return await actual_browser(user,run,call_id,task,domains,inspect_only=True)
                engine.browser_tasks.delegate=read_browser
                sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();port=sock.getsockname()[1]
                config.ORIGIN=f'https://aster.localhost:{port}'
                server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(production/'tls/server.pem'),ssl_keyfile=str(production/'tls/server.key'),log_level='error',access_log=False))
                task=asyncio.create_task(server.serve(sockets=[sock]));servers.append((server,task))
                while not server.started:
                    if task.done():await task
                    await asyncio.sleep(.01)
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                    for index,case in enumerate(selected):
                        owner=users[index];case.update(status='running',checks={},issues=[]);report['cases'].append(case);save()
                        context=await browser.new_context(viewport={'width':1200,'height':950});page=await context.new_page();console=[]
                        page.on('pageerror',lambda error:console.append(str(error)))
                        await page.goto(config.ORIGIN);await page.locator('#username').fill(owner['username']);await page.locator('#password').fill(credentials[owner['username']]);await page.locator('#password').press('Enter');await page.locator('#composer').wait_for(state='visible')
                        hotel=f"Stay specifically at {case['hotel']}; do not substitute another hotel." if case.get('hotel') else f"Find {case['stars']}-star hotels."
                        family='4 travelers (2 adults and 2 children)' if case['party']==4 else '2 adults'
                        budget_text=('$' if case['currency']=='USD' else case['currency']+' ')+f"{case['budget']:,}"
                        prompt=(f"Plan a trip from {case['origin']} to {case['city']} from {case['start']} to {case['end']} for {family}, {case['rooms']} room(s), {case['cabin']} flights, with a total budget of {budget_text}. {hotel} "
                            'Show flight and hotel availability and a complete day-by-day itinerary. For every full day at the destination, give breakfast, lunch and dinner venues with available slots, plus specific entertainment/sightseeing in the morning, afternoon or evening. '
                            'Use varied dining venues; at most two visits to the same venue. Infer dining dates and party size from the trip. Account for arrival/departure days. Do not book or reserve anything.')
                        case['prompt']=prompt;began=time.monotonic();started=time.time();save()
                        async with page.expect_response(lambda response:response.url.endswith('/api/runs') and response.request.method=='POST') as pending:
                            await page.locator('#input').fill(prompt);await page.locator('#input').press('Enter')
                        submitted=await (await pending.value).json();rid=submitted['run_id'];case['run_id']=rid;save()
                        async def progress():
                            while True:
                                row=database.one("SELECT payload FROM events WHERE run_id=? AND kind='progress' ORDER BY id DESC LIMIT 1",(rid,))
                                case['progress']=json.loads(row['payload']) if row else 'Thinking';case['elapsed_seconds']=round(time.monotonic()-began);save();await asyncio.sleep(3)
                        ticker=asyncio.create_task(progress())
                        try:
                            execution=service.tasks.get(rid)
                            if execution:await asyncio.wait_for(asyncio.shield(execution),300)
                        except TimeoutError:
                            await service.cancel(owner['id'],rid);case['issues'].append({'severity':'error','issue':'The audit stopped this case after five minutes.'})
                        finally:ticker.cancel();await asyncio.gather(ticker,return_exceptions=True)
                        run=database.one('SELECT status,error FROM runs WHERE id=?',(rid,));case['run_status']=run['status'];case['seconds']=round(time.monotonic()-began,2)
                        answer=database.one("SELECT payload FROM events WHERE run_id=? AND kind='answer' ORDER BY id DESC LIMIT 1",(rid,))
                        case['reply']=json.loads(answer['payload']) if answer else ''
                        final_message=database.one("SELECT id,created FROM messages WHERE session_id=(SELECT session_id FROM runs WHERE id=?) AND role='assistant' AND content=? ORDER BY id DESC LIMIT 1",(rid,case['reply']))
                        if final_message:case['response_seconds']=round(final_message['created']-started,2)
                        groups=[json.loads(r['payload']) for r in database.rows("SELECT payload FROM events WHERE run_id=? AND kind='availability_result' ORDER BY id",(rid,))]
                        case['availability']=groups
                        planned=database.one("SELECT payload FROM events WHERE run_id=? AND kind='trip_plan' ORDER BY id DESC LIMIT 1",(rid,));plan=json.loads(planned['payload']) if planned else None
                        checks,issues=analyze(case,run,groups,plan,case['reply']);case['checks']=checks;case['issues']+=issues
                        if plan:case['trip_plan']=plan;case['summary']=plan['_audit_summary']
                        case['usage']=database.one('SELECT model_calls,searches FROM run_usage WHERE run_id=?',(rid,))
                        case['planner_limits']=[json.loads(r['payload']) for r in database.rows("SELECT payload FROM events WHERE run_id=? AND kind='planner_limit'",(rid,))]
                        case['plan_gaps']=[json.loads(r['payload']) for r in database.rows("SELECT payload FROM events WHERE run_id=? AND kind='trip_gap'",(rid,))]
                        case['entertainment_research']=[json.loads(r['payload']) for r in database.rows("SELECT payload FROM events WHERE run_id=? AND kind='research_evidence'",(rid,))]
                        case['browser_research']=browser_attempts.get(rid,[])
                        if console:case['issues'].append({'severity':'error','issue':'The browser UI reported an uncaught error.'});case['console_errors']=console
                        await page.reload();await page.locator('#composer').wait_for(state='visible')
                        if final_message:
                            node=page.locator('[data-message-id="'+str(final_message['id'])+'"]');await node.wait_for(state='visible')
                            case['ui_text']=await node.inner_text();case['ui_tables']=await node.locator('table').count()
                            if plan and case['ui_tables']<2:case['issues'].append({'severity':'error','issue':'Hotel/flight tables did not render in the saved chat UI.'})
                        image=artifacts/(case['city'].lower().replace(' ','-')+'.png');await page.screenshot(path=str(image),full_page=True);case['screenshot']=str(image)
                        case['status']='failed' if any(i['severity']=='error' for i in case['issues']) else 'passed'
                        case.pop('progress',None);save()
                        print(json.dumps({'city':case['city'],'status':case['status'],'seconds':case['seconds'],'issues':case['issues'],'summary':case.get('summary')}),flush=True)
                        await context.close();await host.close_user(owner['id'])
                    await browser.close()
                report['status']='passed' if all(c['status']=='passed' for c in report['cases']) else 'issues_found';save()
                print(json.dumps({'status':report['status'],'cases':len(report['cases']),'report':str(output)}),flush=True)
        finally:
            if service:await service.close()
            if engine:await engine.browser_tasks.close();await engine.client.close()
            await host.close()
            for server,task in reversed(servers):server.should_exit=True;await task
            await search_client.close();await environment.shutdown()
            AGENT_PORTS.update(original_agents);COMPANY_PORTS.update(original_companies)
            config.RUNTIME=production;config.ORIGIN=original_origin;config.TEMPORAL_ADDRESS=original_address


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--cities',nargs='*');parser.add_argument('--report',default='world-itinerary-evaluation.json');args=parser.parse_args()
    asyncio.run(main(args.cities,args.report))
