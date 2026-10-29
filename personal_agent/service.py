import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import aclosing

from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.types import Command

from .agent import text_content
from .memory import Memory, TEMPORARY, fact_context, safe_fact
from .sources import evidence_sources, vetted_text
from .clock import clock_context
from .approvals import approval_text
from .browser_recovery import input_text
from .browser_diagnostics import load_diagnostics,debug_text
from .browser_intent import starts_new_request


class Service:
    def __init__(self, db, engine):
        self.db, self.engine = db, engine
        self.memory = Memory(db, engine.model)
        self.tasks = {}
        self.graphs = {}
        self.user_locks = {}
        self.cancel_reasons = {}

    def recover(self):
        # Paused approvals survive; in-flight actions never auto-resume after restart.
        for run in self.db.rows("SELECT * FROM runs WHERE status IN ('running','resuming')"):
            self.db.execute("UPDATE runs SET status='interrupted',error=? WHERE id=?",
                            ("Application restarted. Inspect before retrying any browser task.", run['id']))
            self.db.message(run['user_id'], run['session_id'], 'assistant',
                            "This run was interrupted by an application restart. Its action outcome may be unknown; inspect the website before retrying.")
            self.db.event(run['id'], 'done', {"status": "interrupted"})

    def active(self, user):
        return self.db.one("SELECT * FROM runs WHERE user_id=? AND status IN ('running','approval','resuming','input')", (user,))

    def unverified_research(self, run, sources):
        usage = self.db.one('SELECT searches FROM run_usage WHERE run_id=?', (run,))
        return bool(usage and usage['searches'] and not sources)

    def browser_outcome(self,user,run,text):
        if self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='browser_use_tasks'"):
            task=self.db.one('SELECT status,result FROM browser_use_tasks WHERE user_id=? AND run_id=? ORDER BY created DESC LIMIT 1',(user,run))
            if task and task['result']:
                result=json.loads(task['result'])
                verification=result.get('verification',{})
                if result.get('status')=='rejected':return 'Action cancelled. Nothing further was submitted.'
                if result.get('status')!='verified':
                    attempts=result.get('recovery_attempts',[])
                    report=''
                    if attempts:report='\nRecovery attempts: '+', '.join(dict.fromkeys(a['operation']+' ('+a['phase']+')' for a in attempts))+'.'
                    details=load_diagnostics(self.db,user,run=run)
                    return ('Browser task '+result.get('status',task['status'])+': '+verification.get('reason',result.get('message','The result is not verified.'))+
                            report+'\nInspect the current website before repeating a submission.'+ ('\n\n'+debug_text(details) if details else ''))
                clean='\n'.join(line for line in text.splitlines() if not re.search(r'please confirm|would you like|shall i|before i|ready to|only after your approval',line,re.I))
                return 'Verified browser result: '+verification.get('reason','The requested result was observed.')+'\n\n'+clean
        return text

    def user_lock(self, user):
        return self.user_locks.setdefault(user, asyncio.Lock())

    async def new_session(self, user):
        async with self.user_lock(user):
            active = self.active(user)
            if active:
                await self._cancel(user, active['id'], 'Started a new chat.')
            return self.db.new_session(user)

    async def submit(self, user, session, text, pending_input_id=None):
        async with self.user_lock(user):
            return await self._submit(user, session, text, pending_input_id)

    async def _submit(self, user, session, text, pending_input_id=None):
        self.db.owned_session(user, session)
        active = self.active(user)
        if active:
            if text.strip() == '/stop':
                self.db.message(user, session, 'user', text)
                await self._cancel(user, active['id'])
                reply = "Stopped. Inspect the website before repeating any pending action."
                self.db.message(user, session, 'assistant', reply)
                return {"reply": reply}
            if active['status'] == 'approval':
                parts = text.strip().lower().split()
                if len(parts) == 2 and parts[0] in {'approve', 'reject'} and parts[1] == active['approval_id']:
                    if active['session_id'] != session:
                        raise ValueError("Open the conversation with the pending approval first")
                    return await self._resume(user, active['id'], parts[0], parts[1])
                if parts and parts[0] in {'approve','reject','cancel'} and len(parts) <= 2:
                    raise ValueError('Use the current approval buttons or its exact approval ID')
            # Only a reply explicitly bound to this pause can continue its graph.
            # A normal new prompt replaces the active run, including in another chat.
            if pending_input_id:
                if active['session_id'] != session or active['status'] != 'input' or active['approval_id'] != pending_input_id:
                    raise LookupError('Pending input not found')
                action = json.loads(active['approval'])['action']
                if text.casefold() in {'continue', 'done'} or not starts_new_request(text, action['kind']):
                    return await self._respond_input(user, active['id'], pending_input_id, text)
            await self._cancel(user, active['id'], 'Replaced by a new request.')
        elif pending_input_id:
            raise LookupError('Pending input not found')
        mid = self.db.message(user, session, 'user', text)
        title = self.db.owned_session(user, session)['title']
        if title == 'New chat':
            self.db.execute("UPDATE sessions SET title=? WHERE id=?", (text[:80], session))
        if text.startswith('/'):
            reply = self.command(user, session, mid, text)
            answer = self.db.message(user, session, 'assistant', reply)
            dependencies = self.db.dependencies(user, session)
            if text.split()[0] == '/memory':
                status = 'quarantined' if text.strip() == '/memory review' else 'active'
                dependencies += [f['id'] for f in self.db.rows('SELECT id FROM facts WHERE user_id=? AND status=?', (user, status))]
            self.db.record_dependencies(user, session, answer, dependencies)
            try:
                await self.memory.summarize(user, session)
            except Exception:
                pass  # All exact command exchanges remain available on failure.
            return {"reply": reply}
        run = secrets.token_hex(16)
        try:
            self.db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,?,?)",
                            (run, user, session, 'running', time.time()))
        except Exception:
            raise ValueError("A run is already active") from None
        self.tasks[run] = asyncio.create_task(self.execute(user, session, run))
        return {"run_id": run}

    def command(self, user, session, mid, text):
        parts = text.split(maxsplit=2)
        name = parts[0]
        if name == '/sessions':
            rows = self.db.rows("SELECT id,title FROM sessions WHERE user_id=? ORDER BY created DESC LIMIT 100", (user,))
            return '\n'.join(f"{r['id']} — {r['title']}" for r in rows) + '\nUse /open SESSION_ID to continue a conversation.'
        if name == '/files':
            files = self.db.rows('SELECT id,filename FROM artifacts WHERE user_id=? ORDER BY rowid DESC LIMIT 50',(user,))
            return '\n'.join(f"{f['id']}: {f['filename']}" for f in files) or 'No files yet. Drop a file onto the message composer to upload it.'
        if name == '/debug':
            details=load_diagnostics(self.db,user,session=session)
            return debug_text(details) if details else 'No browser exception details were captured in this chat. Earlier generic errors did not retain their underlying exceptions.'
        if name == '/memory':
            if len(parts) > 1 and parts[1] == 'review':
                facts = self.db.rows("SELECT id,text FROM facts WHERE user_id=? AND status='quarantined' ORDER BY updated DESC,id DESC", (user,))
                return ('Previously automatic memories, quarantined and excluded from chats:\n' +
                        '\n'.join(f"{f['id']}: {f['text']}" for f in facts) +
                        '\nUse /remember TEXT to explicitly save a lasting fact, or /forget ID to remove one.') if facts else 'No quarantined memories.'
            facts = self.db.facts(user, inspect=True)
            reply = '\n'.join(f"{f['id']}: {f['text']}" for f in facts) or "No permanent memory saved yet. Use /remember TEXT to save a lasting fact."
            if self.db.one("SELECT 1 FROM facts WHERE user_id=? AND status='quarantined'", (user,)):
                reply += '\nOlder automatic memories are quarantined. Use /memory review to inspect them.'
            return reply
        if name == '/remember' and len(parts) >= 2:
            value = text[len('/remember'):].strip()
            if len(value) > 600 or not safe_fact(value) or TEMPORARY.search(value):
                return "Save only lasting facts or preferences, up to 600 characters. Keep secrets and temporary tasks out of permanent memory."
            key = 'explicit.' + hashlib.sha256(value.casefold().encode()).hexdigest()[:24]
            fid, _ = self.db.put_fact(user, value, mid, key, evidence=value)
            return f"Saved permanent memory {fid}: {value}\nUse /correct {fid} NEW TEXT to replace it, or /forget {fid} to remove it."
        if name == '/forget' and len(parts) >= 2:
            self.db.forget(user, parts[1])
            return "Memory forgotten. Dependent messages and summaries are excluded from future context; unrelated session history is retained."
        if name == '/correct' and len(parts) == 3:
            if len(parts[2]) > 600 or not safe_fact(parts[2]) or TEMPORARY.search(parts[2]):
                return "That text cannot be stored as durable memory. Do not save secrets."
            old = self.db.one('SELECT * FROM facts WHERE id=? AND user_id=?', (parts[1], user))
            if not old:
                raise LookupError('Memory not found')
            with self.db.connect() as conn:
                if old['status'] == 'quarantined':
                    self.db._invalidate(conn, user, [old])
                    conn.execute("UPDATE facts SET status='superseded' WHERE id=?", (old['id'],))
                self.db.put_fact(user, parts[2], mid, old['memory_key'], evidence=parts[2], conn=conn)
            return "Memory corrected; stale summary context cleared."
        if name == '/stop':
            return "No active run."
        return "Commands: /sessions, /open SESSION_ID, /files, /debug, /remember TEXT, /memory (or /memory review), /forget ID (or all), /correct ID TEXT, /stop."

    async def settle_pause(self,user,run_id):
        row=self.db.one('SELECT status FROM runs WHERE id=? AND user_id=?',(run_id,user))
        if not row:raise LookupError('Run not found')
        if row['status'] not in {'approval','input'}:return
        previous=self.tasks.get(run_id)
        if previous and previous is not asyncio.current_task():
            await asyncio.shield(previous)

    async def resume(self, user, run_id, decision, approval_id):
        async with self.user_lock(user):
            return await self._resume(user, run_id, decision, approval_id)

    async def _resume(self, user, run_id, decision, approval_id):
        await self.settle_pause(user,run_id)
        if decision not in {'approve','reject'}:raise ValueError('Choose Approve or Cancel')
        run = self.db.one("SELECT * FROM runs WHERE id=? AND user_id=? AND status='approval'", (run_id, user))
        if not run or run['approval_id'] != approval_id:
            raise LookupError("Pending approval not found")
        # Atomic claim prevents repeated approval submissions.
        with self.db.connect() as db:
            changed = db.execute("UPDATE runs SET status='resuming' WHERE id=? AND status='approval'", (run_id,)).rowcount
            if changed != 1:
                raise ValueError("Approval already handled")
        cursor = self.db.one("SELECT COALESCE(MAX(id),0) AS cursor FROM events WHERE run_id=?", (run_id,))['cursor']
        self.db.message(user,run['session_id'],'user','Approve' if decision=='approve' else 'Cancel')
        self.tasks[run_id] = asyncio.create_task(self.execute(user, run['session_id'], run_id, decision))
        return {"run_id": run_id, "after": cursor}

    async def respond_input(self,user,run_id,input_id,text,completed_in_browser=False):
        async with self.user_lock(user):
            return await self._respond_input(user,run_id,input_id,text,completed_in_browser)

    async def _respond_input(self,user,run_id,input_id,text,completed_in_browser=False):
        await self.settle_pause(user,run_id)
        run=self.db.one("SELECT * FROM runs WHERE id=? AND user_id=? AND status='input'",(run_id,user))
        if not run or run['approval_id']!=input_id:raise LookupError('Pending input not found')
        pending=json.loads(run['approval'])
        action=pending['action']
        value=text.strip()
        if not value or len(value)>2000:raise ValueError('Enter a short response')
        if action['kind']=='manual' or completed_in_browser:
            if value.casefold() not in {'continue','done'}:raise ValueError('Enter passwords and verification codes in the browser, then choose Continue')
            # Check that private fields/challenges are gone before exposing the page to the model.
            host=self.engine.browsers
            if hasattr(host,'manual_ready') and not await host.manual_ready(user,run_id):
                raise ValueError('Complete the required website fields and any private login/verification step before continuing. The agent is still paused.')
            value='Completed manual website step.' if action['kind']=='manual' else 'Completed website information step.'
        elif re.search(r'password|verification.code|one.time.code|\botp\b',action['prompt'],re.I):
            raise ValueError('Private login information must be entered in the browser')
        with self.db.connect() as c:
            changed=c.execute("UPDATE runs SET status='resuming' WHERE id=? AND user_id=? AND status='input' AND approval_id=?",(run_id,user,input_id)).rowcount
            if changed!=1:raise ValueError('Input already handled')
        host=self.engine.browsers
        try:
            if hasattr(host,'release_control'):await host.release_control(user,run_id)
        except ValueError:
            self.db.execute("UPDATE runs SET status='input' WHERE id=? AND user_id=? AND status='resuming' AND approval_id=?",(run_id,user,input_id))
            raise
        cursor=self.db.one('SELECT COALESCE(MAX(id),0) n FROM events WHERE run_id=?',(run_id,))['n']
        self.db.message(user,run['session_id'],'user',value)
        self.tasks[run_id]=asyncio.create_task(self.execute(user,run['session_id'],run_id,{'type':'browser_input','text':value,'completed_in_browser':completed_in_browser}))
        return {'run_id':run_id,'after':cursor}

    async def execute(self, user, session, run, decision=None, max_calls=24, max_searches=3):
        buffer = ''
        pending = ''
        status = 'completed'
        dependencies = []
        try:
            context = self.memory.context(user, session, '')
            if sum(len(m['content']) for m in context['unsummarized_messages']) > 100000:
                await self.memory.summarize(user, session)
                context = self.memory.context(user, session, '')
            current = self.db.one("SELECT id,role,content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1", (session,))
            latest = current['content']
            context['current_user_message'] = current
            context['clock'] = clock_context()
            context['unsummarized_messages'] = [m for m in context['unsummarized_messages'] if m['id'] != current['id']]
            context['recent_messages'] = [m for m in context['recent_messages'] if m['id'] != current['id']]
            # Retrieve with the current question plus recent user topic, never
            # the older assistant's inferred subject.
            topic = ' '.join(m['content'] for m in (context['recent_messages'] + context['unsummarized_messages'])[-6:] if m['role'] == 'user')
            facts = self.db.facts(user, latest + ' ' + topic)
            context['long_term_facts'] = [fact_context(f) for f in facts]
            dependencies = sorted(set(context.pop('memory_dependencies') + [f['id'] for f in facts]))
            graph = self.graphs.get(run) or self.engine.graph(user, run, context, max_calls, max_searches)
            self.graphs[run] = graph
            cfg = {"configurable": {"thread_id": f"{user}:{session}:{run}"}, "recursion_limit": 110}
            # Context carries all uncovered messages, including the current one, exactly once.
            payload = Command(resume=decision) if decision else {"messages": [HumanMessage(content=(
                "Continue this session and answer current_user_message in the supplied context.\n" +
                json.dumps(context, ensure_ascii=False)))]}
            async with asyncio.timeout(240):
                async with aclosing(graph.astream(payload, cfg, stream_mode=['messages', 'updates'])) as stream:
                    async for mode, event in stream:
                        if mode == 'messages':
                            chunk, metadata = event
                            if isinstance(chunk, AIMessageChunk) and metadata.get('langgraph_node') == 'model':
                                text = text_content(chunk.content)
                                if text:
                                    buffer += text
                                    pending += text
                                    # Hold incomplete lines so a split URL cannot bypass citation checks.
                                    while '\n' in pending:
                                        line, pending = pending.split('\n', 1)
                                        sources = evidence_sources(self.db, run)
                                        if not self.unverified_research(run, sources):
                                            clean = vetted_text(line + '\n', sources, empty_fallback=False)
                                            if clean:
                                                self.db.event(run, 'token', clean)
                        elif mode == 'updates' and '__interrupt__' in event:
                            approval = event['__interrupt__'][0].value
                            approval_id = secrets.token_hex(4)
                            self.db.execute("UPDATE runs SET status='approval',approval=?,approval_id=? WHERE id=?",
                                            (json.dumps(approval), approval_id, run))
                            if buffer:
                                mid = self.db.message(user, session, 'assistant', vetted_text(buffer, evidence_sources(self.db, run)))
                                self.db.record_dependencies(user, session, mid, dependencies)
                                buffer = ''
                            waiting=approval.get('type')=='browser_input'
                            if waiting:self.db.execute("UPDATE runs SET status='input' WHERE id=?",(run,))
                            body = input_text(approval['action']) if waiting else approval_text(approval)
                            message_id=self.db.message(user, session, 'assistant', body)
                            self.db.event(run, 'input' if waiting else 'approval', {"text": body, "id": approval_id,"message_id":message_id})
                            status = 'input' if waiting else 'approval'
                            return
            if not buffer:
                state = await graph.aget_state(cfg)
                for message in reversed(state.values.get('messages', [])):
                    if message.type == 'ai' and not getattr(message, 'tool_calls', []):
                        buffer = text_content(message.content)
                        break
            sources = evidence_sources(self.db, run)
            buffer = self.browser_outcome(user,run,buffer)
            buffer = vetted_text(buffer, sources)
            if self.unverified_research(run, sources):
                buffer = "I couldn't verify information from the official website for this request. I won't use other websites as substitutes."
                self.db.event(run, 'token', buffer)
            elif pending:
                clean = vetted_text(pending, sources, empty_fallback=False)
                if clean:
                    self.db.event(run, 'token', clean)
            if sources:
                buffer += '\n\nSources:\n' + '\n'.join(f"[{s['title']}]({s['url']})" for s in sources)
            buffer = vetted_text(buffer, sources)
            buffer = buffer or "No response was produced. Please try a more specific request."
            mid = self.db.message(user, session, 'assistant', buffer)
            self.db.record_dependencies(user, session, mid, dependencies)
            self.db.event(run, 'answer', buffer)
            buffer = ''
            await self.digest(user, session, run)
        except asyncio.CancelledError:
            status = 'cancelled'
            buffer = vetted_text(buffer, evidence_sources(self.db, run))
            note = self.cancel_reasons.get(run)
            body = (buffer + '\n\n' if buffer else '') + (note + " Previous task stopped." if note else "Stopped. Any in-flight browser action may have an unknown outcome; inspect before retrying.")
            mid = self.db.message(user, session, 'assistant', body)
            self.db.record_dependencies(user, session, mid, dependencies)
            self.db.event(run, 'answer', body)
        except Exception as exc:
            status = 'failed'
            buffer = vetted_text(buffer, evidence_sources(self.db, run))
            body = (buffer + '\n\n' if buffer else '') + f"The run stopped ({type(exc).__name__}). Your conversation is saved. Inspect the browser before repeating an action."
            mid = self.db.message(user, session, 'assistant', body)
            self.db.record_dependencies(user, session, mid, dependencies)
            self.db.event(run, 'answer', body)
            # Record only exception class; SDK messages can contain sensitive request data.
            self.db.execute("UPDATE runs SET error=? WHERE id=?", (type(exc).__name__, run))
        finally:
            if status not in {'approval','input'}:
                self.db.execute("UPDATE runs SET status=?,approval=NULL,approval_id=NULL WHERE id=?", (status, run))
                self.db.event(run, 'done', {"status": status})
                self.graphs.pop(run, None)
            if self.tasks.get(run) is asyncio.current_task():self.tasks.pop(run, None)

    async def digest(self, user, session, run):
        self.db.event(run, 'progress', 'Saving session summary and explicitly requested memory…')
        try:
            async with asyncio.timeout(60):
                await self.memory.summarize(user, session)
        except Exception:
            self.db.event(run, 'progress', 'Summary update is pending. Unsummarized messages remain available for the next turn.')

    async def cancel(self, user, run_id):
        async with self.user_lock(user):
            await self._cancel(user, run_id)

    async def _cancel(self, user, run_id, reason=None):
        run = self.db.one("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user))
        if not run:
            raise LookupError("Run not found")
        if run['status'] not in {'running','resuming','approval','input'}:
            return
        if reason:
            self.cancel_reasons[run_id] = reason
        task = self.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        noted = self.db.one('SELECT status FROM runs WHERE id=?', (run_id,))['status'] == 'cancelled'
        self.cancel_reasons.pop(run_id, None)
        # Cancellation before execute starts (or just after an interrupt) has no
        # coroutine finalizer. Always revoke the pause and retire the database row.
        self.db.execute("UPDATE runs SET status='cancelled',approval=NULL,approval_id=NULL WHERE id=? AND user_id=?", (run_id,user))
        self.graphs.pop(run_id, None)
        self.tasks.pop(run_id, None)
        browser_tasks = getattr(self.engine, 'browser_tasks', None)
        if browser_tasks:
            await browser_tasks.cancel_run(user, run_id)
        if not noted and (reason or run['status'] in {'approval','input'}):
            body = (reason + ' Previous task stopped.' if reason else 'Browser task cancelled. No further browser actions will be taken.')
            self.db.message(user, run['session_id'], 'assistant', body)
            self.db.event(run_id, 'answer', body)
        self.db.event(run_id, 'done', {"status": "cancelled"})

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
