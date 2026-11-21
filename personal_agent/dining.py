"""Aster's Company MCP client, Temporal worker and owned dining requests."""
import asyncio
import hashlib
import json
import secrets
import time
from datetime import date

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from . import config
from .dining_workflow import DiningWorkflow,DiningWatchWorkflow


class Dining:
    def __init__(self,service,client=None,company_url=None,task_queue=None):
        self.service,self.db=service,service.db
        self.client=client;self.company_url=company_url or config.DINING_COMPANY_URL
        self.task_queue=task_queue or config.DINING_TASK_QUEUE
        self.worker=None;self.worker_task=None

    async def connect(self):
        if self.client is None:self.client=await Client.connect(config.TEMPORAL_ADDRESS,namespace=config.TEMPORAL_NAMESPACE)
        return self.client

    async def start(self):
        client=await self.connect()
        self.db.migrate_dining()
        self.worker=Worker(client,task_queue=self.task_queue,workflows=[DiningWorkflow,DiningWatchWorkflow],
            activities=[self.company_search,self.register_watch,self.publish_opening])
        self.worker_task=asyncio.create_task(self.worker.run())

    async def close(self):
        if self.worker:
            await self.worker.shutdown()
            await self.worker_task
        self.worker=None;self.worker_task=None

    async def call_company(self,tool_name,query):
        from availability.security import Security
        import httpx
        async with httpx.AsyncClient(headers=Security(config.RUNTIME).headers(),timeout=20) as http:
            async with streamable_http_client(self.company_url,http_client=http) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    result=await session.call_tool(tool_name,query)
                    if result.isError:raise RuntimeError('Company dining request failed')
                    response=result.structuredContent
                    if response is None:response=json.loads(next(c.text for c in result.content if c.type=='text'))
        if (not isinstance(response,dict) or any(response.get(k)!=v for k,v in query.items())
            or type(response.get('available')) is not bool or not isinstance(response.get('slots'),list)
            or any(not isinstance(slot,str) for slot in response['slots'])):
            raise ValueError('Company returned invalid or mismatched dining availability')
        return response

    @activity.defn(name='company_search')
    async def company_search(self,request:dict)->dict:
        # Retain old activity names to replay/drain already-started workflows.
        response=await self.call_company('search_dining',request['company_query'])
        if not response['available']:await self.call_company('watch_dining',request['company_query'])
        else:self.db.execute("UPDATE dining_watches SET status='available',updated=? WHERE id=? AND user_id=? AND status='active'",
            (time.time(),request['watch_id'],request['user_id']))
        return response

    @activity.defn(name='register_dining_watch')
    async def register_watch(self,request:dict)->dict:
        watch=self.db.one("SELECT id FROM dining_watches WHERE id=? AND user_id=? AND status='active'",
            (request['watch_id'],request['user_id']))
        if not watch:
            return {'watching':False,'status':'cancelled'}
        return await self.call_company('watch_dining',request['company_query'])

    @activity.defn(name='publish_dining_opening')
    async def publish_opening(self,payload:dict):
        request,event=payload['request'],payload['event'];now=time.time()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            watch=conn.execute("SELECT w.* FROM dining_watches w JOIN sessions s ON s.id=w.session_id AND s.user_id=w.user_id WHERE w.id=? AND w.user_id=? AND w.status='active'",
                (request['watch_id'],request['user_id'])).fetchone()
            if not watch:return
            c=json.loads(watch['criteria']);eid=event['event_id']
            if any(event.get(k)!=request['company_query'][k] for k in ('request_id','venue','location','date','party_size')):
                raise ValueError('Availability event does not match the request')
            title=c['venue']+' is now available'
            body=f"A table has opened up at {c['venue']} for {c['party_size']} in {c['location']} on {c['date']} at {', '.join(event['slots'])}."
            conn.execute("INSERT OR IGNORE INTO availability_events VALUES(?,?,?,'company_opening',?,?)",
                (eid,watch['id'],watch['user_id'],json.dumps(event),now))
            conn.execute('INSERT OR IGNORE INTO notifications(user_id,event_id,title,body,created) VALUES(?,?,?,?,?)',
                (watch['user_id'],eid,title,body,now))
            if not conn.execute('SELECT message_id FROM availability_chat_messages WHERE event_id=?',(eid,)).fetchone():
                mid=conn.execute("INSERT INTO messages(session_id,role,content,created) VALUES(?,'assistant',?,?)",
                    (watch['session_id'],body,now)).lastrowid
                conn.execute('INSERT INTO availability_chat_messages VALUES(?,?)',(eid,mid))
                conn.execute('''INSERT INTO pending_memory_summaries VALUES(?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET through_message=MAX(through_message,excluded.through_message)''',
                    (watch['session_id'],watch['user_id'],mid))
            conn.execute("UPDATE dining_watches SET status='notified',updated=? WHERE id=?",(now,watch['id']))

    async def ensure_workflow(self,watch):
        criteria=json.loads(watch['criteria']);wid='aster-dining-'+watch['id']
        request={'watch_id':watch['id'],'user_id':watch['user_id'],'session_id':watch['session_id'],
            'company_query':{'request_id':wid,**{k:criteria[k] for k in ('venue','location','date','party_size')}}}
        client=await self.connect()
        workflow_run=DiningWatchWorkflow.run if criteria.get('workflow_version')==2 else DiningWorkflow.run
        try:await client.start_workflow(workflow_run,request,id=wid,task_queue=self.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        except WorkflowAlreadyStartedError:pass

    async def dispatch(self,job):
        watch=self.db.one("SELECT * FROM dining_watches WHERE id=? AND user_id=? AND status='active'",
            (json.loads(job['payload'])['watch_id'],job['user_id']))
        if watch:await self.ensure_workflow(watch)
        self.db.execute("UPDATE background_jobs SET state='complete',lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?",
            (job['id'],job['lease_token']))

    async def search_for_agent(self,user,run,source_message,venue,location='',day='',party_size=0):
        """Called by the agent's tool: MCP lookup first, then an unavailable-only watch."""
        owned=self.db.one("SELECT * FROM runs WHERE id=? AND user_id=? AND status IN ('running','resuming')",(run,user))
        if not owned:raise LookupError('Active dining request not found')
        source=self.db.one("SELECT id FROM messages WHERE id=? AND session_id=? AND role='user'",(source_message,owned['session_id']))
        if not source:raise LookupError('Owned user request not found')
        venue=venue.strip();location=location.strip()
        if not 2<=len(venue)<=100:raise ValueError('Provide a venue name from the user request.')
        missing=[name for name,value in [('location',location),('date',day),('party_size',party_size)] if not value]
        if location and not 2<=len(location)<=80:raise ValueError('Provide the venue location/city.')
        if party_size and not 1<=party_size<=20:raise ValueError('Party size must be from 1 to 20.')
        if day:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            parsed=date.fromisoformat(day)
            if parsed<datetime.now(ZoneInfo(config.TIMEZONE)).date():raise ValueError('Choose a current or future date.')
        c={'venue':venue,'location':location,'date':day,'party_size':party_size,'workflow_version':2}
        key=hashlib.sha256(json.dumps([user,venue.casefold(),location.casefold(),day,party_size]).encode()).hexdigest() if not missing else None
        now=time.time()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing=conn.execute("SELECT * FROM dining_watches WHERE user_id=? AND criteria_key=? AND status IN ('checking','active')",(user,key)).fetchone() if key else None
            pending=conn.execute("SELECT * FROM dining_watches WHERE user_id=? AND session_id=? AND status='needs_details' ORDER BY created DESC",(user,owned['session_id'])).fetchall()
            draft=next((row for row in pending if json.loads(row['criteria']).get('venue','').casefold()==venue.casefold()),None)
            if existing:
                wid=existing['id']
                c=json.loads(existing['criteria'])
                if draft:conn.execute("UPDATE dining_watches SET status='cancelled',updated=? WHERE id=?",(now,draft['id']))
            elif draft:
                wid=draft['id']
                conn.execute('UPDATE dining_watches SET criteria=?,criteria_key=?,source_message=?,status=?,updated=? WHERE id=?',
                    (json.dumps(c),key,source_message,'needs_details' if missing else 'checking',now,wid))
            else:
                wid=secrets.token_hex(16)
                conn.execute('INSERT INTO dining_watches VALUES(?,?,?,?,?,?,?,?,?)',
                    (wid,user,owned['session_id'],source_message,json.dumps(c),key,'needs_details' if missing else 'checking',now,now))
        if missing:return {'status':'needs_details','venue':venue,'missing':missing}
        query={'request_id':'aster-dining-'+wid,**{k:c[k] for k in ('venue','location','date','party_size')}}
        self.db.event(run,'progress','Checking dining availability…')
        response=await self.call_company('search_dining',query)
        self.db.event(run,'dining_lookup',{'venue':venue,'available':response['available'],'watch_id':wid})
        if response['available']:
            self.db.execute("UPDATE dining_watches SET status='available',updated=? WHERE id=? AND status='checking'",(time.time(),wid))
            return {**response,'watch_status':'not_needed'}
        with self.db.connect() as conn:
            conn.execute("UPDATE dining_watches SET status='active',updated=? WHERE id=? AND status='checking'",(time.time(),wid))
            conn.execute("INSERT OR IGNORE INTO background_jobs(id,kind,user_id,payload,next_at,updated) VALUES(?,'dining_start',?,?,?,?)",
                ('watch:'+wid,user,json.dumps({'watch_id':wid}),time.time(),time.time()))
        try:
            async with asyncio.timeout(5):
                await self.ensure_workflow(self.db.one('SELECT * FROM dining_watches WHERE id=?',(wid,)))
            self.db.execute("UPDATE background_jobs SET state='complete' WHERE id=? AND state='queued'",('watch:'+wid,))
            return {**response,'watch_status':'active'}
        except Exception:
            return {**response,'watch_status':'queued'}
