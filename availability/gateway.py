"""Main-owned delegation, schedules and atomic chat/alert projection."""
import asyncio
import json
import time
import secrets
import re
from copy import deepcopy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

import httpx
from temporalio import activity
from temporalio.client import (Client,Schedule,ScheduleActionStartWorkflow,ScheduleIntervalSpec,ScheduleSpec,
    SchedulePolicy,ScheduleOverlapPolicy,ScheduleUpdate)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from personal_agent import config
from .contracts import AGENT_PORTS,Task,Query,criteria,describe,digest,expires_at,role_for,identity,leaves,candidate_id
from .security import Security
from .store import Store
from .workflows import MainAvailability,AvailabilityCheck


def semantic(result):
    value={k:result.get(k) for k in ('status','available','options','missing_fields','error')}
    if isinstance(value['options'],list):value['options']=[{k:v for k,v in option.items() if k not in {'photo_url','description'}} for option in value['options']]
    return value


class Gateway:
    def __init__(self,service):
        self.service=service;self.db=service.db;self.security=Security(config.RUNTIME)
        self.store=Store(config.RUNTIME/'availability.sqlite');self.client=None;self.worker=None;self.runner=None

    def default_cabin(self,user):
        return 'economy'

    def pending(self,user,session,floor=0):
        newer=[];pending=[]
        for row in self.store.rows('SELECT envelope,result FROM tasks WHERE user_id=? AND session_id=? AND updated>? ORDER BY updated DESC LIMIT 30',(user,session,time.time()-86400)):
            envelope=json.loads(row['envelope'])
            if envelope.get('watch_policy')=='observe_only':continue
            if envelope['source_message_id']<=floor:continue
            for result in json.loads(row['result']).get('results',[]):
                domain=result['domain'];known=identity(domain,result['criteria'])
                superseded=any(r['domain']==domain and all(identity(domain,r['criteria']).get(k)==v for k,v in known.items()) for r in newer)
                if result['status']=='needs_input' and not superseded:
                    try:
                        raw=dict(result['criteria'])
                        if domain=='flights' and not raw.get('cabin'):raw['cabin']=self.default_cabin(user)
                        if domain=='flights' and not raw.get('origin'):
                            from .followup import flight_origin
                            source=self.db.one("SELECT content FROM messages WHERE id=? AND session_id=? AND role='user'",(envelope['source_message_id'],session))
                            origin=flight_origin(source['content']) if source else None
                            if not origin:
                                for previous in self.store.rows('SELECT envelope FROM tasks WHERE user_id=? AND session_id=? ORDER BY updated DESC LIMIT 30',(user,session)):
                                    prior=json.loads(previous['envelope'])
                                    if not floor<prior['source_message_id']<=envelope['source_message_id']:continue
                                    related=any(q['domain']=='flights' and all(identity('flights',q['criteria']).get(k)==identity('flights',raw).get(k) for k in ('destination','departure_date','return_date')) for q in prior['queries'])
                                    if not related:continue
                                    evidence=self.db.one("SELECT content FROM messages WHERE id=? AND session_id=? AND role='user'",(prior['source_message_id'],session))
                                    origin=flight_origin(evidence['content']) if evidence else None
                                    if origin:break
                            if origin:raw['origin']=origin
                        normalized,missing=criteria(domain,raw)
                        pending.append({**result,'criteria':normalized,'missing_fields':missing,'status':'needs_input' if missing else 'ready_for_check'})
                    except (ValueError,TypeError):pending.append(result)
                newer.append(result)
        return pending[:10]

    def recent(self,user,session,floor=0):
        results=[];seen=set()
        for row in self.store.rows('SELECT envelope,result FROM tasks WHERE user_id=? AND session_id=? AND updated>? ORDER BY updated DESC LIMIT 30',(user,session,time.time()-86400)):
            envelope=json.loads(row['envelope'])
            if envelope['source_message_id']<=floor or envelope.get('watch_policy')=='observe_only':continue
            group=json.loads(row['result']).get('results',[])
            for result in group:
                if result['domain']=='flights' and result['status']=='needs_input' and not result['criteria'].get('cabin'):
                    raw={**result['criteria'],'cabin':self.default_cabin(user)}
                    try:
                        normalized,missing=criteria('flights',raw)
                        result.update(criteria=normalized,missing_fields=missing,status='needs_input' if missing else 'ready_for_check')
                    except (ValueError,TypeError):pass
            results.extend(r for r in group if r['domain'] not in seen)
            seen.update(r['domain'] for r in group)
        return results[:10]

    async def start(self):
        for role in AGENT_PORTS:
            health=await self.api(role,'/health','')
            if health.get('service')!=role+'-subagent' or not health.get('ready'):raise RuntimeError(role+' subagent is not ready')
        self.client=await Client.connect(config.TEMPORAL_ADDRESS,namespace=config.TEMPORAL_NAMESPACE)
        self.worker=Worker(self.client,task_queue='aster-main-v3',workflows=[MainAvailability,AvailabilityCheck],activities=[self.publish,self.poll])
        self.worker_task=asyncio.create_task(self.worker.run());self.runner=asyncio.create_task(self.maintain())

    async def close(self):
        if self.runner:self.runner.cancel();await asyncio.gather(self.runner,return_exceptions=True)
        if self.worker:await self.worker.shutdown();await self.worker_task

    async def api(self,role,path,user,body=None):
        async with httpx.AsyncClient(timeout=120,headers=self.security.headers(user)) as client:
            response=await client.request('POST' if body is not None else 'GET',f'http://127.0.0.1:{AGENT_PORTS[role]}'+path,json=body)
            response.raise_for_status();return response.json()

    def envelope(self,user,run,raw,role,watch_policy='normal'):
        if watch_policy not in {'normal','observe_only'}:raise ValueError('Invalid watch policy')
        owned=self.db.one("SELECT * FROM runs WHERE id=? AND user_id=? AND status IN ('running','resuming')",(run,user))
        if not owned:raise LookupError('Active request not found')
        source=self.db.one("SELECT id,content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",(owned['session_id'],))
        from personal_agent.steering import request_messages
        merged_messages=request_messages(self.db,user,run) if self.db.one('SELECT message_id FROM run_followups WHERE run_id=?',(run,)) else []
        from .targets import literal_hotels,requested_match,generic_target
        from .contracts import canonical
        marker=self.db.one("SELECT payload FROM events WHERE run_id=? AND kind='hotel_targets' ORDER BY id DESC LIMIT 1",(run,))
        marker=json.loads(marker['payload']) if marker else {}
        targets=literal_hotels(source['content'],marker.get('names',[]) if marker.get('source_message_id')==source['id'] else [],hotel_domain=any(q.get('domain')=='hotels' for q in raw)) if role=='travel' else []
        from .trips import scope,dining_queries,dining_followup
        avoid_venues=[];trip={}
        if role=='dining':
            trip=scope(self.db,user,run)
            if trip:
                from .routes import location
                supplied=[q.get('criteria',{}) for q in raw]
                def same_destination(city):
                    other=trip.get('location','')
                    return canonical(city)==canonical(other) or bool(location(city) and location(city)==location(other))
                mismatch=any(c.get('location') and not same_destination(c['location']) for c in supplied)
                dated=any(c.get('date') for c in supplied) and bool(re.search(r'\b\d{4}-\d{2}-\d{2}\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b',source['content'],re.I))
                standalone_day=dated and not re.search(r'\b(?:trip|itinerary|hotels?|flights?|throughout|every|each|between)\b',source['content'],re.I)
                if mismatch or standalone_day:trip={}
            if trip:
                meals=[meal for meal in ('breakfast','lunch','dinner') if re.search(r'\b'+meal+r'\b',source['content'],re.I)]
                if meals:trip={**trip,'meals':meals}
            if not trip:
                trip=dining_followup(self.db,self.store,user,run,source['content'],raw)
                if trip:self.db.event(run,'dining_range',{**trip,'source_message_id':source['id']})
        if trip:
            expanded=dining_queries(trip)
            for query in expanded:
                for supplied in raw:
                    c=supplied.get('criteria',{})
                    if c.get('date')==query['criteria']['date'] and c.get('meal','dinner')==query['criteria']['meal']:
                        query['criteria'].update({k:v for k,v in c.items() if k in {'venue','cuisine','preferred_time'}})
            done=set()
            for event in self.db.rows("SELECT payload FROM events WHERE run_id=? AND kind='availability_result'",(run,)):
                for result in leaves(json.loads(event['payload']).get('results',[])):
                    if result['domain']=='dining' and result.get('status')=='completed':done.add((result['criteria'].get('date'),result['criteria'].get('meal')))
            gap=self.db.one("SELECT payload FROM events WHERE run_id=? AND kind='trip_gap' ORDER BY id DESC LIMIT 1",(run,))
            gap=json.loads(gap['payload']) if gap else {}
            avoid_venues=gap.get('avoid_venues',[])
            if avoid_venues:
                done.difference_update((r['date'],r['meal']) for r in gap.get('meals',[]))
            pending=[q for q in expanded if (q['criteria']['date'],q['criteria']['meal']) not in done]
            if pending:expanded=pending
            raw=expanded
        queries=[Query.model_validate({'domain':q.get('domain'),'criteria':q.get('criteria',{}),'research':q.get('research',False)}) for q in raw]
        if any(role_for(q.domain)!=role for q in queries):raise ValueError('Unsupported delegation domain')
        from .followup import room_counts,party_counts,cabins,cabin_value,explicit_reply,star_ratings,flight_origin
        from .contracts import canonical
        floor=self.db.owned_session(user,owned['session_id'])['context_floor']
        drafts=self.pending(user,owned['session_id'],floor)
        recent=self.recent(user,owned['session_id'],floor)
        same_request=self.store.one("SELECT id FROM tasks WHERE user_id=? AND session_id=? AND json_extract(envelope,'$.source_message_id')=? LIMIT 1",(user,owned['session_id'],source['id']))
        text=source['content'].casefold().strip()
        correction=len(text)<180 and (text.startswith(('from ','to ','in ','actually ','make that ','change ','instead ')) or bool(room_counts(text)) or bool(cabins(text)))
        for query in queries:
            basis=recent if correction or same_request else drafts
            incoming=identity(query.domain,query.criteria)
            candidates=[d for d in basis if d['domain']==query.domain and (correction or all(not incoming.get(k) or not identity(d['domain'],d['criteria']).get(k) or incoming[k]==identity(d['domain'],d['criteria'])[k] for k in ('hotel','venue','destination','origin','location')))]
            if len(candidates)==1:query.criteria={**candidates[0]['criteria'],**{k:v for k,v in query.criteria.items() if v not in ('',None)}}
            query.criteria.update(explicit_reply(source['content'],query.domain))
            if query.domain=='flights':
                origin=flight_origin(source['content'])
                if origin:query.criteria['origin']=origin
            fields={'hotels':{'rooms':room_counts,'guests':party_counts,'stars':star_ratings},'flights':{'passengers':party_counts,'cabin':cabins},'dining':{'party_size':party_counts}}[query.domain]
            for field,parse in fields.items():
                known=parse(source['content'])
                if not known:
                    for message in reversed(merged_messages):
                        known=parse(message['content'])
                        if known:break
                if not known and role=='dining' and trip and field=='party_size':known={trip['party_size']}
                if not known:
                    for record in self.store.rows('SELECT envelope,result FROM tasks WHERE user_id=? AND session_id=? ORDER BY updated DESC LIMIT 30',(user,owned['session_id'])):
                        envelope=json.loads(record['envelope'])
                        if envelope['source_message_id']<=floor:continue
                        evidence=self.db.one("SELECT content FROM messages WHERE id=? AND session_id=? AND role='user'",(envelope['source_message_id'],owned['session_id']))
                        if not evidence:continue
                        for result in json.loads(record['result']).get('results',[]):
                            c=result['criteria'];keys={'hotels':('destination','hotel','check_in'),'flights':('origin','destination','departure_date'),'dining':('venue','location','date')}[query.domain]
                            same=result['domain']==query.domain and any(c.get(k) and canonical(c[k])==canonical(query.criteria.get(k)) for k in keys)
                            value=c.get(field);normalized=cabin_value(value) if field=='cabin' else value
                            if same and normalized in parse(evidence['content']):known.add(normalized)
                            if same and not known:
                                replies=self.db.rows("SELECT content FROM messages WHERE session_id=? AND role='user' AND id>? AND id<=? ORDER BY id DESC LIMIT 20",(owned['session_id'],envelope['source_message_id'],source['id']))
                                for reply in replies:
                                    if len(reply['content'])<=120 and parse(reply['content']):known.update(parse(reply['content']));break
                        if known:break
                value=query.criteria.get(field);value=cabin_value(value) if field=='cabin' else value
                if len(known)==1:query.criteria[field]=next(iter(known))
                elif value not in known:query.criteria.pop(field,None)
            if role=='travel':
                from personal_agent.travel_planning import TravelPlans
                active=self.db.one("SELECT payload FROM events WHERE run_id=? AND kind='travel_intent' ORDER BY id DESC LIMIT 1",(run,))
                draft=TravelPlans(self.db).load(user,owned['session_id']) if active and json.loads(active['payload'])['intent']!='none' else None
                for field,key in ({'cabin':'cabin','passengers':'travelers'} if query.domain=='flights' else {'guests':'travelers','rooms':'rooms'}).items():
                    if field not in query.criteria and draft and key in draft['confirmed']:
                        query.criteria[field]=draft['confirmed'][key]['value']
                if query.domain=='flights' and not query.criteria.get('cabin'):
                    query.criteria['cabin']=self.default_cabin(user)
                    if query.criteria['cabin']=='economy':
                        self.db.event(run,'travel_assumptions',{'cabin':'economy'})
            # Discovery belongs to the subagent. A main-model shortlist is not
            # equivalent to the user explicitly choosing a named resource.
            resource={'hotels':'hotel','dining':'venue','flights':'carrier'}[query.domain]
            if query.domain=='hotels' and targets:
                bound=targets[0] if len(targets)==1 else requested_match(query.criteria.get('hotel',''),targets)
                if bound:query.criteria['hotel']=bound
            name=query.criteria.get(resource)
            if query.domain=='hotels' and name and generic_target(name):
                query.criteria.pop(resource,None);query.research=True;name=None
            def mentioned(text):
                needle=identity(query.domain,{resource:name})[resource]
                corpus=identity(query.domain,{resource:text})[resource].replace('5th','fifth')
                if query.domain=='hotels':
                    def property_name(value):
                        value=value.replace('5th','fifth')
                        city=identity('hotels',{'destination':query.criteria.get('destination','')})['destination']
                        if city=='new york':value=re.sub(r'\b(?:new york city|new york|nyc|ny)\b',' ',value)
                        elif city:value=re.sub(r'\b'+re.escape(city)+r'\b',' ',value)
                        value=re.sub(r'\b(?:the|hotel|hotels)\b',' ',value)
                        return ' '.join(re.sub(r'\bave\b','avenue',value).split())
                    needle=property_name(needle);corpus=property_name(corpus)
                    if not needle:return False
                aliases={'emirates':'ek','british airways':'ba','qatar airways':'qr','american airlines':'aa'}
                return bool(re.search(r'\b'+re.escape(needle)+r'\b',corpus) or needle in aliases and re.search(r'\b'+aliases[needle]+r'\b',corpus))
            explicit=bool(name and mentioned(source['content']))
            if name and not explicit:explicit=any(mentioned(m['content']) for m in merged_messages)
            if name and not explicit:
                for record in self.store.rows('SELECT envelope,result FROM tasks WHERE user_id=? AND session_id=? ORDER BY updated DESC LIMIT 20',(user,owned['session_id'])):
                    envelope=json.loads(record['envelope'])
                    if envelope['source_message_id']<=floor:continue
                    previous=self.db.one("SELECT content FROM messages WHERE id=? AND session_id=? AND role='user'",(envelope['source_message_id'],owned['session_id']))
                    if previous and mentioned(previous['content']) and any(r['domain']==query.domain and identity(query.domain,r['criteria']).get(resource)==identity(query.domain,{resource:name})[resource] for r in json.loads(record['result']).get('results',[])):
                        explicit=True;break
            if name and not explicit:
                query.criteria.pop(resource,None)
                if query.domain=='flights':query.criteria.pop('connections',None)
                query.research=True
            if query.domain=='hotels' and not query.criteria.get('hotel') and not query.criteria.get('stars'):
                # A named property or explicit rating wins over the search default.
                query.criteria['stars']=4
                self.db.event(run,'travel_assumptions',{'hotel_quality':'mid luxury (4-star)'})
            if query.domain=='dining' and query.criteria.get('cuisine') and not any(canonical(query.criteria['cuisine']) in canonical(content) for content in [source['content']]+[m['content'] for m in merged_messages]):query.criteria.pop('cuisine')
        if role=='travel':
            from .routes import location
            from .trips import active_scope
            trip=active_scope(self.db,user,run)
            def same_city(left,right):
                return bool(left and right and (canonical(left)==canonical(right) or location(left) and location(left)==location(right)))
            from personal_agent.travel_planning import TravelPlans
            draft=TravelPlans(self.db).load(user,owned['session_id']) or {}
            assumed=draft.get('assumptions',{})
            destination=assumed.get('date_destination','')
            if assumed.get('departure_date') and assumed.get('return_date') and any(same_city(destination,q.criteria.get('destination','')) for q in queries):
                trip={'start':assumed['departure_date'],'end':assumed['return_date'],'location':destination}
                self.db.event(run,'travel_assumptions',{key:assumed[key] for key in ('departure_date','return_date')})
                self.db.event(run,'trip_requested',{'source_message':source['id'],'criteria':trip})
            for query in queries:
                if query.domain!='flights':continue
                c=query.criteria;destination=c.get('destination','')
                dates={}
                if same_city(destination,trip.get('location','')):dates={'departure_date':trip['start'],'return_date':trip['end']}
                else:
                    stays={(q.criteria.get('check_in'),q.criteria.get('check_out')) for q in queries
                           if q.domain=='hotels' and same_city(destination,q.criteria.get('destination',''))
                           and q.criteria.get('check_in') and q.criteria.get('check_out')}
                    if len(stays)==1:
                        start,end=next(iter(stays));dates={'departure_date':start,'return_date':end}
                for field,value in dates.items():
                    if field=='return_date' and ('one-way' in text or 'one way' in text):continue
                    if not c.get(field):
                        c[field]=value
                        self.db.event(run,'trip_date_inherited',{'field':field,'date':value,'destination':destination})
            for query in queries:
                if query.domain!='hotels':continue
                destination=query.criteria.get('destination','')
                flight=next((q.criteria for q in queries if q.domain=='flights' and same_city(destination,q.criteria.get('destination',''))),{})
                for field,key,trip_key in [('check_in','departure_date','start'),('check_out','return_date','end')]:
                    value=flight.get(key) or (trip.get(trip_key) if same_city(destination,trip.get('location','')) else None)
                    if not query.criteria.get(field) and value:query.criteria[field]=value
        for query in queries:
            # Connections are derived from researched routes by Travel, never
            # supplied by the main model (including on a named-airline query).
            if query.domain=='flights':query.criteria.pop('connections',None)
            try:
                c,missing=criteria(query.domain,query.criteria)
                query.criteria=c
            except (ValueError,TypeError):continue
            if missing:continue
            candidates=self.store.rows('''SELECT q.*,s.state AS watch_state FROM queries q LEFT JOIN subscriptions s ON s.id=q.id
                WHERE q.user_id=? AND q.session_id=? AND q.domain=? ORDER BY q.updated DESC''',(user,owned['session_id'],query.domain))
            old=next((q for q in candidates if q['watch_state']!='cancelled' and identity(query.domain,json.loads(q['criteria']))==identity(query.domain,c)),None)
            query.query_id=old['id'] if old else 'av-'+secrets.token_hex(20)
        # Model arguments cannot choose ownership, callback URLs, or workflow IDs.
        preferences=self.db.rows("SELECT memory_key,text FROM facts WHERE user_id=? AND status='active' AND memory_key LIKE 'preference.%' LIMIT 20",(user,))
        terms={'dining':('diet','cuisine','food','dining','restaurant','budget'),
            'travel':('travel','hotel','flight','cabin','airline','seat','room','budget','accommodation')}[role]
        preferences=[p for p in preferences if any(term in p['memory_key'].removeprefix('preference.') for term in terms)]
        if role=='travel':self.db.event(run,'trip_context',{'queries':[q.model_dump() for q in queries]})
        prefs={p['memory_key']:p['text'] for p in preferences}
        if role=='travel':
            from .option_counts import policy
            prefs['option_counts']=policy(self.db,run,source['content'],{q.domain for q in queries})
        from .intervals import requested,DEFAULT
        cadence=requested(source['content'])
        prefs['monitor_interval_seconds']=cadence or DEFAULT
        prefs['monitor_interval_explicit']=cadence is not None
        if role=='travel' and self.db.one("SELECT id FROM events WHERE run_id=? AND kind='trip_requested' LIMIT 1",(run,)):
            prefs['arrival_aligned_stay']=not bool(re.search(r'check[- ]?in',text))
            current=[json.loads(row['payload']) for row in self.db.rows("SELECT payload FROM events WHERE run_id=? AND kind='availability_result' ORDER BY id",(run,))]
            arrivals=[o['arrival'] for g in current for r in leaves(g.get('results',[])) if r['domain']=='flights' for o in r.get('options',[]) if o.get('arrival')]
            if arrivals:prefs['flight_arrival']=arrivals[0]
        if avoid_venues:prefs['avoid_venues']=avoid_venues
        return Task(request_id=digest([run,role,raw,prefs,targets,watch_policy])[:40],user_id=user,session_id=owned['session_id'],source_message_id=source['id'],queries=queries,preferences=prefs,requested_hotels=targets,watch_policy=watch_policy)

    async def delegate(self,user,run,raw,role,monitor=False,deadline='',watch_policy='normal'):
        marker=self.db.one("SELECT payload FROM events WHERE run_id=? AND kind='travel_intent' ORDER BY id DESC LIMIT 1",(run,))
        if marker and json.loads(marker['payload'])['intent'] in {'explore','compare'}:watch_policy='observe_only'
        task=self.envelope(user,run,raw,role,watch_policy)
        if monitor and task.watch_policy=='observe_only':raise ValueError('Exploratory checks cannot create monitors')
        if monitor:
            response=await self.create_monitor(task,deadline)
            sources=[s for research in response.get('research',[]) for s in research.get('sources',[])]
            if sources:self.db.event(run,'sources',sources)
            if 'results' in response:self.db.event(run,'availability_result',response)
            return response
        response=await self.check(task)
        response['watch_policy']=task.watch_policy
        sources=[s for research in response.get('research',[]) for s in research.get('sources',[])]
        if sources:self.db.event(run,'sources',sources)
        for result in response['results']:
            if result['domain']!='dining' or result['status']!='completed':continue
            venue=identity('dining',result['criteria']).get('venue')
            for draft in self.db.rows("SELECT id,criteria FROM dining_watches WHERE user_id=? AND session_id=? AND status='needs_details'",(user,task.session_id)):
                if identity('dining',json.loads(draft['criteria'])).get('venue')==venue:
                    self.db.execute("UPDATE dining_watches SET status='cancelled',updated=? WHERE id=? AND status='needs_details'",(time.time(),draft['id']))
        await self.prepare_watches(task,response)
        self.db.event(run,'dining_lookup' if role=='dining' else 'travel_lookup',response)
        self.db.event(run,'availability_result',response)
        from .streaming import publish
        publish(self.db,user,run)
        return response

    async def check(self,task):
        roles={role_for(q.domain) for q in task.queries}
        async def one(role):
            queries=[q for q in task.queries if role_for(q.domain)==role]
            scoped=task.model_copy(update={'request_id':task.request_id+'-'+role,'queries':queries})
            try:
                result=await self.api(role,'/v1/availability/query',task.user_id,scoped.model_dump())
                if result.get('request_id')!=scoped.request_id or len(result.get('results',[]))!=len(queries):raise ValueError('Subagent correlation mismatch')
                arrivals=[o['arrival'] for r in leaves(result['results']) if r['domain']=='flights' and r.get('status')=='completed' for o in r.get('options',[]) if o.get('arrival')]
                arrival=arrivals[0] if arrivals else task.preferences.get('flight_arrival','')
                for expected,response in zip(queries,result['results']):
                    try:c,_=criteria(expected.domain,expected.criteria)
                    except (ValueError,TypeError):c=expected.criteria
                    qid=expected.query_id or 'av-'+digest([task.user_id,task.session_id,task.scope_id or scoped.request_id,expected.domain,c])[:40]
                    if expected.domain=='hotels' and task.preferences.get('arrival_aligned_stay') and arrival and c.get('check_in','')<arrival[:10]<c.get('check_out',''):
                        c={**c,'check_in':arrival[:10]}
                        qid=candidate_id(qid,'hotels',c)
                    if response.get('status') not in {'completed','needs_input','error'}:raise ValueError('Invalid result status')
                    if response.get('domain')!=expected.domain or response.get('criteria')!=c:raise ValueError('Subagent query mismatch')
                    if (response.get('status')=='completed' or response.get('query_id')) and response.get('query_id')!=qid:raise ValueError('Subagent query mismatch')
                    for child in response.get('candidate_results',[]):
                        cc=child['criteria'];resource={'hotels':'hotel','dining':'venue','flights':'carrier'}[expected.domain]
                        if child.get('domain')!=expected.domain or not cc.get(resource) or child.get('query_id')!=candidate_id(qid,expected.domain,cc):raise ValueError('Candidate correlation mismatch')
                        if any(cc.get(k)!=v for k,v in c.items() if k not in {resource,'connections'}):raise ValueError('Candidate changed user criteria')
                        if child['status']=='completed' and (child.get('provenance')!='Company '+expected.domain or type(child.get('available')) is not bool or bool(child.get('options'))!=child['available']):raise ValueError('Candidate availability is invalid')
                return {'results':result['results'],'research':result.get('research',[])}
            except Exception as exc:
                return {'results':[{'domain':q.domain,'criteria':q.criteria,'status':'error','error':type(exc).__name__} for q in queries],'research':[]}
        groups=await asyncio.gather(*(one(role) for role in sorted(roles)))
        return {'request_id':task.request_id,'results':[r for group in groups for r in group['results']],
            'research':[r for group in groups for r in group['research']]}

    async def parent(self,task,expiry,query_ids=None):
        if task.watch_policy=='observe_only':raise ValueError('Exploratory checks cannot start workflows')
        if self.client is None:raise RuntimeError('Availability workflow worker is not ready')
        wid='av-main-'+(task.scope_id or task.request_id)
        if query_ids is None:
            query_ids=[q.query_id or 'av-'+digest([task.user_id,task.session_id,task.scope_id or task.request_id+'-'+role_for(q.domain),q.domain,criteria(q.domain,q.criteria)[0]])[:40] for q in task.queries]
        try:await self.client.start_workflow(MainAvailability.run,{'expires':expiry,'recurring':bool(task.scope_id),'query_ids':query_ids or []},id=wid,task_queue='aster-main-v3',id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        except WorkflowAlreadyStartedError:pass
        return wid

    async def prepare_watches(self,task,response,expiry=None):
        if task.watch_policy=='observe_only':return
        if task.scope_id:
            monitor=self.store.one('SELECT state FROM monitors WHERE id=?',(task.scope_id,))
            if not monitor or monitor['state']!='active':return
        pending=[r for r in leaves(response['results']) if r['status']=='completed' and not r['available']]
        # Repeated checks reuse the original receiving workflow and Company
        # subscription, including after a spelling alias or case change.
        pending=[r for r in pending if not self.store.one("SELECT id FROM subscriptions WHERE id=? AND state IN ('active','opened')",(r['query_id'],))]
        for r in leaves(response['results']):
            if r.get('query_id') and not task.scope_id:
                self.store.execute('INSERT OR IGNORE INTO changes VALUES(?,?,?,?,?,?)',
                    (digest([r['query_id'],semantic(r)]),task.user_id,task.session_id,r['query_id'],json.dumps(r),time.time()))
        if not pending:return
        expiry=expiry or max(expires_at([r]) for r in pending)
        wid=await self.parent(task,expiry,[r['query_id'] for r in pending])
        for result in pending:
            now=time.time()
            self.db.execute('INSERT OR IGNORE INTO dining_watches VALUES(?,?,?,?,?,?,?,?,?)',
                (result['query_id'],task.user_id,task.session_id,task.source_message_id,json.dumps({**result['criteria'],'domain':result['domain'],'version':3}),result['query_id'],'active',now,now))
        for role in {role_for(r['domain']) for r in pending}:
            command={'role':role,'user':task.user_id,'request_id':task.request_id+'-'+role,'parent_workflow_id':wid}
            for result in pending:
                if role_for(result['domain'])!=role:continue
                self.store.execute('INSERT OR IGNORE INTO subscriptions VALUES(?,?,?,?,?,?,?,?)',
                    (result['query_id'],task.user_id,command['request_id'],result['domain'],wid,min(expiry,expires_at([result])),'active',time.time()))
            did='watch:'+command['request_id']
            self.store.execute("INSERT OR IGNORE INTO delivery VALUES(?,'watch',?,?)",(did,json.dumps(command),time.time()))
            try:await self.send_watch(command,did)
            except Exception:pass # Durable main outbox retries without losing early events.

    async def send_watch(self,command,did):
        await self.api(command['role'],'/v1/watches',command['user'],{'request_id':command['request_id'],'user_id':command['user'],'parent_workflow_id':command['parent_workflow_id']})
        self.store.execute("UPDATE delivery SET state='done' WHERE id=?",(did,))

    @activity.defn(name='availability_publish')
    async def publish(self,payload:dict):
        qid=payload['query_id'];event=payload['event']
        q=self.store.one('SELECT * FROM queries WHERE id=?',(qid,))
        if not q:return
        sub=self.store.one('SELECT * FROM subscriptions WHERE id=?',(qid,))
        if not sub or sub['state']=='cancelled' or sub['expires']<time.time():return
        try:workflow_id=activity.info().workflow_id
        except RuntimeError:workflow_id=None
        if workflow_id and workflow_id!=sub['parent']:raise ValueError('Opening delivered to the wrong main workflow')
        expected=identity(q['domain'],json.loads(q['criteria']))
        if (event.get('query_id')!=qid or event.get('domain')!=q['domain'] or event.get('criteria_hash')!=digest(expected)
            or identity(q['domain'],event.get('criteria',{}))!=expected or event.get('event_id')!='opening:'+qid
            or event.get('available') is not True or event.get('revision')!=1):raise ValueError('Invalid main opening event')
        result={**deepcopy(event),'status':'completed'}
        # Company events establish inventory, while the scoped subagent response
        # retains the public sources needed for descriptions and venue photos.
        saved=self.store.one('SELECT result FROM tasks WHERE id=? AND user_id=? AND session_id=?',(sub['request_id'],q['user_id'],q['session_id']))
        if saved:
            previous=next((r for r in leaves(json.loads(saved['result'] or '{}').get('results',[])) if r.get('query_id')==qid),None)
            if previous:
                for field in ('sources','description'):
                    if not result.get(field) and previous.get(field):result[field]=previous[field]
        from .media import enrich
        await enrich(self.store,[result])
        self.project(q,result)

    def project(self,q,result,monitor_id=None):
        """One transaction across attached local stores: cancellation, dedupe, chat, alert."""
        now=time.time()
        with self.db.connect() as db:
            db.execute('ATTACH DATABASE ? AS availability',(str(self.store.path),))
            db.execute('BEGIN IMMEDIATE')
            sub=db.execute('SELECT state,parent FROM availability.subscriptions WHERE id=?',(q['id'],)).fetchone()
            if sub and sub['state']=='cancelled':return
            mid=monitor_id or (sub['parent'].removeprefix('av-main-') if sub else None)
            monitor=db.execute('SELECT state,expires FROM availability.monitors WHERE id=?',(mid,)).fetchone() if mid else None
            if monitor and (monitor['state']!='active' or monitor['expires']<now):return
            if not db.execute('SELECT id FROM sessions WHERE id=? AND user_id=?',(q['session_id'],q['user_id'])).fetchone():return
            # Critical delivery deduplication lives in the SAME database as
            # chat/alerts; attached WAL stores are not crash-atomic together.
            last=db.execute("SELECT id,payload FROM availability_events WHERE watch_id=? AND kind LIKE 'availability:%' ORDER BY created DESC,rowid DESC LIMIT 1",(q['id'],)).fetchone()
            if last and semantic(json.loads(last['payload']))==semantic(result):return
            eid=digest([q['id'],semantic(result),last['id'] if last else None])
            c=result['criteria'];label=c.get('venue') or c.get('hotel') or c.get('carrier') or c.get('destination') or result['domain']
            title=label+(' is now available' if result.get('available') else ' availability update')
            body=describe(result)
            from .presentation import grouped_options
            chat_body=title+'.\n\n'+grouped_options([result]) if result.get('status')=='completed' and result.get('available') else body
            # Keep the legacy physical projection/FKs; new structured state is independent.
            source=db.execute("SELECT id FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",(q['session_id'],)).fetchone()
            if not source:return
            db.execute('INSERT OR IGNORE INTO dining_watches VALUES(?,?,?,?,?,?,?,?,?)',
                (q['id'],q['user_id'],q['session_id'],source['id'],json.dumps({**c,'domain':result['domain'],'version':3}),q['id'],'notified',now,now))
            db.execute("UPDATE dining_watches SET status='notified',updated=? WHERE id=?",(now,q['id']))
            db.execute('INSERT INTO availability_events VALUES(?,?,?,?,?,?)',(eid,q['id'],q['user_id'],'availability:'+eid,json.dumps(result),now))
            db.execute('INSERT OR IGNORE INTO notifications(user_id,event_id,title,body,created) VALUES(?,?,?,?,?)',(q['user_id'],eid,title,body,now))
            message=db.execute("INSERT INTO messages(session_id,role,content,created) VALUES(?,'assistant',?,?)",(q['session_id'],chat_body,now)).lastrowid
            db.execute('INSERT INTO availability_chat_messages VALUES(?,?)',(eid,message))
            db.execute('INSERT INTO availability.changes VALUES(?,?,?,?,?,?)',(eid,q['user_id'],q['session_id'],q['id'],json.dumps(result),now))
            db.execute('INSERT INTO pending_memory_summaries VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET through_message=MAX(through_message,excluded.through_message)',(q['session_id'],q['user_id'],message))

    async def create_monitor(self,task,deadline):
        if task.watch_policy=='observe_only':raise ValueError('Exploratory checks cannot create monitors')
        normalized=[];needs_input=[]
        for q in task.queries:
            try:c,missing=criteria(q.domain,q.criteria)
            except (ValueError,TypeError) as exc:
                from .contracts import validation_result
                needs_input.append(validation_result(q.domain,q.criteria,exc));continue
            if missing:
                needs_input.append({'status':'needs_input','domain':q.domain,'criteria':c,'missing_fields':missing});continue
            normalized.append({'domain':q.domain,'criteria':c})
        if not normalized:return {'status':'needs_input' if any(r['status']=='needs_input' for r in needs_input) else 'error','results':needs_input}
        expiry=max(expires_at([q]) for q in normalized)
        if deadline:
            end=datetime.fromisoformat(deadline)
            if end.tzinfo is None:end=end.replace(tzinfo=ZoneInfo(config.TIMEZONE))
            expiry=min(expiry,end.timestamp())
        if expiry<=time.time():raise ValueError('Monitoring deadline has expired')
        mid=digest([task.user_id,task.session_id,normalized])[:40]
        task=task.model_copy(update={'scope_id':mid,'queries':[Query.model_validate(q) for q in normalized]})
        old=self.store.one('SELECT * FROM monitors WHERE id=?',(mid,))
        if old and old['state'] in {'active','pending'}:
            if task.preferences.get('monitor_interval_explicit') is False:
                from .intervals import seconds
                previous=Task.model_validate_json(old['envelope'])
                task.preferences['monitor_interval_seconds']=seconds(previous.preferences)
            expiry=min(expiry,old['expires'])
            self.store.execute('UPDATE monitors SET expires=?,envelope=? WHERE id=?',(expiry,task.model_dump_json(),mid))
            response=await self.check(task)
            self.store.execute('UPDATE monitors SET last_check=?,last_result=? WHERE id=?',(time.time(),json.dumps(response),mid))
            await self.prepare_watches(task,response,expiry)
            await self.ensure_schedule(mid)
            from .intervals import seconds
            return {**response,'results':response['results']+needs_input,'status':'monitoring','monitor_id':mid,'interval_seconds':seconds(task.preferences)}
        # A new ID after cancellation prevents old Company events from reviving a watch.
        if old:mid=digest([mid,time.time_ns()])[:40];task=task.model_copy(update={'scope_id':mid})
        self.store.execute('INSERT INTO monitors(id,user_id,session_id,envelope,expires,state,schedule_id,anchor) VALUES(?,?,?,?,?,?,?,?)',(mid,task.user_id,task.session_id,task.model_dump_json(),expiry,'pending','av-schedule-'+mid,time.time()))
        await self.parent(task,expiry)
        response=await self.check(task)
        self.store.execute("UPDATE monitors SET state='active',last_check=?,last_result=?,last_hash=? WHERE id=? AND state='pending'",(time.time(),json.dumps(response),digest([semantic(r) for r in response['results']]),mid))
        if self.store.one('SELECT state FROM monitors WHERE id=?',(mid,))['state']!='active':return {'status':'cancelled','monitor_id':mid}
        for r in response['results']:
            if r.get('query_id'):self.store.execute('INSERT OR IGNORE INTO changes VALUES(?,?,?,?,?,?)',(digest([r['query_id'],semantic(r)]),task.user_id,task.session_id,r['query_id'],json.dumps(r),time.time()))
        await self.prepare_watches(task,response,expiry)
        await self.ensure_schedule(mid)
        from .intervals import seconds
        return {**response,'results':response['results']+needs_input,'status':'monitoring','monitor_id':mid,'interval_seconds':seconds(task.preferences)}

    async def ensure_schedule(self,mid):
        row=self.store.one('SELECT * FROM monitors WHERE id=?',(mid,))
        if not row or row['state']!='active':return
        task=Task.model_validate_json(row['envelope'])
        from .intervals import requested,seconds
        if 'monitor_interval_seconds' not in task.preferences:
            source=self.db.one("SELECT content FROM messages WHERE id=? AND session_id=? AND role='user'",(task.source_message_id,task.session_id))
            task.preferences['monitor_interval_seconds']=requested(source['content']) if source else None
            task.preferences['monitor_interval_seconds']=task.preferences['monitor_interval_seconds'] or 300
            self.store.execute('UPDATE monitors SET envelope=? WHERE id=?',(task.model_dump_json(),mid))
        interval=seconds(task.preferences)
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=interval),offset=timedelta(seconds=(row['anchor'] or row['last_check'] or time.time())%interval))],end_at=datetime.fromtimestamp(row['expires'],ZoneInfo('UTC')))
        try:
            await self.client.create_schedule(row['schedule_id'],Schedule(
                action=ScheduleActionStartWorkflow(AvailabilityCheck.run,{'monitor_id':mid},id='av-poll-'+mid,task_queue='aster-main-v3',execution_timeout=timedelta(seconds=120)),
                spec=spec,
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP,catchup_window=timedelta(minutes=5))))
        except Exception as exc:
            from temporalio.client import ScheduleAlreadyRunningError
            if not isinstance(exc,ScheduleAlreadyRunningError):raise
        handle=self.client.get_schedule_handle(row['schedule_id'])
        description=await handle.describe()
        actual=description.schedule.spec
        if not actual.intervals or actual.intervals[0].every!=timedelta(seconds=interval) or actual.end_at!=spec.end_at:
            def update(current):
                schedule=current.description.schedule;schedule.spec=spec
                return ScheduleUpdate(schedule=schedule)
            await handle.update(update)
            description=await handle.describe()
        next_times=description.info.next_action_times
        self.store.execute('UPDATE monitors SET next_check=? WHERE id=?',(next_times[0].timestamp() if next_times else None,mid))

    @activity.defn(name='availability_poll')
    async def poll(self,payload:dict):
        row=self.store.one("SELECT * FROM monitors WHERE id=? AND state='active'",(payload['monitor_id'],))
        if not row:return
        if row['expires']<=time.time():await self.cancel(row['id'],row['user_id'],expired=True);return
        task=Task.model_validate_json(row['envelope'])
        live=[q for q in task.queries if expires_at([{'domain':q.domain,'criteria':q.criteria}])>time.time()]
        if not live:await self.cancel(row['id'],row['user_id'],expired=True);return
        task=task.model_copy(update={'queries':live})
        task=task.model_copy(update={'request_id':digest([task.scope_id,time.time_ns()])[:40]})
        response=await self.check(task)
        # Each logical category has its own state; an independent failure never hides success.
        previous=json.loads(row['last_result'] or '{"results":[]}')['results']
        previous_by_query={digest([r['domain'],identity(r['domain'],r['criteria'])]):r for r in leaves(previous)}
        for r in leaves(response['results']):
            qid=r.get('query_id') or 'av-'+digest([task.user_id,task.session_id,task.scope_id,r['domain'],r['criteria']])[:40]
            r['query_id']=qid
            prior=previous_by_query.get(digest([r['domain'],identity(r['domain'],r['criteria'])]))
            if prior and semantic(prior)==semantic(r):continue
            # No cached snapshot is used as the result of a check.
            self.project({'id':qid,'user_id':task.user_id,'session_id':task.session_id},r,row['id'])
        self.store.execute('UPDATE monitors SET last_check=?,last_result=?,last_hash=? WHERE id=?',(time.time(),json.dumps(response),digest([semantic(r) for r in response['results']]),row['id']))
        await self.prepare_watches(task,response,row['expires'])

    async def cancel(self,mid,user,expired=False):
        row=self.store.one('SELECT * FROM monitors WHERE id=? AND user_id=?',(mid,user))
        if not row:raise LookupError('Monitor not found')
        self.store.execute('UPDATE monitors SET state=? WHERE id=?',('expired' if expired else 'cancelled',mid))
        self.store.execute("INSERT OR IGNORE INTO delivery VALUES(?,'stop-monitor',?,?)",('stop:'+mid,json.dumps(row),time.time()))
        for sub in self.store.rows('SELECT * FROM subscriptions WHERE parent=? AND user_id=?',('av-main-'+mid,user)):
            await self.cancel_watch(sub['id'],user)
        if self.client:
            try:await self.client.get_schedule_handle(row['schedule_id']).delete()
            except Exception:pass
        return {'cancelled':True}

    async def cancel_watch(self,qid,user):
        sub=self.store.one('SELECT * FROM subscriptions WHERE id=? AND user_id=?',(qid,user))
        if not sub:raise LookupError('Watch not found')
        self.store.execute("UPDATE subscriptions SET state='cancelled' WHERE id=?",(qid,))
        self.store.execute("INSERT OR IGNORE INTO delivery VALUES(?,'cancel',?,?)",('cancel:'+qid,json.dumps(sub),time.time()))
        self.db.execute("UPDATE dining_watches SET status='cancelled' WHERE id=? AND user_id=?",(qid,user))
        return {'cancelled':True}

    def monitors(self,user):
        rows=self.store.rows('SELECT id,state,expires,last_check,next_check,envelope FROM monitors WHERE user_id=? ORDER BY rowid DESC LIMIT 30',(user,))
        for row in rows:
            envelope=json.loads(row.pop('envelope'));row['queries']=envelope['queries']
            from .intervals import seconds
            row['interval_seconds']=seconds(envelope.get('preferences',{}))
            for query in row['queries']:query['state']='expired' if expires_at([query])<=time.time() else row['state']
            if row['state']!='active':row['next_check']=None
            elif row['next_check'] and row['next_check']>row['expires']:row['next_check']=None
        return rows

    async def maintain(self):
        while True:
            try:
                for row in self.store.rows("SELECT * FROM delivery WHERE state='stop-monitor'"):
                    monitor=json.loads(row['payload'])
                    try:
                        await self.client.get_schedule_handle(monitor['schedule_id']).delete()
                    except Exception as exc:
                        from temporalio.service import RPCError,RPCStatusCode
                        if not isinstance(exc,RPCError) or exc.status!=RPCStatusCode.NOT_FOUND:continue
                    try:await self.client.get_workflow_handle('av-main-'+monitor['id']).cancel()
                    except Exception:pass
                    self.store.execute("UPDATE delivery SET state='done' WHERE id=?",(row['id'],))
                for row in self.store.rows("SELECT * FROM delivery WHERE state='watch'"):
                    command=json.loads(row['payload'])
                    monitor=self.store.one('SELECT state FROM monitors WHERE id=?',(command['parent_workflow_id'].removeprefix('av-main-'),))
                    if monitor and monitor['state'] not in {'active','pending'}:self.store.execute("UPDATE delivery SET state='done' WHERE id=?",(row['id'],));continue
                    try:await self.send_watch(command,row['id'])
                    except Exception:pass
                for row in self.store.rows("SELECT * FROM monitors WHERE state IN ('pending','active')"):
                    if row['expires']<time.time():await self.cancel(row['id'],row['user_id'],expired=True)
                    elif row['state']=='pending':
                        if row['anchor'] and time.time()-row['anchor']<120:continue
                        # Recover a process interruption during immediate checking.
                        self.store.execute("UPDATE monitors SET state='active' WHERE id=?",(row['id'],))
                        await self.parent(Task.model_validate_json(row['envelope']),row['expires'])
                        await self.poll({'monitor_id':row['id']});await self.ensure_schedule(row['id'])
                    else:await self.ensure_schedule(row['id'])
            except Exception:pass
            await asyncio.sleep(5)
