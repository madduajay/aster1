"""Per-user Chromium hosting and network policy; Browser Use executes all actions."""
import asyncio
import ipaddress
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import json
import base64
import time
from collections import deque
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, async_playwright

from . import config
from .sources import allowed_url,normalize_domains
from .browser_recovery import PAGE_OBSERVER, combine_observations
from .browser_dom import FOCUS,InspectionUnavailable,inspect_dom
from .browser_policy import browser_policy
from .chrome_runtime import launch_visible_chrome


LOGIN_DIAGNOSTICS=r"""(()=>{
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
return {
fields:Array.from(document.querySelectorAll('input,textarea,select')).filter(visible).slice(0,30).map(e=>({type:e.type||e.tagName.toLowerCase(),filled:!!e.value,disabled:!!e.disabled,readonly:!!e.readOnly,required:!!e.required,valid:!e.validity||e.validity.valid,value_missing:!!e.validity?.valueMissing,pattern_mismatch:!!e.validity?.patternMismatch})),
buttons:Array.from(document.querySelectorAll('button,input[type=submit],[role=button]')).filter(visible).slice(0,40).map(e=>({type:e.type||'',login:/log\s*in|sign\s*in/i.test(e.innerText||e.getAttribute('aria-label')||e.value||''),disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',in_form:!!e.form})),
forms:Array.from(document.forms).slice(0,10).map(e=>({method:e.method,host:new URL(e.action||location.href,location.href).hostname}))
};})()"""


def network_error_code(failure):
    match=re.search(r'net::ERR_[A-Z0-9_]+',failure or '')
    return match[0] if match else 'request_failed'


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


async def page_viewport(page,client=None):
    """Read CSS viewport dimensions without injecting JS or resizing Chrome."""
    if page.viewport_size:return dict(page.viewport_size)
    own=client is None
    if own:client=await page.context.new_cdp_session(page)
    try:
        viewport=(await client.send('Page.getLayoutMetrics'))['cssLayoutViewport']
        # Chrome can report 733.999999999 during fractional scrolling. Truncating
        # would manufacture a resize and reject input on an unchanged window.
        return {'width':max(1,round(viewport['clientWidth'])),'height':max(1,round(viewport['clientHeight']))}
    finally:
        if own:await client.detach()


class BrowserHost:
    def __init__(self,db,headless=None):
        self.db,self.headless=db,config.BROWSER_HEADLESS if headless is None else headless
        self.playwright=None
        self.contexts={};self.locks={};self.run_for_user={};self.scopes={};self.dialogs={};self.blocked_destinations={};self.document_status={}
        self.downloads=set()
        self.views={};self.preview_locks={};self.page_targets={}
        self.controls={};self.control_locks={}
        self.network_events={}
        self.context_locks={}
        self.native_browsers={};self.attached_browsers={}
        self.initial_urls={};self.native_entries={}
        self.db.execute('CREATE TABLE IF NOT EXISTS browser_delegations (user_id TEXT NOT NULL,run_id TEXT NOT NULL,domain TEXT NOT NULL,PRIMARY KEY(user_id,run_id,domain))')
        self.db.execute('CREATE TABLE IF NOT EXISTS browser_preferences (user_id TEXT PRIMARY KEY REFERENCES users(id),mode TEXT NOT NULL CHECK(mode IN (\'headless\',\'visible\')))')

    def mode_info(self,user):
        row=self.db.one('SELECT mode FROM browser_preferences WHERE user_id=?',(user,))
        mode=row['mode'] if row else ('headless' if self.headless else 'visible')
        return {'mode':mode,'running':user in self.contexts}

    def profile_path(self,user):
        # Chrome and bundled Chromium can have different major versions. Keep
        # their on-disk profiles separate to avoid an unsupported downgrade.
        name='chrome-profiles' if self.mode_info(user)['mode']=='visible' else 'browser-profiles'
        root=(config.RUNTIME/name).resolve();path=(root/user).resolve()
        if not path.is_relative_to(root):raise ValueError('Invalid browser profile')
        return path

    async def set_mode(self,user,mode):
        if mode not in {'headless','visible'}:raise ValueError('Unknown browser mode')
        if not self.db.one('SELECT id FROM users WHERE id=?',(user,)):raise LookupError('Browser user not found')
        current=self.mode_info(user)
        if current['mode']==mode:return current
        # The API holds the same user lock as task submission. Never close a
        # browser under an agent or manual-control session.
        if self.db.one("SELECT id FROM runs WHERE user_id=? AND status IN ('running','resuming','input','approval')",(user,)):
            raise ValueError('Finish or cancel your active task before switching browser mode.')
        await self.close_user(user)
        self.db.execute('INSERT INTO browser_preferences(user_id,mode) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode',(user,mode))
        return self.mode_info(user)

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
        async with self.context_locks.setdefault(user,asyncio.Lock()):
            return await self._context(user)

    async def prepare_native_entry(self,user,run,url):
        if not url or self.mode_info(user)['mode']!='visible' or user in self.contexts:return False
        if not self.db.one('SELECT id FROM runs WHERE id=? AND user_id=?',(run,user)):raise LookupError('Browser run not found')
        await public_url(url)
        p=urlsplit(url)
        if p.query or p.fragment or not allowed_url(url,self.scopes.get((user,run),[])):return False
        self.initial_urls[(user,run)]=url
        return True

    async def _context(self,user):
        if not self.db.one('SELECT id FROM users WHERE id=?',(user,)):raise LookupError('Browser user not found')
        existing=self.contexts.get(user)
        if existing:
            try:
                _=existing.pages
                return existing
            except PlaywrightError:self.contexts.pop(user,None)
        if self.playwright is None:self.playwright=await async_playwright().start()
        path=self.profile_path(user)
        path.mkdir(parents=True,exist_ok=True,mode=0o700)
        visible=self.mode_info(user)['mode']=='visible'
        if visible:
            run=self.run_for_user.get(user);entry=self.initial_urls.pop((user,run),None)
            native=self.native_browsers.get(user)
            created=not native or native.process.returncode is not None
            if created:
                self.native_entries.pop(user,None)
                if native:await native.close(self.attached_browsers.get(user))
                native=await launch_visible_chrome(path,config.BROWSER_DISABLE_HTTP2,**({'initial_url':entry} if entry else {}))
                if entry:self.native_entries[user]={'run':run,'url':entry}
            browser=None
            try:
                browser=await self.playwright.chromium.connect_over_cdp(native.endpoint)
                context=browser.contexts[0]
                if not context.pages:
                    # Some native Chrome starts expose the debugging endpoint
                    # before creating a window, or start with no initial tab.
                    await asyncio.sleep(.1)
                    if not context.pages:await context.new_page()
            except BaseException:
                if created:await native.close(browser)
                raise
            self.native_browsers[user]=native;self.attached_browsers[user]=browser
        else:
            context=await self.playwright.chromium.launch_persistent_context(str(path),headless=True,channel=config.BROWSER_CHANNEL or None,
                viewport={'width':1280,'height':850},device_scale_factor=1,
                accept_downloads=True,service_workers='block',chromium_sandbox=True,
                args=['--remote-debugging-address=127.0.0.1','--remote-debugging-port=0']+(['--disable-http2'] if config.BROWSER_DISABLE_HTTP2 else []))
        # Native networking in both modes. Keep passive request diagnostics,
        # without registering Fetch interception or rewriting website traffic.
        run=self.run_for_user.get(user)
        if run:
            self.db.event(run,'browser_network_configuration',{'interception':False,'persistent':True,'mode':'visible' if visible else 'headless'})
        def response(item):
            if item.request.resource_type=='document':self.document_status[(user,item.url)]=item.status
            self.record_network(user,item.request,'response',status=item.status)
        context.on('response',response)
        context.on('request',lambda request:self.record_network(user,request,'request'))
        context.on('requestfailed',lambda request:self.record_network(user,request,'failed',error=network_error_code(request.failure)))
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

    def record_network(self,user,request,kind,**details):
        if request.resource_type not in {'document','xhr','fetch'}:return
        # No path/query, headers, body, response text, or credential values.
        event={
            'run':self.run_for_user.get(user),'time':time.time(),'kind':kind,
            'host':urlsplit(request.url).hostname,'method':request.method,**details}
        self.network_events.setdefault(user,deque(maxlen=40)).append(event)
        if event['run'] and hasattr(self.db,'event'):
            try:
                self.db.event(event['run'],'browser_network',{k:v for k,v in event.items() if k!='run'})
                self.db.execute("DELETE FROM events WHERE run_id=? AND kind='browser_network' AND id NOT IN (SELECT id FROM events WHERE run_id=? AND kind='browser_network' ORDER BY id DESC LIMIT 100)",(event['run'],event['run']))
            except sqlite3.Error:pass # Diagnostics must not interrupt website requests.

    async def login_diagnostics(self,user,run,input_id):
        self.pending_manual(user,run,input_id)
        page=await self.view_page(user,run)
        frames=await inspect_dom(page,self.scopes.get((user,run),[]),LOGIN_DIAGNOSTICS)
        return {'headless':self.mode_info(user)['mode']=='headless','frames':[{'host':urlsplit(f['url']).hostname,**f['value']} for f in frames if f.get('value')],
                'network':[{k:v for k,v in e.items() if k!='run'} for e in self.network_events.get(user,[]) if e['run']==run]}

    async def follow_public_redirect(self,user,run,request,scope):
        redirect=request.redirected_from
        if not (browser_policy().follow_providers and request.method=='GET' and redirect and allowed_url(redirect.url,scope) and request.frame.parent_frame is None):return False
        await public_url(request.url)
        domain=urlsplit(request.url).hostname
        domains=list(dict.fromkeys(scope+[domain]));self.scopes[(user,run)]=domains
        self.db.execute('INSERT OR IGNORE INTO browser_delegations VALUES(?,?,?)',(user,run,domain))
        if self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='browser_use_tasks'"):
            self.db.execute("UPDATE browser_use_tasks SET action_domains=? WHERE user_id=? AND run_id=? AND status IN ('running','input','approval')",(json.dumps(domains),user,run))
        return True

    def embedded_dependency(self,request,scope):
        # The route has already checked public_url. Let an authorized page load
        # its public embedded dependencies without granting agent access to
        # their DOM or adding their hosts to the action scope.
        if not browser_policy().follow_providers:return False
        try:
            frame=request.frame
            if frame.parent_frame is None:return False
            while frame.parent_frame is not None:frame=frame.parent_frame
            return allowed_url(frame.url,scope)
        except PlaywrightError:return False

    async def cdp_endpoint(self,user):
        await self.context(user)
        port=int((self.profile_path(user)/'DevToolsActivePort').read_text().splitlines()[0])
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

    def retire_view(self,user,run):
        if not self.db.one('SELECT id FROM runs WHERE id=? AND user_id=?',(run,user)):raise LookupError('Browser run not found')
        self.views.pop((user,run),None)
        self.scopes.pop((user,run),None)
        self.blocked_destinations.pop((user,run),None)
        if self.run_for_user.get(user)==run:self.run_for_user.pop(user,None)

    def view_info(self,user,run):
        view=self.views.get((user,run))
        if not view:return {'available':False}
        live=view['live'] and self.run_for_user.get(user)==run and user in self.contexts
        dialog=next((d for p,d in self.dialogs.items() if self.contexts.get(user) and p in self.contexts[user].pages),None)
        return {'available':bool(live or view['frame']),'live':bool(live),'url':view['url'],'title':view['title'],'headless':self.mode_info(user)['mode']=='headless','target_id':view.get('target_id',''),
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
                        if self.mode_info(user)['mode']=='visible':
                            client=await context.new_cdp_session(page)
                            try:
                                async with asyncio.timeout(1.5):
                                    capture=await client.send('Page.captureScreenshot',{'format':'jpeg','quality':60,'fromSurface':True,'captureBeyondViewport':False})
                                view['frame']=base64.b64decode(capture['data'],validate=True)
                            finally:await client.detach()
                        else:
                            view['frame']=await page.screenshot(type='jpeg',quality=60,timeout=1500,caret='hide')
                        view['captured']=time.monotonic()
                    except (PlaywrightError,TimeoutError):pass  # Retain the last frame during navigation/dialogs.
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
            row=self.db.one('SELECT before_state FROM browser_use_actions WHERE id=? AND task_id=?',(action.get('action_id'),task['id']))
            observed=json.loads(row['before_state']) if row else {}
            url=observed.get('url','') or (action.get('url','') if action.get('purpose')=='direct_control' else '')
            page=next((p for p in context.pages if url and p.url==url),None)
            if page is None:
                page=await context.new_page() if url else (context.pages[-1] if context.pages else await context.new_page())
            # Restore only a previously observed public page with a GET; do not replay clicks or forms.
            if page.url=='about:blank' and url:
                await public_url(url)
                if not allowed_url(url,self.scopes[(user,run)]):raise ValueError('Saved page is outside the website scope')
                from .browser_policy import URL_MUTATION,action_url
                destination=url
                if not URL_MUTATION.search(action_url(destination)):await page.goto(destination,wait_until='domcontentloaded',timeout=15000)
            client=await context.new_cdp_session(page)
            try:target=(await client.send('Target.getTargetInfo'))['targetInfo']['targetId']
            finally:await client.detach()
            self.set_view(user,run,{'url':page.url,'title':await page.title(),'target_id':target})
        self.views[(user,run)]['live']=True
        self.views[(user,run)]['captured']=0
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
            if action.get('purpose')=='page_opened':
                from .browser_intent import navigation_only_run
                if navigation_only_run(self.db,user,run) and not state.get('human_required'):return True
            if any((field.get('required') and field.get('empty')) or not field.get('valid',True) for field in state.get('fields',[])):return False
            if action['kind']=='information':
                from .browser_recovery import input_need
                if input_need(state):return False
            return True
        except PlaywrightError:raise InspectionUnavailable() from None

    async def manual_action(self,user,run,input_id,operation,x=None,y=None,text=None,key=None,pointer_x=None,pointer_y=None):
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
            target_x,target_y=(pointer_x,pointer_y) if operation=='scroll' else (x,y)
            for frame in state[1:]:
                box=frame['box']
                positioned=operation in {'click','pointerdown','pointerup'} or operation=='scroll' and target_x is not None and target_y is not None
                relevant=bool(box and target_x is not None and target_y is not None and box['x']<=target_x<box['x']+box['width'] and box['y']<=target_y<box['y']+box['height']) if positioned else frame['active']
                if relevant and not frame['allowed']:raise ValueError('A website transition requires domain approval')
            if operation in {'click','pointerdown','pointerup'}:
                viewport=await page_viewport(page)
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
            elif operation=='scroll':
                if pointer_x is not None and pointer_y is not None:
                    viewport=await page_viewport(page)
                    if not 0<=pointer_x<viewport['width'] or not 0<=pointer_y<viewport['height']:raise ValueError('Scroll within the browser viewport')
                    await page.mouse.move(pointer_x,pointer_y)
                await page.mouse.wheel(max(-800,min(800,x or 0)),max(-800,min(800,y or 0)))
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
        self.native_entries.pop(user,None)
        for key in [key for key in self.initial_urls if key[0]==user]:self.initial_urls.pop(key,None)
        self.network_events.pop(user,None)
        if control:=self.controls.get(user):control.stop.set()
        for key in [k for k in self.views if k[0]==user]:self.views.pop(key,None)
        context=self.contexts.pop(user,None)
        native=self.native_browsers.pop(user,None);browser=self.attached_browsers.pop(user,None)
        if context:
            for page in context.pages:
                self.page_targets.pop(page,None)
                dialog=self.dialogs.pop(page,None)
                if dialog:
                    try:await dialog.dismiss()
                    except PlaywrightError:pass
        if native:await native.close(browser)
        if context:
            try:await context.close()
            except PlaywrightError:
                if not native:raise
        self.network_events.pop(user,None)

    async def close(self):
        for user in set(self.contexts)|set(self.native_browsers):await self.close_user(user)
        for task in list(self.downloads):task.cancel()
        await asyncio.gather(*self.downloads,return_exceptions=True)
        if self.playwright:await self.playwright.stop()
