"""Authenticated, pause-bound live Chromium display and direct user input.

Frames and keystrokes are transient. They never enter the agent, chat, or audit
journal. This connects only to a user-owned page; no CDP endpoint is exposed.
"""
import asyncio
import base64
import json
import time

from playwright.async_api import Error as BrowserError
from pydantic import BaseModel, Field, FiniteFloat, ValidationError
from starlette.websockets import WebSocketDisconnect
from .browser_dom import InspectionUnavailable


class ViewportSize(BaseModel):
    width: int = Field(ge=240,le=2400)
    height: int = Field(ge=240,le=1600)


class ControlCommand(BaseModel):
    id: int = Field(ge=1,le=1_000_000)
    operation: str = Field(pattern='^(click|pointerdown|pointerup|type|key|scroll|resize)$')
    x: FiniteFloat | None = None
    y: FiniteFloat | None = None
    text: str | None = Field(default=None,max_length=2000)
    key: str | None = Field(default=None,max_length=32)
    width: int | None = Field(default=None,ge=240,le=2400)
    height: int | None = Field(default=None,ge=240,le=1600)


class BrowserControl:
    def __init__(self,host,socket,user,run,input_id,authorize,viewport=None):
        self.host,self.socket,self.user,self.run,self.input_id=host,socket,user,run,input_id
        self.authorize=authorize
        self.stop=asyncio.Event();self.closed=asyncio.Event()
        self.frames=asyncio.Queue(maxsize=1);self.send_lock=asyncio.Lock()
        self.client=None;self.page=None
        self.last_frame=0
        self.viewport=viewport or {'width':1280,'height':850}
        self.original_viewport=None
        self.restore_failed=False
        self.pointer_down=False

    async def cancel_pointer(self):
        if self.pointer_down and self.page and not self.page.is_closed():
            self.pointer_down=False
            # Abort a lost/cancelled gesture off the page, rather than leaving
            # a website receiving an indefinitely held button after disconnect.
            await self.page.mouse.move(-1,-1)
            await self.page.mouse.up()

    async def resize(self,width,height):
        size=ViewportSize(width=width,height=height).model_dump()
        async with self.host.preview_locks.setdefault(self.user,asyncio.Lock()):
            self.check()
            await self.cancel_pointer()
            await self.page.set_viewport_size(size)
            self.viewport=size
            self.host.views[(self.user,self.run)]['captured']=0

    def check(self):
        if self.stop.is_set() or not self.authorize():raise LookupError('Browser control revoked')
        self.host.pending_manual(self.user,self.run,self.input_id)

    async def send(self,value):
        async with self.send_lock:
            if isinstance(value,bytes):await self.socket.send_bytes(value)
            else:await self.socket.send_json(value)

    async def on_frame(self,event):
        # Acknowledge immediately, retaining only the newest unsent frame.
        try:
            await self.client.send('Page.screencastFrameAck',{'sessionId':event['sessionId']})
            if len(event.get('data',''))>4_000_000:return
            frame=base64.b64decode(event['data'],validate=True)
            if self.frames.full():self.frames.get_nowait()
            self.frames.put_nowait(frame)
        except (BrowserError,ValueError,asyncio.QueueFull):pass

    async def display(self):
        while True:
            self.check()
            # Detect tab/profile loss, resume, cancellation and logout even
            # while the website is idle and produces no screencast frames.
            if await self.host.view_page(self.user,self.run)!=self.page:raise LookupError('Browser tab changed')
            try:frame=await asyncio.wait_for(self.frames.get(),.25)
            except TimeoutError:continue
            delay=.12-(time.monotonic()-self.last_frame)
            if delay>0:await asyncio.sleep(delay)
            while not self.frames.empty():frame=self.frames.get_nowait()
            self.check()
            await self.send(frame)
            self.last_frame=time.monotonic()

    async def commands(self):
        last_id=0
        while True:
            raw=await self.socket.receive_text()
            self.check()
            if len(raw)>16000:
                await self.socket.close(code=1009);return
            data={}
            try:
                data=json.loads(raw)
                command=ControlCommand.model_validate(data)
                if command.id<=last_id:
                    await self.send({'type':'ack','id':command.id,'error':'This browser input was already handled. Inspect the page before continuing.'});continue
                last_id=command.id
            except (ValidationError,ValueError,TypeError):
                identifier=data.get('id',0) if isinstance(data,dict) else 0
                if not isinstance(identifier,int) or not 1<=identifier<=1_000_000:identifier=0
                await self.send({'type':'ack','id':identifier,'error':'Unsupported browser input.'});continue
            try:
                if command.operation=='resize':
                    await self.resize(command.width,command.height)
                else:
                    # A click on an old frame must not land on a different field
                    # after a responsive layout change. Never replay that click.
                    if command.operation in {'click','pointerdown','pointerup'} and {'width':command.width,'height':command.height}!=self.page.viewport_size:
                        await self.cancel_pointer()
                        raise ValueError('Stale browser viewport')
                    if command.operation=='pointerdown':
                        if self.pointer_down:raise ValueError('A mouse button is already held')
                        self.pointer_down=True
                    if command.operation=='pointerup' and not self.pointer_down:raise ValueError('No mouse button is held')
                    await self.host.manual_action(self.user,self.run,self.input_id,command.operation,
                        command.x,command.y,command.text,command.key)
                    if command.operation=='pointerup':self.pointer_down=False
                focus={} if command.operation=='resize' else await self.host.input_focus(self.user,self.run,self.input_id)
                await self.send({'type':'ack','id':command.id,**focus,**self.viewport})
            except (ValueError,BrowserError) as exc:
                if command.operation in {'pointerdown','pointerup'}:
                    try:await self.cancel_pointer()
                    except BrowserError:pass
                # Neither browser exceptions nor validation details may echo
                # a typed password/code back through an error message.
                known={'Stale browser viewport':'The website layout changed. Wait for the panel to update, then select the field again.',
                    'Click an editable website field first':'Click the website textbox before typing.',
                    'A website transition requires domain approval':'The website opened a different provider that needs approval. The agent remains paused.'}
                message=str(exc) if isinstance(exc,InspectionUnavailable) else known.get(str(exc),'The website changed or the selected control is unavailable. Inspect the page and select the field again.')
                await self.send({'type':'ack','id':command.id,'error':message})

    async def run_control(self):
        host=self.host
        async with host.control_locks.setdefault(self.user,asyncio.Lock()):
            previous=host.controls.get(self.user)
            if previous:
                previous.stop.set()
                await asyncio.wait_for(previous.closed.wait(),7)
            self.check()
            host.controls[self.user]=self
        jobs=[]
        try:
            self.page=await host.view_page(self.user,self.run)
            self.original_viewport=self.page.viewport_size
            await self.resize(**self.viewport)
            context=host.contexts[self.user]
            self.client=await context.new_cdp_session(self.page)
            self.client.on('Page.screencastFrame',self.on_frame)
            await self.client.send('Page.startScreencast',{'format':'jpeg','quality':85,'maxWidth':2400,'maxHeight':1600,'everyNthFrame':1})
            await self.send({'type':'ready',**self.viewport})
            # Some headless pages do not paint until foregrounded.
            await self.client.send('Page.bringToFront')
            jobs=[asyncio.create_task(self.display()),asyncio.create_task(self.commands())]
            done,_=await asyncio.wait(jobs,return_when=asyncio.FIRST_COMPLETED)
            for job in done:await job
        finally:
            self.stop.set()
            for job in jobs:job.cancel()
            await asyncio.gather(*jobs,return_exceptions=True)
            if self.client:
                try:
                    async with asyncio.timeout(2):
                        await self.client.send('Page.stopScreencast')
                        await self.client.detach()
                except (BrowserError,TimeoutError):pass
            if self.page and self.original_viewport and not self.page.is_closed():
                try:
                    async with asyncio.timeout(3):
                        await self.cancel_pointer()
                        await self.page.set_viewport_size(self.original_viewport)
                except (BrowserError,TimeoutError):self.restore_failed=True
            if host.controls.get(self.user) is self:host.controls.pop(self.user,None)
            self.closed.set()


async def serve_control(host,socket,user,run,input_id,authorize,viewport=None):
    control=BrowserControl(host,socket,user,run,input_id,authorize,viewport)
    try:await control.run_control()
    except (LookupError,ValueError,KeyError,BrowserError,TimeoutError,WebSocketDisconnect,RuntimeError):pass
    finally:
        control.closed.set()
        try:await socket.close(code=1000)
        except (RuntimeError,WebSocketDisconnect):pass
