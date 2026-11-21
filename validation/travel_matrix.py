"""Real-model multi-turn travel acceptance tests against isolated local services.

No real user's conversations, browser, bookings or notifications are touched.
Only real model/web-search APIs are used; Company inventory stays local.
"""
import asyncio
import json
import logging
import secrets
import socket
import tempfile
import time
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from openai import AsyncOpenAI
from temporalio.testing import WorkflowEnvironment

from availability.company import create_company
from availability.contracts import AGENT_PORTS,COMPANY_PORTS,identity,Task,leaves
from availability.subagent import create_subagent
from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.db import Database
from personal_agent.service import Service


class NoBrowser:
    async def close_user(self,user):pass


async def serve(app):
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if task.done():await task
        if server.started:return server,task,port
        await asyncio.sleep(.01)
    raise TimeoutError('Local service did not start')


async def main(only_itinerary=False,only_hotel=False,only_defaults=False):
    for name in ('httpx','mcp','deepagents.profiles.harness.harness_profiles'):logging.getLogger(name).setLevel(logging.ERROR)
    production=config.RUNTIME;key=config.keychain_key();report={'cases':[],'status':'running'}
    path=production/('availability-defaults-evaluation.json' if only_defaults else 'named-hotel-evaluation.json' if only_hotel else 'itinerary-evaluation.json' if only_itinerary else 'travel-evaluation.json')
    def save():path.write_text(json.dumps(report,indent=2))
    original_agents=dict(AGENT_PORTS);original_companies=dict(COMPANY_PORTS);address=config.TEMPORAL_ADDRESS
    day=datetime.now(ZoneInfo(config.TIMEZONE)).date()+timedelta(days=14)
    start=day.isoformat();end=(day+timedelta(days=9)).isoformat();changed=(day+timedelta(days=10)).isoformat()
    if only_itinerary and day.year==2026 and datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()<'2026-12-01':start='2026-12-01';end='2026-12-10'
    if only_hotel and datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()<'2026-12-01':start='2026-12-01';end='2026-12-03'
    with tempfile.TemporaryDirectory(prefix='aster-travel-matrix-') as directory:
        config.RUNTIME=Path(directory);database=Database(config.RUNTIME/'main.sqlite')
        user='validation-'+secrets.token_hex(8)
        database.execute('INSERT INTO users(id,username,password,must_change) VALUES(?,?,?,0)',(user,user,'unused'))
        environment=await WorkflowEnvironment.start_local(download_dest_dir=str(config.ROOT/'.runtime/bin'),dev_server_database_filename=str(config.RUNTIME/'temporal.sqlite'))
        config.TEMPORAL_ADDRESS=environment.client.service_client.config.target_host
        servers=[];service=None
        search_client=AsyncOpenAI(api_key=key,base_url='https://api.openai.com/v1',max_retries=2,timeout=25)
        try:
            for domain in COMPANY_PORTS:
                server,task,port=await serve(create_company(domain,config.RUNTIME));servers.append((server,task));COMPANY_PORTS[domain]=port
            for role in AGENT_PORTS:
                model=ChatOpenAI(model=config.MODEL,api_key=key,base_url='https://api.openai.com/v1',max_retries=2,timeout=45,reasoning_effort='low',use_responses_api=True,store=False)
                server,task,port=await serve(create_subagent(role,config.RUNTIME,model,environment.client,search_client));servers.append((server,task));AGENT_PORTS[role]=port
            async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'main-checkpoints.sqlite')) as saver:
                engine=Engine(database,NoBrowser(),saver,key);service=Service(database,engine)
                async def no_browser(*args,**kwargs):raise RuntimeError('Browser execution is outside this read-only travel validation')
                engine.browser_tasks.delegate=no_browser
                await service.availability.start()
                async def turn(case,sid,text,expected):
                    if only_defaults and not case['name'].startswith('Defaults:'):return []
                    if not only_defaults and case['name'].startswith('Defaults:'):return []
                    if only_itinerary and case['name']!='Dubai full requested ten-day itinerary':return []
                    if only_hotel and not case['name'].startswith('Named hotel:'):return []
                    began=time.monotonic();request=await service.submit(user,sid,text);rid=request['run_id']
                    await asyncio.wait_for(service.tasks[rid],240)
                    run=database.one('SELECT status,error FROM runs WHERE id=?',(rid,))
                    groups=[json.loads(r['payload']) for r in database.rows("SELECT payload FROM events WHERE run_id=? AND kind='availability_result' ORDER BY id",(rid,))]
                    results=[r for group in groups for r in group['results']]
                    reply=database.one("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))['content']
                    entry={'prompt':text,'seconds':round(time.monotonic()-began,2),'run_status':run['status'],'results':results,'reply':reply,'status':'passed'}
                    entry['usage']=database.one('SELECT model_calls,searches FROM run_usage WHERE run_id=?',(rid,))
                    entry['planner_limits']=[json.loads(r['payload']) for r in database.rows("SELECT payload FROM events WHERE run_id=? AND kind='planner_limit'",(rid,))]
                    entry['plan_gaps']=[json.loads(r['payload']) for r in database.rows("SELECT payload FROM events WHERE run_id=? AND kind='trip_gap'",(rid,))]
                    planned=database.one("SELECT payload FROM events WHERE run_id=? AND kind='trip_plan' ORDER BY id DESC LIMIT 1",(rid,))
                    if planned:entry['trip_plan']=json.loads(planned['payload'])
                    try:
                        assert run['status']=='completed',run
                        expected(results,reply)
                    except Exception as exc:entry.update(status='failed',failure=str(exc) or type(exc).__name__)
                    case['turns'].append(entry);save()
                    print(json.dumps({'case':case['name'],'turn':len(case['turns']),'status':entry['status'],'seconds':entry['seconds'],'failure':entry.get('failure')}),flush=True)
                    return results
                def category(domain,status='completed',**fields):
                    def check(results,reply):
                        matches=[r for r in results if r['domain']==domain and r['status']==status]
                        assert matches,f'Missing {domain} {status} result'
                        assert any(all(str(r['criteria'].get(k,'')).casefold()==str(v).casefold() for k,v in fields.items()) for r in matches),f'Criteria were not retained: {fields}'
                        if status=='completed':assert any(r.get('options') for r in matches),'No options rendered'
                    return check
                def new_case(name):
                    case={'name':name,'turns':[]};report['cases'].append(case);save();return case,database.new_session(user)
                def arithmetic(results,reply):
                    assert not results,'Unrelated prompt delegated'
                    assert '4' in reply,'Arithmetic answer was lost'
                def business(results,reply):
                    category('flights')(results,reply)
                    assert any('business' in r['criteria'].get('cabin','') for r in results),'Cabin correction missing'

                if only_defaults:
                    def combined_defaults(results,reply):
                        category('hotels',rooms=1,guests=2,check_in='2026-12-03',check_out='2026-12-21')(results,reply)
                        category('flights',passengers=2)(results,reply)
                        assert all(r['criteria'].get('hotel','').casefold()!='flights' for r in leaves(results) if r['domain']=='hotels')
                        assert 'Please provide' not in reply
                    case,sid=new_case('Defaults: original combined flights and hotels request')
                    await turn(case,sid,'can you check flights from LA to Paris on December 3rd to 21st for 2 people , business class and also check the hotel options',combined_defaults)
                    case,sid=new_case('Defaults: combined trip without people or rooms')
                    await turn(case,sid,'Check business flights from LA to Paris December 3rd to 21st and hotel options.',combined_defaults)
                    today=datetime.now(ZoneInfo(config.TIMEZONE)).date()
                    case,sid=new_case('Defaults: hotel without dates or counts')
                    await turn(case,sid,'Check Hyatt Grand Central hotel New York availability.',category('hotels',rooms=1,guests=2,check_in=(today+timedelta(days=1)).isoformat(),check_out=(today+timedelta(days=2)).isoformat()))
                    case,sid=new_case('Defaults: dining without date or people')
                    await turn(case,sid,'Check availability at Peacock Indian Bar and Grill in New York.',category('dining',party_size=2,date=today.isoformat()))

                case,sid=new_case('Dubai default room count')
                await turn(case,sid,f'Plan a trip from Phoenix to Dubai {start} to {end} for 2 travelers, business class. Show hotel and flight availability.',category('hotels',rooms=1,guests=2))
                await turn(case,sid,'1 room',category('hotels',rooms=1,guests=2,destination='Dubai',check_in=start,check_out=end))
                await turn(case,sid,'Thanks. What is 2 + 2?',arithmetic)

                case,sid=new_case('Paris flight origin and cabin correction')
                await turn(case,sid,f'Check economy flights to Paris on {start}, 1 passenger.',category('flights','needs_input'))
                await turn(case,sid,'From Phoenix',category('flights',destination='Paris',departure_date=start,passengers=1))
                await turn(case,sid,'Make that business class',business)

                case,sid=new_case('Tokyo multiple missing fields and date correction')
                await turn(case,sid,f'Check hotel availability in Tokyo from {start} to {end}.',category('hotels',rooms=1,guests=2))
                await turn(case,sid,'Two adults, one room',category('hotels',guests=2,rooms=1,check_in=start,check_out=end))
                await turn(case,sid,f'Actually change checkout to {changed}',category('hotels',guests=2,rooms=1,check_out=changed))

                case,sid=new_case('Fully specified hotel and flight together')
                await turn(case,sid,f'Find a room at The Plaza in New York {start} to {end}, 2 guests, 1 room, and Emirates business class from JFK to Dubai on {start} for 2 passengers.',lambda results,reply:(category('hotels')(results,reply),category('flights')(results,reply)))

                from availability.targets import hotel_key
                def named_hotel(expected,available=True):
                    def check(results,reply):
                        hotels=[r for r in leaves(results) if r['domain']=='hotels']
                        assert hotels and all(r['status']=='completed' and r.get('available')==available for r in hotels)
                        assert all(hotel_key(r['criteria'].get('hotel',''))==hotel_key(expected) for r in hotels),'Requested property was replaced by generic alternatives'
                        assert all(r['criteria']['check_in']==start and r['criteria']['check_out']==end and r['criteria']['guests']==2 and r['criteria']['rooms']==1 for r in hotels)
                        assert all(r.get('provenance')=='Company hotels' for r in hotels)
                        assert 'four seasons' not in reply.casefold() and 'marriott marquis' not in reply.casefold()
                    return check
                for label,name,available in [('Hyatt','hyatt grand central hotel new york',True),('OYO user spelling','oyo time square hotel new york',True),
                        ('OYO canonical spelling','OYO Times Square hotel New York',True),('Fifth Avenue embedded city','fifth avenue new york hotel',False)]:
                    case,sid=new_case('Named hotel: '+label)
                    await turn(case,sid,f'hey find availability for {name} {start} to {end} for 2 people, 1 room',named_hotel(name,available))
                case,sid=new_case('Named hotel: change property follow-up')
                await turn(case,sid,f'Find availability for Hyatt Grand Central hotel New York {start} to {end} for 2 people, 1 room',named_hotel('Hyatt Grand Central'))
                await turn(case,sid,'Actually use OYO time square hotel instead',named_hotel('OYO time square hotel'))

                case,sid=new_case('Dubai full requested ten-day itinerary')
                def itinerary(results,reply):
                    category('hotels')(results,reply);category('flights')(results,reply)
                    assert 'Research recommendations' not in reply,'Disconnected research list remained'
                    assert 'Dubai Hotel —' not in reply,'Generic hotel placeholder remained'
                    assert all(o.get('stars')==4 for r in leaves(results) if r['domain']=='hotels' for o in r.get('options',[])),'Wrong hotel rating'
                    assert any(r['domain']=='dining' for r in leaves(results)),'Dining subagent was not used'
                    assert all(o.get('return_departure','').startswith(end) for r in leaves(results) if r['domain']=='flights' and r.get('available') for o in r.get('options',[])),'Return flight date was dropped'
                    assert all(o.get('connections') for r in leaves(results) if r['domain']=='flights' and r.get('available') for o in r.get('options',[])),'Phoenix–Dubai route omitted its connection'
                    for index in range(1,11):assert f'Day {index} ·' in reply,f'Day {index} omitted'
                await turn(case,sid,f'Can you plan a trip to Dubai from Phoenix, 2 travelers, 1 room, {start} to {end}, 20k budget, business class flights, 4-star hotels and great dining venues? Please show day-by-day details on dining venues and entertainment around Dubai. Be specific on the dining venue recommendations.',itinerary)
                entry=case['turns'][-1] if case['turns'] else {}
                if entry.get('trip_plan'):
                    assert entry['trip_plan']['budget']['total']<=20000
                    assert len(entry['trip_plan']['days'])==10
                    from availability.trips import required_meals
                    plan=entry['trip_plan'];flight=next(r for r in plan['flights'] if r['query_id']==plan['selected_flight'])['options'][0]
                    for day in plan['days']:
                        expected=set(required_meals(day['date'],flight.get('arrival',''),flight.get('return_departure','')))
                        assert {r['criteria'].get('meal') for r in day['dining']}==expected,'Breakfast/lunch/dinner coverage is incomplete'
                        if day['transit'] not in {'outbound','return'}:assert day['activities'],'Entertainment omitted'
                    from collections import Counter
                    counts=Counter(o['name'].casefold() for d in plan['days'] for r in d['dining'] for o in r['options'])
                    assert max(counts.values())<=2,'Dining venues repeat more than twice'
                    for meal in ('Breakfast','Lunch','Dinner'):
                        assert entry['reply'].count('**'+meal+':**')==10,'A meal section is missing from a day'

                case,sid=new_case('Explicit monitor and cancellation')
                await turn(case,sid,f'Monitor business class flights from JFK to Dubai on {start} for 1 passenger every five minutes.',category('flights'))
                if not only_itinerary and not only_hotel and not only_defaults:assert service.availability.monitors(user),'Explicit monitoring did not create a schedule'
                await turn(case,sid,'Stop that flight monitor',lambda results,reply:None)
                if not only_itinerary and not only_hotel and not only_defaults and any(m['state']=='active' for m in service.availability.monitors(user)):case['turns'][-1].update(status='failed',failure='Monitor remained active after cancellation')

                research=[]
                for row in service.availability.store.rows('SELECT result FROM tasks WHERE user_id=?',(user,)):
                    research.extend(json.loads(row['result']).get('research',[]))
                report['component_checks']=[]
                checks=[] if only_itinerary or only_hotel or only_defaults else [('dining','dining',{'venue':'Nobu','location':'New York','date':start,'party_size':2}),
                    ('travel','hotels',{'hotel':'The Plaza','destination':'New York','check_in':start,'check_out':end,'guests':2,'rooms':1})]
                for role,domain,c in checks:
                    task=Task(request_id='research-'+secrets.token_hex(12),user_id=user,session_id=database.new_session(user),source_message_id=0,
                        queries=[{'domain':domain,'criteria':c,'research':True}])
                    response=await service.availability.api(role,'/v1/availability/query',user,task.model_dump())
                    ok=any(r.get('status')=='completed' and r.get('sources') for r in response.get('research',[])) and response['results'][0]['status']=='completed'
                    report['component_checks'].append({'role':role,'check':'named-resource web research plus Company lookup','status':'passed' if ok else 'failed','response':response})
                    research.extend(response.get('research',[]));save()
                    print(json.dumps({'component':role,'status':'passed' if ok else 'failed'}),flush=True)
                report['subagent_web_searches']={'attempted':len(research),'completed':sum(r.get('status')=='completed' for r in research)}
                report['cases']=[c for c in report['cases'] if c['turns']]
                report['status']='passed' if all(t['status']=='passed' for c in report['cases'] for t in c['turns']) and all(c['status']=='passed' for c in report['component_checks']) and (only_hotel or report['subagent_web_searches']['completed']) else 'failed'
                report['turns']=sum(len(c['turns']) for c in report['cases']);save()
                print(json.dumps({'status':report['status'],'turns':report['turns'],'subagent_web_searches':report['subagent_web_searches'],'report':str(path)}),flush=True)
                await service.close();service=None;await engine.browser_tasks.close();await engine.client.close()
        finally:
            if service:await service.close()
            for server,task in reversed(servers):server.should_exit=True;await task
            await search_client.close();await environment.shutdown()
            AGENT_PORTS.update(original_agents);COMPANY_PORTS.update(original_companies);config.RUNTIME=production;config.TEMPORAL_ADDRESS=address
    if report['status']!='passed':raise SystemExit(1)


if __name__=='__main__':
    import sys
    asyncio.run(main(only_itinerary='--itinerary' in sys.argv,only_hotel='--hotel' in sys.argv,only_defaults='--defaults' in sys.argv))
