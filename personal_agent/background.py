"""Idle memory maintenance and durable Temporal workflow dispatch."""
import asyncio
import json
import logging
import secrets
import sqlite3
import time

from . import config
from .memory_profile import MemoryConsolidator,read_profile


class Background:
    def __init__(self,service,clock=time.time):
        self.service,self.db,self.clock=service,service.db,clock
        self.consolidator=MemoryConsolidator(self.db,service.engine.model)
        self.runner=None;self.tasks={};self.last_seed=0

    def queue_memory(self,user,immediate=True):
        now=self.clock();key='memory:'+user
        self.db.execute('''INSERT OR IGNORE INTO background_jobs(id,kind,user_id,payload,next_at,updated)
            VALUES(?,'memory',?,'{}',?,?)''',(key,user,now if immediate else now+60,now))
        if immediate:self.db.execute("UPDATE background_jobs SET next_at=?,state='queued',last_error=NULL WHERE id=? AND state!='running'",(now,key))

    def cancel_watch(self,user,wid):
        with self.db.connect() as conn:
            row=conn.execute('SELECT id FROM dining_watches WHERE id=? AND user_id=?',(wid,user)).fetchone()
            if not row:raise LookupError('Watch not found')
            conn.execute("UPDATE dining_watches SET status='cancelled',updated=? WHERE id=?",(self.clock(),wid))
            conn.execute("UPDATE background_jobs SET state='cancelled',lease_token=NULL WHERE id=? AND user_id=?",('watch:'+wid,user))

    def claim(self,kind):
        now=self.clock();token=secrets.token_hex(16)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('''SELECT * FROM background_jobs WHERE kind=? AND next_at<=?
                AND (state='queued' OR (state='running' AND lease_until<?)) ORDER BY next_at LIMIT 1''',(kind,now,now)).fetchone()
            if not row:return None
            conn.execute("UPDATE background_jobs SET state='running',lease_until=?,lease_token=?,updated=? WHERE id=?",(now+120,token,now,row['id']))
            return {**dict(row),'lease_token':token}

    async def run_job(self,job):
        try:
            if job['kind']=='dining_start':
                await self.service.dining.dispatch(job);return
            user=job['user_id'];lock=self.service.user_lock(user)
            last=self.db.one('SELECT MAX(m.created) AS created FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.user_id=?',(user,))['created'] or 0
            if lock.locked() or self.db.one("SELECT id FROM runs WHERE user_id=? AND status IN ('running','resuming')",(user,)) or self.clock()-last<config.MEMORY_IDLE_SECONDS:
                self.finish(job,60);return
            # Do not hold the foreground lock across model calls. Version/signature
            # checks guard the derived write; new user activity cancels this task.
            async with asyncio.timeout(90):
                await self.service.memory.retry_pending(user,'')
                await self.consolidator.consolidate(user)
            self.finish(job,config.MEMORY_CONSOLIDATION_INTERVAL)
        except asyncio.CancelledError:
            self.finish(job,1);raise
        except Exception as exc:
            self.finish(job,min(3600,30*2**min(job['failures'],6)),error=type(exc).__name__)

    def finish(self,job,delay,error=None):
        self.db.execute('''UPDATE background_jobs SET state='queued',next_at=?,lease_until=0,lease_token=NULL,
            failures=?,last_error=?,updated=? WHERE id=? AND state='running' AND lease_token=?''',
            (self.clock()+delay,job['failures']+1 if error else 0,error,self.clock(),job['id'],job['lease_token']))

    def seed_memory(self):
        for row in self.db.rows('''SELECT id FROM users WHERE EXISTS(SELECT 1 FROM facts WHERE user_id=users.id AND status='active')
            OR EXISTS(SELECT 1 FROM pending_memory_summaries WHERE user_id=users.id)'''):
            self.queue_memory(row['id'],immediate=False)

    async def loop(self):
        while True:
            try:
                if self.clock()-self.last_seed>=60:self.seed_memory();self.last_seed=self.clock()
                for kind,limit in [('dining_start',4),('memory',1)]:
                    if sum(k==kind for k,t in self.tasks.values())>=limit:continue
                    job=self.claim(kind)
                    if job:
                        task=asyncio.create_task(self.run_job(job));self.tasks[job['id']]=(kind,task)
                        task.add_done_callback(lambda t,key=job['id']:self.tasks.pop(key,None))
            except sqlite3.OperationalError:
                logging.getLogger(__name__).warning('Background database operation failed; retrying')
                await asyncio.sleep(1)
            await asyncio.sleep(.25)

    def start(self):
        if self.runner is None or self.runner.done():self.runner=asyncio.create_task(self.loop())

    def foreground(self,user):
        entry=self.tasks.get('memory:'+user)
        if entry:entry[1].cancel()

    async def stop(self):
        tasks=([self.runner] if self.runner else [])+[t for _,t in self.tasks.values()]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.tasks.clear();self.runner=None

    def snapshot(self,user):
        watches=self.db.rows('SELECT * FROM dining_watches WHERE user_id=? ORDER BY created DESC LIMIT 20',(user,))
        for row in watches:row['criteria']=json.loads(row['criteria'])
        notices=self.db.rows('SELECT * FROM notifications WHERE user_id=? AND read_at IS NULL ORDER BY id DESC LIMIT 50',(user,))
        chat_updates=self.db.rows('''SELECT m.id,m.session_id,m.role,m.content FROM availability_chat_messages a
            JOIN messages m ON m.id=a.message_id JOIN sessions s ON s.id=m.session_id
            JOIN availability_events e ON e.id=a.event_id
            WHERE s.user_id=? AND e.user_id=? ORDER BY m.id DESC LIMIT 50''',(user,user))
        return {'watches':watches,'notifications':notices,'chat_updates':chat_updates,
            'worker_running':bool(self.runner and not self.runner.done()),
            'profile':read_profile(self.db,user),'memory_job':self.db.one('SELECT state,next_at,last_error FROM background_jobs WHERE id=?',('memory:'+user,)),
            'memory_review':self.db.one('SELECT status,created FROM memory_reviews WHERE user_id=? ORDER BY id DESC LIMIT 1',(user,))}
