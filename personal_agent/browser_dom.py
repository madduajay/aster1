"""Trusted, read-only DOM inspection independent of a site's JavaScript globals.

Playwright's main-world evaluate uses window.eval, which some sites replace.
Isolated DevTools worlds keep website CSP and scripts unchanged. No caller may
pass website/model-provided code to this module.
"""
from contextlib import asynccontextmanager
import asyncio

from playwright.async_api import Error as BrowserError

from .sources import allowed_url


FOCUS = r"""(()=>{
let e=document.activeElement;while(e?.shadowRoot?.activeElement)e=e.shadowRoot.activeElement;
const editable=!!e&&((e.matches('input:not([type=file]):not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]),textarea')&&!e.disabled&&!e.readOnly)||e.isContentEditable);
return {editable,multiline:editable&&(e.tagName==='TEXTAREA'||e.isContentEditable),input_mode:editable&&['tel','email','number','url'].includes(e.type)?e.type:'text'};
})()"""

OWNER = r"""function(){
const r=this.getBoundingClientRect(),s=getComputedStyle(this);
let e=document.activeElement;while(e?.shadowRoot?.activeElement)e=e.shadowRoot.activeElement;
return {visible:r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&!this.closest('[inert],[aria-hidden=true]'),focused:e===this,
box:{x:r.x,y:r.y,width:r.width,height:r.height},left:this.clientLeft,top:this.clientTop};
}"""


class InspectionUnavailable(BrowserError):
    def __init__(self):
        super().__init__('The website could not be inspected. The agent is still paused; select the website field and try again.')


class DOMReader:
    def __init__(self,page,domains):
        self.page,self.domains=page,domains
        self.sessions=[];self.targets={};self.worlds={}

    async def attach(self,target):
        client=await self.page.context.new_cdp_session(target)
        self.sessions.append(client)
        tree=(await client.send('Page.getFrameTree'))['frameTree']
        self.targets[tree['frame']['id']]=(client,tree)
        return client,tree

    async def world(self,frame_id,client,url):
        if frame_id in self.worlds:return self.worlds[frame_id]
        try:
            world=await client.send('Page.createIsolatedWorld',{'frameId':frame_id,'worldName':'personal-agent-input-observer'})
        except BrowserError:
            # Out-of-process frames need their own attachment. Frame identity is
            # checked from CDP's frame tree, never inferred from a matching URL.
            for frame in self.page.frames:
                if frame.parent_frame is None or frame.url!=url:continue
                try:await self.attach(frame)
                except BrowserError:continue
                if frame_id in self.targets:break
            target=self.targets.get(frame_id)
            if not target:raise InspectionUnavailable() from None
            client=target[0]
            world=await client.send('Page.createIsolatedWorld',{'frameId':frame_id,'worldName':'personal-agent-input-observer'})
        result=(client,world['executionContextId'])
        self.worlds[frame_id]=result
        return result

    async def value(self,client,world,expression):
        result=await client.send('Runtime.evaluate',{'expression':expression,'contextId':world,'returnByValue':True})
        if result.get('exceptionDetails') or 'value' not in result.get('result',{}):raise InspectionUnavailable()
        return result['result']['value']

    async def owner(self,parent,world,frame_id):
        node=await parent.send('DOM.getFrameOwner',{'frameId':frame_id})
        remote=await parent.send('DOM.resolveNode',{'backendNodeId':node['backendNodeId'],'executionContextId':world})
        oid=remote['object']['objectId']
        try:
            result=await parent.send('Runtime.callFunctionOn',{'objectId':oid,'functionDeclaration':OWNER,'returnByValue':True})
            if result.get('exceptionDetails'):raise InspectionUnavailable()
            return result['result']['value']
        finally:await parent.send('Runtime.releaseObject',{'objectId':oid})

    async def read(self,expression):
        client,root=await self.attach(self.page)
        if not allowed_url(root['frame']['url'],self.domains):raise ValueError('A website transition requires domain approval')
        values=[]
        async def visit(branch,client,active,offset=(0,0),box=None,permitted=True):
            if len(values)>=25:return
            frame=branch['frame'];fid=frame['id'];url=frame.get('url','')
            record={'url':url,'active':active,'box':box,'allowed':permitted}
            values.append(record)
            if not permitted:return  # Never inspect an unapproved frame's content.
            client,world=await self.world(fid,client,url)
            record['value']=await self.value(client,world,expression)
            branch=self.targets.get(fid,(None,branch))[1]
            for child in branch.get('childFrames',[])[:12]:
                flags={}
                try:
                    flags=await self.owner(client,world,child['frame']['id'])
                    if not flags['visible']:continue
                    b=flags['box'];b['x']+=offset[0];b['y']+=offset[1]
                    child_url=child['frame'].get('url','')
                    permitted=allowed_url(child_url,self.domains) or child_url in {'about:blank','about:srcdoc'}
                    await visit(child,client,active and flags['focused'],(b['x']+flags['left'],b['y']+flags['top']),b,permitted)
                except BrowserError:
                    # An unreadable active frame is a real inspection failure,
                    # not evidence that a field is empty or no field has focus.
                    if active and flags.get('focused'):raise InspectionUnavailable() from None
        await visit(root,client,True)
        return values


@asynccontextmanager
async def dom_reader(page,domains):
    reader=DOMReader(page,domains)
    try:yield reader
    finally:
        for client in reversed(reader.sessions):
            try:
                async with asyncio.timeout(1):await client.detach()
            except (BrowserError,TimeoutError):pass


async def inspect_dom(page,domains,expression):
    try:
        async with dom_reader(page,domains) as reader:
            async with asyncio.timeout(6):return await reader.read(expression)
    except TimeoutError:raise InspectionUnavailable() from None
