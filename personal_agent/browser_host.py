"""Per-user Chromium hosting and network policy; Browser Use executes all actions."""
import asyncio
import ipaddress
from pathlib import Path
import re
import secrets
import socket
import time
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, async_playwright

from . import config
from .sources import allowed_url,normalize_domains
from .browser_recovery import PAGE_OBSERVER, combine_observations
from .browser_dom import FOCUS,InspectionUnavailable,inspect_dom


def redact(value):
    return re.sub(r'sk-[\w-]+|\b(?:\d[ -]?){12,19}\b','[private]',str(value))[:1000]


def audit_url(url):
    p=urlsplit(url)
    return p._replace(query='',fragment='').geturl()


async def public_url(url):
    p=urlsplit(url)
    if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password:
        raise ValueError('Only public HTTP/HTTPS websites are allowed')
    if p.port not in {None,80,443}:raise ValueError('Only standard web ports are allowed')
    addresses=await asyncio.get_running_loop().getaddrinfo(p.hostname,p.port or 443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Local/private network destinations are unavailable')


class BrowserHost:
    def __init__(self,db,headless=None):
        self.db,self.headless=db,config.BROWSER_HEADLESS if headless is None else headless
        self.playwright=None
        self.contexts={};self.locks={};self.run_for_user={};self.scopes={};self.dialogs={};self.blocked_destinations={};self.document_status={}
        self.downloads=set()
        self.views={};self.preview_locks={};self.page_targets={}
        self.controls={};self.control_locks={}
        self.db.execute('CREATE TABLE IF NOT EXISTS browser_delegations (user_id TEXT NOT NULL,run_id TEXT NOT NULL,domain TEXT NOT NULL,PRIMARY KEY(user_id,run_id,domain))')

    def set_scope(self,user,run,domains):
        if not self.db.one('SELECT id FROM runs WHERE id=? AND user_id=?',(run,user)):
            raise LookupError('Browser run not found')
        domains=normalize_domains(domains)
        previous=self.scopes.get((user,run))
        if previous and not set(domains).issubset(previous):
            raise ValueError('A new provider domain requires an observed transition and explicit approval')
        delegated=[r['domain'] for r in self.db.rows('SELECT domain FROM browser_delegations WHERE user_id=? AND run_id=?',(user,run))]
        self.scopes[(user,run)]=list(dict.fromkeys((previous or domains)+delegated))

    async def context(self,user):
        if not self.db.one('SELECT id FROM users WHERE id=?',(user,)):raise LookupError('Browser user not found')
        existing=self.contexts.get(user)
        if existing:
            try:
                _=existing.pages
                return existing
            except PlaywrightError:self.contexts.pop(user,None)
        if self.playwright is None:self.playwright=await async_playwright().start()
        root=(config.RUNTIME/'browser-profiles').resolve();path=(root/user).resolve()
        if not path.is_relative_to(root):raise ValueError('Invalid browser profile')
        path.mkdir(parents=True,exist_ok=True,mode=0o700)
        context=await self.playwright.chromium.launch_persistent_context(str(path),headless=self.headless,
            accept_downloads=True,service_workers='block',chromium_sandbox=True,viewport={'width':1280,'height':850},
            args=['--remote-debugging-address=127.0.0.1','--remote-debugging-port=0'])
        async def route(intercept):
            request=intercept.request;run=self.run_for_user.get(user);scope=self.scopes.get((user,run))
            try:
                await public_url(request.url)
                if scope and request.resource_type=='document' and not allowed_url(request.url,scope):
                    self.blocked_destinations.setdefault((user,run),set()).add(request.url)
                    raise ValueError('Outside approved domain scope')
            except (ValueError,OSError):
                await intercept.abort();return
            await intercept.continue_()
        await context.route('**/*',route)
        def response(item):
            if item.request.resource_type=='document':self.document_status[(user,item.url)]=item.status
        context.on('response',response)
        def watch(page):
            page.on('close',lambda:self.page_targets.pop(page,None))
            def download(item):
                task=asyncio.create_task(self.download(user,item));self.downloads.add(task);task.add_done_callback(self.downloads.discard)
            page.on('download',download)
            page.on('dialog',lambda dialog:self.dialogs.__setitem__(page,dialog))
        context.on('page',watch)
        context.on('close',lambda:self.contexts.pop(user,None) if self.contexts.get(user)==context else None)
        for page in context.pages:watch(page)
        self.contexts[user]=context
        return context

    async def cdp_endpoint(self,user):
        await self.context(user)
        port=int((config.RUNTIME/'browser-profiles'/user/'DevToolsActivePort').read_text().splitlines()[0])
        return f'http://127.0.0.1:{port}'

    def set_view(self,user,run,observed=None):
        if not self.db.one('SELECT id FROM runs WHERE id=? AND user_id=?',(run,user)):raise LookupError('Browser run not found')
        view=self.views.setdefault((user,run),{'live':True,'url':'','title':'','frame':None,'captured':0})
        view['live']=True
        if observed:
            for key in ['url','title','target_id']:
                if key in observed:view[key]=observed[key]
        older=[key for key in self.views if key[0]==user and key!=(user,run)]
        for key in older[:-2]:self.views.pop(key,None)

    def view_info(self,user,run):
        view=self.views.get((user,run))
        if not view:return {'available':False}
        live=view['live'] and self.run_for_user.get(user)==run and user in self.contexts
        dialog=next((d for p,d in self.dialogs.items() if self.contexts.get(user) and p in self.contexts[user].pages),None)
        return {'available':bool(live or view['frame']),'live':bool(live),'url':view['url'],'title':view['title'],'headless':self.headless,
                'dialog':{'type':dialog.type,'message':redact(dialog.message)} if dialog else None}

    async def preview(self,user,run):
        if not self.db.one('SELECT id FROM runs WHERE id=? AND user_id=?',(run,user)):raise LookupError('Browser run not found')
        view=self.views.get((user,run))
        if not view:return None
        async with self.preview_locks.setdefault(user,asyncio.Lock()):
            live=view['live'] and self.run_for_user.get(user)==run
            if live and (time.monotonic()-view['captured']>=1 or not view['frame']):
                context=self.contexts.get(user)
                pages=[p for p in context.pages if not p.is_closed()] if context else []
                page=None
                for candidate in pages:
                    if view.get('target_id'):
                        target=self.page_targets.get(candidate)
                        if not target:
                            session=None
                            try:
                                session=await context.new_cdp_session(candidate)
                                target=(await session.send('Target.getTargetInfo'))['targetInfo']['targetId']
                            except PlaywrightError:continue
                            finally:
                                if session:
                                    try:await session.detach()
                                    except PlaywrightError:pass
                            self.page_targets[candidate]=target
                        if target==view['target_id']:page=candidate;break
                    elif candidate.url==view.get('url'):page=candidate;break
                if page is None and not view.get('target_id') and pages:page=pages[-1]
                if page:
                    try:
                        view['frame']=await page.screenshot(type='jpeg',quality=60,timeout=1500,caret='hide')
                        view['captured']=time.monotonic()
                    except PlaywrightError:pass  # Retain the last frame during navigation/dialogs.
            return view['frame']

    async def view_page(self,user,run):
        if self.run_for_user.get(user)!=run:raise ValueError('This browser belongs to another run')
        view=self.views.get((user,run),{})
        context=self.contexts.get(user)
        if not context:raise ValueError('The browser is unavailable')
        pages=[p for p in context.pages if not p.is_closed()]
        for page in pages:
            if view.get('target_id'):
                target=self.page_targets.get(page)
                if not target:
                    client=await context.new_cdp_session(page)
                    try:target=(await client.send('Target.getTargetInfo'))['targetInfo']['targetId']
                    finally:await client.detach()
                    self.page_targets[page]=target
                if target==view['target_id']:return page
            elif page.url==view.get('url'):return page
        raise ValueError('The active browser tab changed. Reopen the manual step.')

    def pending_manual(self,user,run,input_id=None):
        import json
        row=self.db.one("SELECT * FROM runs WHERE id=? AND user_id=? AND status='input'",(run,user))
        if not row:raise LookupError('Pending manual step not found')
        if input_id is not None and row['approval_id']!=input_id:raise LookupError('Pending manual step not found')
        action=json.loads(row['approval'])['action']
        if action.get('kind') not in {'manual','information'}:raise ValueError('This task is not waiting for website input')
        return action

    async def open_takeover(self,user,run,input_id):
        import json
        action=self.pending_manual(user,run,input_id)
        task=self.db.one('SELECT * FROM browser_use_tasks WHERE id=? AND user_id=? AND run_id=?',(action['task_id'],user,run))
        if not task:raise LookupError('Browser task not found')
        self.set_scope(user,run,json.loads(task['domains']))
        self.run_for_user[user]=run
        context=await self.context(user)
        view=self.views.get((user,run))
        if view:
            try:await self.view_page(user,run)
            except (ValueError,PlaywrightError):view=None;self.views.pop((user,run),None)
        if not view:
            row=self.db.one('SELECT before_state FROM browser_use_actions WHERE id=? AND task_id=?',(action['action_id'],task['id']))
            observed=json.loads(row['before_state']) if row else {}
            page=context.pages[-1] if context.pages else await context.new_page()
            url=observed.get('url','')
            # Restore only a previously observed public page with a GET; do not replay clicks or forms.
            if page.url=='about:blank' and url:
                await public_url(url)
                if not allowed_url(url,self.scopes[(user,run)]):raise ValueError('Saved page is outside the website scope')
                from .browser_policy import RISK,action_url
                destination=url
                if not RISK.search(action_url(destination)):await page.goto(destination,wait_until='domcontentloaded',timeout=15000)
            client=await context.new_cdp_session(page)
            try:target=(await client.send('Target.getTargetInfo'))['targetInfo']['targetId']
            finally:await client.detach()
            self.set_view(user,run,{'url':page.url,'title':await page.title(),'target_id':target})
        return {'ok':True}

    async def snapshot(self,user,run):
        page=await self.view_page(user,run)
        state=await inspect_dom(page,self.scopes.get((user,run),[]),PAGE_OBSERVER)
        root=state[0]['value'];frames=[]
        for record in state[1:]:
            if not record.get('value'):continue
            value=record['value'];value['focused']=record['active'];frames.append(value)
        return combine_observations(root,frames)

    async def manual_ready(self,user,run):
        action=self.pending_manual(user,run)
        try:
            page=await self.view_page(user,run)
            if page in self.dialogs:return False
            state=await self.snapshot(user,run)
            if state.get('manual_required'):return False
            if any((field.get('required') and field.get('empty')) or not field.get('valid',True) for field in state.get('fields',[])):return False
            if action['kind']=='information':
                from .browser_recovery import input_need
                if input_need(state):return False
            return True
        except PlaywrightError:raise InspectionUnavailable() from None

    async def manual_action(self,user,run,input_id,operation,x=None,y=None,text=None,key=None):
        # This channel is available only during an owned user-input pause. It never
        # emits model events, audit payloads, logs, or conversation messages.
        self.pending_manual(user,run,input_id)
        async with self.preview_locks.setdefault(user,asyncio.Lock()):
            self.pending_manual(user,run,input_id)  # A queued input may have been cancelled/resumed.
            page=await self.view_page(user,run)
            scope=self.scopes.get((user,run),[])
            dialog=self.dialogs.get(page)
            if dialog:
                if operation=='type' and dialog.type=='prompt':await dialog.accept(text or '')
                elif operation=='key' and key=='Enter':await dialog.accept()
                elif operation=='key' and key=='Escape':await dialog.dismiss()
                else:raise ValueError('A website dialog is open. Press Enter to accept or Escape to dismiss in the browser view.')
                self.dialogs.pop(page,None)
                return {'ok':True}
            if not allowed_url(page.url,scope):raise ValueError('A website transition requires domain approval')
            state=await inspect_dom(page,scope,FOCUS)
            for frame in state[1:]:
                box=frame['box']
                relevant=bool(box and x is not None and y is not None and box['x']<=x<box['x']+box['width'] and box['y']<=y<box['y']+box['height']) if operation in {'click','pointerdown','pointerup'} else frame['active']
                if relevant and not frame['allowed']:raise ValueError('A website transition requires domain approval')
            if operation in {'click','pointerdown','pointerup'}:
                viewport=page.viewport_size or {'width':1280,'height':850}
                if x is None or y is None or not 0<=x<viewport['width'] or not 0<=y<viewport['height']:raise ValueError('Click within the browser viewport')
                if operation=='click':await page.mouse.click(x,y)
                else:
                    await page.mouse.move(x,y)
                    if operation=='pointerdown':await page.mouse.down()
                    else:await page.mouse.up()
            elif operation=='type':
                if not text or len(text)>2000:raise ValueError('Enter at most 2000 characters')
                # Require a real editable focus, including cross-origin frames.
                focused=any(f['active'] and f.get('value',{}).get('editable') for f in state)
                if not focused:raise ValueError('Click an editable website field first')
                # Preserve native key events for direct character typing;
                # paste/IME strings use text insertion without a shared OS clipboard.
                if len(text)==1 and text.isascii():await page.keyboard.type(text)
                else:await page.keyboard.insert_text(text)
            elif operation=='key':
                if key not in {'Enter','Tab','Shift+Tab','Backspace','Delete','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','ControlOrMeta+A'}:raise ValueError('Unsupported manual key')
                await page.keyboard.press(key)
            elif operation=='scroll':await page.mouse.wheel(max(-800,min(800,x or 0)),max(-800,min(800,y or 0)))
            else:raise ValueError('Unknown manual operation')
            view=self.views[(user,run)];view['captured']=0;view['url']=page.url
        return {'ok':True}

    async def input_focus(self,user,run,input_id):
        """Keyboard hints only: never return field contents or credentials."""
        self.pending_manual(user,run,input_id)
        page=await self.view_page(user,run)
        scope=self.scopes.get((user,run),[])
        for frame in await inspect_dom(page,scope,FOCUS):
            if frame['active'] and frame.get('value',{}).get('editable'):return frame['value']
        return {'editable':False,'multiline':False,'input_mode':'text'}

    async def freeze_view(self,user,run):
        view=self.views.get((user,run))
        if view:
            try:await self.preview(user,run)
            except PlaywrightError:pass
            view['live']=False

    async def download(self,user,download):
        run=self.run_for_user.get(user)
        if not run:
            await download.cancel();return
        aid=secrets.token_hex(16);path=config.RUNTIME/'artifacts'/user/aid
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        try:
            await download.save_as(path)
            if path.stat().st_size>50_000_000:
                path.unlink();self.db.event(run,'progress','Download exceeded 50 MB.');return
            self.db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?)',(aid,user,run,str(path),download.suggested_filename,'application/octet-stream'))
            self.db.event(run,'artifact',{'url':'/api/artifacts/'+aid,'name':download.suggested_filename})
        except Exception as exc:
            self.db.event(run,'progress','Download did not complete ('+type(exc).__name__+').')

    async def release_control(self,user,run):
        # Finish viewport restoration before the agent captures fresh DOM indices.
        async with self.control_locks.setdefault(user,asyncio.Lock()):
            control=self.controls.get(user)
            if control and control.run==run:
                control.stop.set()
                try:await asyncio.wait_for(control.closed.wait(),7)
                except TimeoutError:raise ValueError('Browser control is still closing. Refresh the page before continuing.') from None
                if control.restore_failed:raise ValueError('The browser could not restore its view. Refresh before continuing.')

    async def close_user(self,user):
        if control:=self.controls.get(user):control.stop.set()
        for key in [k for k in self.views if k[0]==user]:self.views.pop(key,None)
        context=self.contexts.pop(user,None)
        if context:
            for page in context.pages:
                self.page_targets.pop(page,None)
                dialog=self.dialogs.pop(page,None)
                if dialog:
                    try:await dialog.dismiss()
                    except PlaywrightError:pass
            await context.close()

    async def close(self):
        for user in list(self.contexts):await self.close_user(user)
        for task in list(self.downloads):task.cancel()
        await asyncio.gather(*self.downloads,return_exceptions=True)
        if self.playwright:await self.playwright.stop()
