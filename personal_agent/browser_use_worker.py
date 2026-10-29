"""Isolated Browser Use runtime. Only this trusted process receives the GPT key.

The parent owns the browser profile, approvals and journal. Every action crosses
an acknowledged IPC boundary before Browser Use can execute it.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys

os.environ.update(ANONYMIZED_TELEMETRY='false',BROWSER_USE_CLOUD_SYNC='false',BROWSER_USE_LOGGING_LEVEL='critical',
                  CDP_LOGGING_LEVEL='CRITICAL',BROWSER_USE_DISABLE_EXTENSIONS='1',DO_NOT_TRACK='1')
sys.path.insert(0,str(Path(__file__).resolve().parent))
from browser_policy import ALLOWED_ACTIONS, WRITE_ACTIONS, READ_ACTIONS, action_binding
from browser_recovery import PAGE_OBSERVER, RecoveryBudget, combine_observations, input_need
from browser_diagnostics import diagnostic_stage, exception_details, clean_message, safe_target
from browser_connection import FreshObservationRequired, connection_error, resilient_browser
from browser_model import browser_model_options
NONCE=None

# The library may print progress; the parent reads only prefixed JSON records.
def emit(value):
    value['nonce']=NONCE
    sys.stdout.write('PA_BU:'+json.dumps(value,ensure_ascii=False)+'\n');sys.stdout.flush()


OBSERVE_NODE="""function(){
const e=this,d=e.ownerDocument,w=d.defaultView;
const visible=x=>{const r=x.getBoundingClientRect(),s=w.getComputedStyle(x);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&!x.closest('[inert],[aria-hidden="true"]')};
const clean=x=>String(x||'').replace(/\\s+/g,' ').trim();
const name=clean(e.getAttribute('aria-label')||(e.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>d.getElementById(id)?.textContent||'').join(' ')||Array.from(e.labels||[]).map(x=>x.innerText).join(' ')||e.innerText||e.title||e.placeholder||e.name);
const control=e.tagName==='LABEL'&&e.control?e.control:e,sensitive=control.matches('input[type=password],input[autocomplete^="cc-"],input[autocomplete="one-time-code"]')||/password|passcode|verification.?code|one.?time|\\botp\\b|\\bcvv\\b|\\bcvc\\b|card.?number/i.test(name+' '+control.name+' '+control.autocomplete);
const f=control.form||e.closest('form'),searchRoot=e.closest('search,[role=search]');
return {url:d.location.href,node:{tag:e.tagName,name:name.slice(0,300),role:control.getAttribute('role')||({A:'link',SELECT:'combobox',INPUT:{radio:'radio',checkbox:'checkbox'}[control.type]||'textbox'})[control.tagName]||'button',type:control.type||'',href:e.href||'',id:e.id,name_attr:control.name||'',disabled:!!control.disabled||control.getAttribute('aria-disabled')==='true',checked:control.checked??null,selected:control.getAttribute('aria-selected'),value:sensitive?'[hidden]':String(control.value||'').slice(0,500),sensitive,editable:e.isContentEditable,visible:visible(e),search_context:!!searchRoot,
form:f?{method:(f.method||'get').toLowerCase(),action:f.action||d.location.href,role:f.getAttribute('role')||'',sensitive:!!f.querySelector('input[type=password],input[autocomplete^="cc-"],input[autocomplete="one-time-code"]')}:null}};
}"""
OBSERVE_PAGE=PAGE_OBSERVER


async def selector_node(browser,selector):
    page=await browser.get_current_page()
    elements=await page.get_elements_by_css_selector(selector)
    if len(elements)!=1:raise ValueError('Selector must identify exactly one observed element')
    element=elements[0]
    value=await element.evaluate(OBSERVE_NODE.replace('function(){','() => {',1))
    if isinstance(value,str):value=json.loads(value)
    # Actor creates a fresh CDP attachment for each Page wrapper. That session
    # id is transport state, not node identity; target + backend node are stable.
    value['node']['identity']=[page._target_id,element._backend_node_id]
    return element,value


async def frame_observations(browser,cdp):
    """Inspect visible same-origin and out-of-process child frames, never other tabs."""
    snapshots=[]
    tree=await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
    async def visit(branch,parent):
        for child in branch.get('childFrames',[])[:12]:
            fid=child['frame']['id'];client=parent
            try:
                owner=await parent.cdp_client.send.DOM.getFrameOwner(params={'frameId':fid},session_id=parent.session_id)
                remote=await parent.cdp_client.send.DOM.resolveNode(params={'backendNodeId':owner['backendNodeId']},session_id=parent.session_id)
                oid=remote['object']['objectId']
                try:
                    state=await parent.cdp_client.send.Runtime.callFunctionOn(params={'objectId':oid,'functionDeclaration':"function(){const r=this.getBoundingClientRect(),s=getComputedStyle(this);return {visible:r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&!this.closest('[inert],[aria-hidden=true]'),focused:document.activeElement===this}}",'returnByValue':True},session_id=parent.session_id)
                    flags=state.get('result',{}).get('value',{})
                finally:await parent.cdp_client.send.Runtime.releaseObject(params={'objectId':oid},session_id=parent.session_id)
                if not flags.get('visible'):continue
                try:
                    world=await client.cdp_client.send.Page.createIsolatedWorld(params={'frameId':fid,'worldName':'personal-agent-observer'},session_id=client.session_id)
                except Exception:
                    client=await browser.get_or_create_cdp_session(target_id=fid,focus=False)
                    world=await client.cdp_client.send.Page.createIsolatedWorld(params={'frameId':fid,'worldName':'personal-agent-observer'},session_id=client.session_id)
                value=await client.cdp_client.send.Runtime.evaluate(params={'expression':OBSERVE_PAGE,'contextId':world['executionContextId'],'returnByValue':True},session_id=client.session_id)
                frame=value.get('result',{}).get('value',{})
                if frame:frame['focused']=flags.get('focused',False);snapshots.append(frame)
                await visit(child,client)
            except Exception:continue  # Frame may detach during navigation; fresh observations follow.
    await visit(tree.get('frameTree',{}),cdp)
    return snapshots


async def _observe(browser,params=None):
    with diagnostic_stage('browser_connection'):
        cdp=await browser.get_or_create_cdp_session()
    with diagnostic_stage('page_snapshot'):
        result=await cdp.cdp_client.send.Runtime.evaluate(params={'expression':OBSERVE_PAGE,'returnByValue':True},session_id=cdp.session_id)
        if result.get('exceptionDetails'):
            error=result['exceptionDetails']
            raise RuntimeError('Page snapshot: '+error.get('exception',{}).get('description',error.get('text','JavaScript evaluation failed')))
        page=result.get('result',{}).get('value',{})
        if not isinstance(page,dict):raise TypeError('Page snapshot did not return an object')
    with diagnostic_stage('frame_snapshot'):
        frames=await frame_observations(browser,cdp)
    with diagnostic_stage('combine_observations'):
        page=combine_observations(page,frames)
        page['target_id']=cdp.target_id
    if (params or {}).get('_focused'):
        with diagnostic_stage('resolve_keyboard_focus'):
            focused=await cdp.cdp_client.send.Runtime.evaluate(params={'expression':'(()=>{let e=document.activeElement;while(e?.shadowRoot?.activeElement)e=e.shadowRoot.activeElement;return e})()'},session_id=cdp.session_id)
            oid=focused.get('result',{}).get('objectId')
            if not oid:raise ValueError('No observed keyboard focus')
            try:
                described=await cdp.cdp_client.send.DOM.describeNode(params={'objectId':oid},session_id=cdp.session_id)
                data=await cdp.cdp_client.send.Runtime.callFunctionOn(params={'objectId':oid,'functionDeclaration':OBSERVE_NODE,'returnByValue':True},session_id=cdp.session_id)
                if data.get('exceptionDetails'):raise ValueError('Keyboard focus is stale')
                value=data['result']['value'];page['node']=value['node'];page['frame_url']=value['url']
                page['node']['identity']=[cdp.target_id,described['node']['backendNodeId']]
                if page['node']['tag']=='IFRAME':raise ValueError('Keyboard focus is inside an unobserved frame')
            finally:await cdp.cdp_client.send.Runtime.releaseObject(params={'objectId':oid},session_id=cdp.session_id)
        return page
    if (params or {}).get('selector'):
        with diagnostic_stage('resolve_css_selector'):
            _,value=await selector_node(browser,params['selector'])
            page['node']=value['node'];page['frame_url']=value.get('url')
            if params.get('target_selector'):
                _,target=await selector_node(browser,params['target_selector'])
                page['destination_node']=target['node']
        return page
    index=(params or {}).get('index')
    if index or (params or {}).get('coordinate_x') is not None:
        with diagnostic_stage('resolve_target_reference'):
            node=await browser.get_element_by_index(index) if index else await browser.get_dom_element_at_coordinates(params['coordinate_x'],params['coordinate_y'])
            if node is None:raise ValueError('Observed browser target is stale')
        with diagnostic_stage('resolve_dom_node'):
            client=await browser.cdp_client_for_node(node)
            remote=await client.cdp_client.send.DOM.resolveNode(params={'backendNodeId':node.backend_node_id},session_id=client.session_id)
            oid=remote['object']['objectId']
        try:
            with diagnostic_stage('read_target_metadata'):
                data=await client.cdp_client.send.Runtime.callFunctionOn(params={'objectId':oid,'functionDeclaration':OBSERVE_NODE,'returnByValue':True},session_id=client.session_id)
                if data.get('exceptionDetails'):raise ValueError('Target detached')
                value=data.get('result',{}).get('value',{})
                page['node']=value.get('node',{})
                page['frame_url']=value.get('url')
                page['node']['identity']=[node.target_id,node.backend_node_id,node.frame_id]
        finally:
            with diagnostic_stage('release_target_reference'):
                await client.cdp_client.send.Runtime.releaseObject(params={'objectId':oid},session_id=client.session_id)
    return page


async def observe(browser,params=None):
    recovery=getattr(browser,'_transport_recovery',None)
    generation=recovery.generation if recovery else 0
    try:
        page=await _observe(browser,params)
    except Exception as exc:
        if not connection_error(exc):raise
        emit({'type':'connection','text':'Browser connection interrupted. Reconnecting to the same tab and inspecting the page…'})
        with diagnostic_stage('connection_recovery'):
            async with asyncio.timeout(35):
                await browser.reconnect()
                page=await _observe(browser)  # Inspect first, without old target references.
        if params:
            raise FreshObservationRequired('Browser connection restored. The old action was not replayed. Read the refreshed browser state and choose a fresh target.') from exc
    if recovery:
        recovery.last_target=page['target_id']
        if params and recovery.generation!=generation:
            raise FreshObservationRequired('Browser connection changed. Rediscover the target in the current page before continuing.')
    return page


def preserve_application_dialogs():
    """Disable Browser Use's default auto-accept; the parent handles dialogs."""
    from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
    from browser_use.browser.events import TabCreatedEvent
    async def preserve_native_dialog(self,event: TabCreatedEvent) -> None:
        return
    preserve_native_dialog.__name__='on_TabCreatedEvent'
    PopupsWatchdog.on_TabCreatedEvent=preserve_native_dialog


async def main():
    global NONCE
    reader=asyncio.StreamReader(limit=2_000_000)
    protocol=asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda:protocol,sys.stdin)
    async def receive():
        line=await reader.readline()
        if not line:raise EOFError('Application connection closed')
        return json.loads(line)
    init=await receive()
    NONCE=init['nonce']
    # Browser Use imports are deliberately confined to its own virtualenv.
    from browser_use import Agent, Browser, ChatOpenAI, Tools, ActionResult
    from browser_use.llm.openai.chat import ChatOpenAI as BaseModel
    from browser_use.browser import BrowserSession
    preserve_application_dialogs()
    logging.disable(logging.CRITICAL)
    seq=init.get('start_seq',0)
    inspected_selectors=set()
    recovery=RecoveryBudget()
    class BudgetModel(BaseModel):
        calls=init.get('used_calls',0)
        async def ainvoke(self,*args,**kwargs):
            self.calls+=1
            if self.calls>init.get('model_call_limit',30):raise RuntimeError('Browser model budget reached')
            return await super().ainvoke(*args,**kwargs)
    llm=BudgetModel(model=init['model'],api_key=init.pop('key'),base_url='https://api.openai.com/v1',timeout=40,max_retries=0,max_completion_tokens=4096,**browser_model_options(init['model']))
    browser=resilient_browser(Browser)(cdp_url=init['cdp_url'],keep_alive=True,allowed_domains=init['allowed_domains'],
        enable_default_extensions=False,captcha_solver=False,auto_download_pdfs=False,cross_origin_iframes=True,highlight_elements=False,permissions=[],
        downloads_path=str(Path(init['workspace'])/'downloads'))
    async def ask_user(kind,prompt):
        nonlocal seq
        seq+=1
        state=await observe(browser)
        params={'kind':kind,'prompt':prompt}
        emit({'type':'before','seq':seq,'operation':'request_user','params':params,'observed':state,'model_calls':llm.calls,'fingerprint':action_binding('request_user',params,state)})
        paused=__import__('time').monotonic()
        reply=await receive()
        recovery.started+=__import__('time').monotonic()-paused
        if reply.get('seq')!=seq or reply.get('decision')!='allow':
            raise EOFError('User input cancelled')
        # Credentials entered in takeover never cross this pipe.
        return reply.get('response','The user completed the manual step. Inspect the current page before continuing.')

    class GuardedTools(Tools):
        stopped=False
        exhausted=False
        uncertain=bool(init.get('uncertain_submission'))
        def retry(self,name,params,state,phase):
            emit({'type':'recovery','operation':name,'phase':phase,'attempt':recovery.failures+1})
            if recovery.failure(name,params,state,phase):
                return ActionResult(error='Recovery needed ('+phase+'). Reinspect the active page/frame; wait for loading, find a fresh control or choose another observed interaction. Do not repeat the same failing action. No unconfirmed submission may be repeated.')
            self.stopped=True
            self.exhausted=True
            return ActionResult(is_done=True,success=False,extracted_content='Recovery limit reached after '+str(recovery.failures)+' failed attempts. The attempted approaches and current blocker will be reported; no further submission was made.')
        async def act(self,action,browser_session,**kwargs):
            nonlocal seq
            values=action.model_dump(exclude_unset=True)
            if len(values)!=1:return ActionResult(error='Only one browser action may execute at a time.')
            name,params=next(iter(values.items()))
            omitted_values=False
            if name=='find_elements' and 'value' in params.get('attributes',[]):
                params['attributes']=[a for a in params['attributes'] if a!='value']
                action=action.__class__.model_validate({name:params})
                omitted_values=True
            target_params=params if name in {'inspect_selector','click_selector','hover_selector','drag_selector'} else {k:v for k,v in params.items() if k not in {'selector','target_selector'}}
            if name=='send_keys':target_params={**target_params,'_focused':True}
            if name not in ALLOWED_ACTIONS:return ActionResult(error='Action disabled by application policy.')
            if self.stopped:return ActionResult(is_done=True,success=False,extracted_content='Task stopped. Inspect the browser before retrying.')
            if name=='request_user':
                response=await ask_user(params.get('kind','information'),params['prompt'])
                return ActionResult(extracted_content='User response for this task only: '+response)
            if self.uncertain and name not in READ_ACTIONS:
                return self.retry(name,params,{},'uncertain submission: inspect only')
            seq+=1
            dispatched=False
            before={};category='';stage='observe_before_action'

            try:
                before=await observe(browser_session,target_params)
                stage='check_user_input'
                need=input_need(before)
                if need and (name=='done' or before.get('manual_required') and name not in READ_ACTIONS):
                    response=await ask_user(need['kind'],need['prompt'])
                    return ActionResult(extracted_content='The user supplied the missing step: '+response+'. Reinspect and continue the original goal.')
                if name in {'click_selector','hover_selector','drag_selector'}:
                    selectors=[params['selector']]+([params['target_selector']] if name=='drag_selector' else [])
                    if any((before.get('url'),selector) not in inspected_selectors for selector in selectors):
                        return ActionResult(error='First use inspect_selector on each exact selector. No action was dispatched.')
                stage='bind_action'
                binding=action_binding(name,params,before)
                stage='send_action_for_validation'
                emit({'type':'before','seq':seq,'operation':name,'params':params,'observed':before,'model_calls':llm.calls,'fingerprint':binding})
                stage='await_validation_reply'
                reply=await receive()
                if reply.get('seq')!=seq or reply.get('decision')!='allow':
                    if reply.get('retry'):
                        if reply.get('inspect_only'):self.uncertain=True
                        return self.retry(name,params,before,reply.get('reason','invalid target'))
                    self.stopped=True
                    return ActionResult(is_done=True,success=False,extracted_content=reply.get('reason','Browser action rejected; no action performed.'))
                category=reply.get('category','')
                if reply.get('allowed_domains'):
                    browser_session.browser_profile.allowed_domains=reply['allowed_domains']
                stage='revalidate_action'
                current=await observe(browser_session,target_params)
                if action_binding(name,params,current)!=binding:
                    emit({'type':'changed','seq':seq,'observed':current})
                    return self.retry(name,params,current,'page changed before dispatch; approval invalidated')
                stage='request_dispatch'
                emit({'type':'dispatch','seq':seq})
                ack=await receive()
                if ack.get('seq')!=seq or ack.get('decision')!='allow':
                    self.stopped=True
                    return ActionResult(is_done=True,success=False,extracted_content=ack.get('reason','Dispatch cancelled'))
                dispatched=True
                stage='execute_action'
                result=await super().act(action,browser_session,**kwargs)
                if result.error:
                    emit({'type':'diagnostic','seq':seq,'details':{'operation':name,'stage':stage,'error_class':'ToolError','message':clean_message(result.error,[params.get('text'),(before.get('node') or {}).get('value')]),'dispatched':True,'target':safe_target(params,before),'trace':[]}})
                stage='observe_after_action'
                if name=='find_elements' and result.extracted_content:
                    result.extracted_content='IMPORTANT: bracketed numbers below are query-result positions, NOT clickable browser_state indices. Use find_controls for actual indices. For controls missing from that list, use inspect_selector on an exact unique CSS selector, then click_selector.\n'+result.extracted_content
                    if omitted_values:result.extracted_content+='\nPrivate field values were excluded. inspect_selector returns the safe state of one exact control.'
                # Browser Use otherwise prefers a brief long_term_memory such
                # as "Found 3 elements" and hides the actual query result from
                # the next model step. Preserve its documented read-state path.
                if result.extracted_content and result.long_term_memory:
                    result.include_extracted_content_only_once=True
                if name=='navigate' and not result.error:
                    try:
                        client=await browser_session.get_or_create_cdp_session()
                        await client.cdp_client.send.Runtime.evaluate(params={'expression':"document.querySelector('main,[role=main]')?.scrollIntoView({block:'start'})",'awaitPromise':True},session_id=client.session_id)
                    except Exception:pass
                for attempt in range(4):
                    try:
                        try:after=await observe(browser_session,target_params)
                        except Exception:after=await observe(browser_session)
                        break
                    except Exception:
                        if attempt==3:raise
                        await asyncio.sleep(.2)
                emit({'type':'after','seq':seq,'observed':after,'error':bool(result.error),'is_done':result.is_done,'reported_success':result.success})
                if name in WRITE_ACTIONS and not result.error:
                    # Associated-label state may be absent from the library's
                    # indexed DOM. Give the planner actual post-action feedback
                    # so it does not keep clicking an already-selected option.
                    result.extracted_content=(result.extracted_content or '')+'\nObserved resulting state (website data): '+json.dumps({
                        'url':after.get('url'),'target':after.get('node'),'selections':after.get('selections',[]),
                        'prices':after.get('prices',[]),'page_text':after.get('text','')[:4000]},ensure_ascii=False)
                    if result.long_term_memory:result.include_extracted_content_only_once=True
                if result.error and name not in {'wait','extract','search_page','find_elements','scroll','find_text','dropdown_options'}:
                    self.uncertain=category in {'approval','mutation'}
                    return self.retry(name,params,after,'uncertain submission; inspect outcome only' if self.uncertain else 'interaction failed')
                return result
            except Exception as exc:
                details=exception_details(exc,operation=name,stage=stage,params=params,state=before,dispatched=dispatched)
                emit({'type':'diagnostic','seq':seq,'details':details})
                if not dispatched:
                    if stage=='revalidate_action':
                        emit({'type':'changed','seq':seq})  # Revoke any pre-dispatch approval.
                    return self.retry(name,params,before,type(exc).__name__+' in '+(getattr(exc,'browser_stage',None) or stage)+' before dispatch')
                self.uncertain=category in {'approval','mutation'}
                import traceback
                frame=traceback.extract_tb(exc.__traceback__)[-1]
                emit({'type':'fault','seq':seq,'error_class':type(exc).__name__,'location':Path(frame.filename).name+':'+str(frame.lineno)})
                return self.retry(name,params,before,'connection/action failure; inspect before continuing')
    tools=GuardedTools()
    @tools.action('Pause this task for missing non-secret information, or request manual website login/human verification. Use kind=information for phone numbers or other missing details; kind=manual for passwords, verification codes, payment secrets or CAPTCHA. Never ask for secrets in chat. The application persists the pause and resumes this same goal.')
    async def request_user(prompt: str,kind: str='information'):
        return ActionResult(extracted_content='User input is handled by the application.')
    @tools.action('Inspect an exact unique CSS selector with Browser Use Actor. Use for visible labels or controls absent from browser_state indices. Returns the real control state; inspect before click_selector, hover_selector or drag_selector.')
    async def inspect_selector(selector: str,browser_session: BrowserSession):
        _,value=await selector_node(browser_session,selector)
        inspected_selectors.add((value['url'],selector))
        return ActionResult(extracted_content=json.dumps(value))
    @tools.action('Click a unique visible CSS selector already checked by inspect_selector, using Browser Use Actor. Useful for associated labels omitted from indexed DOM. Application approval applies.')
    async def click_selector(selector: str,browser_session: BrowserSession):
        element,_=await selector_node(browser_session,selector)
        await element.click()
        return ActionResult(extracted_content='Clicked the inspected element. Verify the resulting state.')
    @tools.action('Hover over a unique visible CSS selector already checked by inspect_selector, using Browser Use Actor.')
    async def hover_selector(selector: str,browser_session: BrowserSession):
        element,_=await selector_node(browser_session,selector)
        await element.hover()
        return ActionResult(extracted_content='Hovered over the inspected element. Read the current state.')
    @tools.action('Drag between two unique visible CSS selectors, each already checked by inspect_selector. Uses Browser Use Actor and requires application approval.')
    async def drag_selector(selector: str,target_selector: str,browser_session: BrowserSession):
        source,_=await selector_node(browser_session,selector)
        target,_=await selector_node(browser_session,target_selector)
        await source.drag_to(target)
        return ActionResult(extracted_content='Dragged between the inspected elements. Verify the resulting state.')
    @tools.action('Request application approval for an observed login/payment-provider link before navigating outside the current domain scope.')
    async def request_domain(url: str):
        return ActionResult(extracted_content='Application approved the observed provider transition. Navigate to the approved URL; research citations retain their original scope.')
    @tools.action('Find interactive controls by label/text/value using Browser Use DOM data. Returns actual clickable browser_state indices, unlike find_elements result positions. Prefer this to locate a specific size, color, button or field.')
    async def find_controls(query: str,browser_session: BrowserSession):
        words=query.casefold().split();matches=[]
        for index,node in (await browser_session.get_selector_map()).items():
            attrs=node.attributes or {}
            sensitive=attrs.get('type')=='password' or str(attrs.get('autocomplete','')).startswith('cc-') or attrs.get('autocomplete')=='one-time-code'
            label=(attrs.get('aria-label') or attrs.get('name') or '[private input]') if sensitive else node.get_meaningful_text_for_llm()[:240]
            searchable=' '.join([label,*[str(attrs.get(k,'')) for k in ['aria-label','title','name','role']]]).casefold()
            if not all(w in searchable for w in words):continue
            parent=node;navigation=False
            while parent:
                if parent.tag_name in {'nav','header','footer'}:navigation=True;break
                parent=parent.parent_node
            matches.append({'browser_state_index':index,'tag':node.tag_name,'label':label,
                'type':attrs.get('type'),'role':attrs.get('role'),'disabled':attrs.get('disabled',False),
                'navigation':navigation})
        matches.sort(key=lambda item:(item['navigation'],item['tag']=='a'))
        return ActionResult(extracted_content=json.dumps({'controls':matches[:40],'instruction':'Only browser_state_index is a click/input index. If no control matches, do not repeat find_controls: use find_elements to discover an exact CSS selector, inspect_selector to check it, then click_selector. Do not invent an index.'}))
    tools.set_coordinate_clicking(True)
    tools.registry.registry.actions={k:v for k,v in tools.registry.registry.actions.items() if k in ALLOWED_ACTIONS}
    async def step(agent):
        await observe(browser)  # Repair transport before the library captures model state.
        emit({'type':'progress','step':agent.state.n_steps,'model_calls':llm.calls})
    agent=Agent(task=init['task'],llm=llm,browser=browser,tools=tools,max_actions_per_step=1,max_failures=6,
        use_vision=True,use_judge=False,calculate_cost=False,enable_signal_handler=False,directly_open_url=False,step_timeout=3600,
        file_system_path=init['workspace'],available_file_paths=init.get('available_file_paths',[]),
        extend_system_message=init['instructions'],register_new_step_callback=None)
    async def run():
        history=await agent.run(max_steps=init.get('max_steps',30),on_step_start=step)
        try:observed=await observe(browser)
        except Exception as exc:
            emit({'type':'diagnostic','details':exception_details(exc,operation='inspect',stage='final_observation',dispatched=False)})
            observed={}  # Never verify with stale page evidence or crash the finished task.
        emit({'type':'done','reported_success':history.is_successful(),'result':history.final_result(),
              'observed':observed,'model_calls':llm.calls,'recovery_attempts':recovery.journal,'recovery_exhausted':tools.exhausted})
    parent=os.getppid()
    async def watch_parent():
        while os.getppid()==parent:await asyncio.sleep(1)
        raise EOFError('Parent exited')
    task=asyncio.create_task(run());watch=asyncio.create_task(watch_parent())
    try:
        finished,_=await asyncio.wait([task,watch],return_when=asyncio.FIRST_COMPLETED)
        for f in finished:await f
    finally:
        agent.stop();task.cancel();watch.cancel()
        await asyncio.gather(task,watch,return_exceptions=True)
        await browser.stop()


if __name__=='__main__':
    try:asyncio.run(main())
    except BaseException as exc:
        import traceback
        frame=traceback.extract_tb(exc.__traceback__)[-1]
        emit({'type':'fatal','error_class':type(exc).__name__,'location':Path(frame.filename).name+':'+str(frame.lineno),'details':exception_details(exc,operation='worker',stage='worker_exit',dispatched=None)})
        raise SystemExit(1)
