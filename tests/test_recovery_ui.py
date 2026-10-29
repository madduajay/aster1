"""Full HTTPS phone -> approval -> private iframe login -> approval -> confirmation."""
import asyncio,json,os,socket,time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest,uvicorn
from playwright.async_api import async_playwright,expect
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from personal_agent import config,browser_host as host_module,browser_tasks as task_module
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.browser_tasks import BrowserTasks,Worker
from personal_agent.browser_policy import action_binding
from personal_agent.browser_host import BrowserHost
from personal_agent.browser_dom import inspect_dom
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel
from validation.recovery_controls import VENUE,LOGIN

NATIVE_VENUE=VENUE.replace('width:650px;height:500px','width:calc(100% - 4px);max-width:650px;height:500px')
NATIVE_LOGIN=LOGIN.replace('<body>','''<style>body{font:16px system-ui;margin:16px}input,button{font:inherit;padding:10px;box-sizing:border-box}input{display:block;width:100%;max-width:320px}button{margin-top:12px}body:after{content:"Website footer";display:block;margin-top:850px}</style><body>''')
NATIVE_LOGIN=NATIVE_LOGIN.replace('</body>','''<button type=button id=hold-control onpointerdown="this.dataset.started=performance.now();this.dataset.held='yes'" onpointerup="this.dataset.elapsed=performance.now()-Number(this.dataset.started);this.dataset.held='no'" onclick="this.dataset.clicks=Number(this.dataset.clicks||0)+1">Hold control</button></body>''')


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chromium requires explicit enablement')
@pytest.mark.parametrize('entry',['chat','panel','locked-main'])
async def test_phone_private_takeover_approval_and_resume_https(database,users,credentials,tmp_path,monkeypatch,entry):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr(host_module,'public_url',AsyncMock());monkeypatch.setattr(task_module,'public_url',AsyncMock())
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    user=users[0]['id']
    def fields(page):return page if entry=='locked-main' else page.frame_locator('iframe')
    class Host(BrowserHost):
        async def context(self,user):
            fresh=user not in self.contexts;context=await super().context(user)
            if fresh:
                async def fixture(r):
                    body=NATIVE_LOGIN if entry=='locked-main' or 'identity.' in r.request.url else NATIVE_VENUE
                    if entry!='chat':body+='<script>window.eval=function(){throw new Error("eval is disabled")}</script>'
                    await r.fulfill(content_type='text/html',body=body)
                await context.route('https://**.fixture.test/**',fixture)
            return context
    host=Host(database)
    class Simulation(BrowserTasks):
        queue=asyncio.Queue()
        submissions=0
        async def start(self,user,run,call_id,objective,domains,inspect_only=False):
            row=self.db.one('SELECT id FROM browser_use_tasks WHERE call_id=?',(call_id,))
            if row:return row['id']
            tid='ui-recovery-task';self.db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created) VALUES(?,?,?,?,?,?,0,'running',0)",(tid,user,run,call_id,objective,json.dumps(domains)))
            host.set_scope(user,run,domains);host.run_for_user[user]=run
            website=(await host.context(user)).pages[0];await website.goto('https://booking.fixture.test/venue');await fields(website).locator('#phone').wait_for()
            host.set_view(user,run,{'url':website.url})
            self.workers[tid]=Worker(SimpleNamespace(returncode=None),'nonce',user,run,asyncio.Lock())
            self.simulation=asyncio.create_task(self.simulate(tid,website));return tid
        async def send(self,tid,payload):await self.queue.put(payload)
        async def simulate(self,tid,page):
            async def boundary(seq,op,params,node=None):
                state=await host.snapshot(user,self.workers[tid].run)
                if node:state['node']=node
                await self.before(tid,{'seq':seq,'operation':op,'params':params,'observed':state,'fingerprint':action_binding(op,params,state)})
                return await self.queue.get()
            phone=await boundary(1,'request_user',{'kind':'information','prompt':'Which phone number should I use?'})
            assert page.viewport_size=={'width':1280,'height':850},'Agent resumed before the live viewport was restored'
            if phone['response'].startswith('The user completed the information fields directly'):
                actual=await fields(page).locator('#phone').input_value()
                assert actual=='+1 202 555 0100',actual
            else:await fields(page).locator('#phone').fill(phone['response'])
            node={'tag':'BUTTON','name':'Send code','visible':True,'type':'button','role':'button','disabled':False}
            decision=await boundary(2,'click',{'index':1},node)
            if decision['decision']!='allow':return
            await fields(page).get_by_role('button',name='Send code').click()
            database.execute("UPDATE browser_use_actions SET status='executed' WHERE task_id=? AND seq=2",(tid,))
            await boundary(3,'request_user',{'kind':'manual','prompt':'Enter the verification code directly in the browser'})
            state=await host.snapshot(user,self.workers[tid].run);assert 'Signed in successfully' in state['text']
            node={'tag':'BUTTON','name':'Confirm booking','visible':True,'type':'button','role':'button','disabled':False}
            decision=await boundary(4,'click',{'index':2},node)
            if decision['decision']=='allow':self.submissions+=1
            self.db.execute("UPDATE browser_use_tasks SET status='completed',result=? WHERE id=?",(json.dumps({'status':'verified','verification':{'success':True,'reason':'Reservation confirmed for the requested party.'}}),tid))
            self.workers[tid].changed.set()
    manager=Simulation(database,host,'sk-controlled',None)
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Complete my reservation','official_domains':['booking.fixture.test','identity.fixture.test']},'id':'ui-recovery','type':'tool_call'}]),AIMessage(content='Reservation confirmed.')])
    async def until(condition):
        end=time.monotonic()+20
        while time.monotonic()<end:
            if condition():return
            await asyncio.sleep(.025)
        if hasattr(manager,'simulation') and manager.simulation.done():manager.simulation.result()
        raise TimeoutError('Recovery UI condition not reached')
    server=None;server_task=None
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'ui-recovery.sqlite')) as saver:
        engine=Engine(database,host,saver,'sk-controlled',model=model,browser_tasks=manager);service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False));server_task=asyncio.create_task(server.serve(sockets=[sock]));await until(lambda:server.started)
            async with async_playwright() as pw:
                chromium=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                page=await chromium.new_page(ignore_https_errors=False,viewport={'width':1400,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(config.ORIGIN);await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter');await page.locator('#password-form').wait_for(state='visible');await page.locator('#new-password').fill('recovery-ui-password-2026');await page.locator('#new-password').press('Enter');await page.locator('#composer').wait_for(state='visible')
                await page.locator('#input').fill('Complete my reservation');await page.locator('#input').press('Enter')
                await until(lambda:service.active(user) and service.active(user)['status']=='input');await expect(page.locator('#status')).to_contain_text('Waiting for your reply')
                info=dict(service.active(user));await page.reload();await expect(page.locator('#status')).to_contain_text('Waiting for your reply')
                await expect(page.locator('body')).to_have_class(__import__('re').compile(r'.*takeover.*'))
                assert await page.locator('#browser-view').evaluate('e=>e.open')
                await expect(page.locator('#browser-frame')).to_have_attribute('src',__import__('re').compile(r'^blob:'))
                assert (await page.locator('#browser-view').bounding_box())['width']>600
                website=host.contexts[user].pages[0]
                image=page.locator('#browser-frame')
                async def native_size():
                    await page.wait_for_function("""()=>{const i=document.querySelector('#browser-frame'),r=i.parentElement.getBoundingClientRect();return i.src.startsWith('blob:')&&i.naturalWidth===Math.floor(r.width)&&i.naturalHeight===Math.floor(r.height)}""")
                    dimensions=await image.evaluate('e=>({width:Math.floor(e.parentElement.getBoundingClientRect().width),height:Math.floor(e.parentElement.getBoundingClientRect().height)})')
                    assert website.viewport_size==dimensions
                    if (await page.evaluate('innerWidth'))>=1050:
                        panel=await page.locator('#browser-view').bounding_box()
                        assert panel['y']>=88 and panel['y']+panel['height']<=(await page.evaluate('innerHeight')),'Interactive panel was clipped by the header or viewport'
                        assert await image.evaluate('e=>{const r=e.getBoundingClientRect();return document.elementFromPoint(r.left+r.width/2,r.bottom-8)===e}'),'Chat composer covered the website input area'
                    return dimensions
                await native_size()
                if entry!='chat':
                    with pytest.raises(Exception,match='eval is disabled'):await website.evaluate('()=>document.title')
                async def click_website(locator):
                    size=await native_size();box=await locator.bounding_box();r=await image.bounding_box()
                    await image.click(position={'x':(box['x']+box['width']/2)*r['width']/size['width'],'y':(box['y']+box['height']/2)*r['height']/size['height']})
                if entry=='chat':
                    await page.locator('#input').fill('+1 202 555 0100');await page.locator('#input').press('Enter')
                else:
                    await page.screenshot(path=str(config.ROOT/'.runtime/website-panel-input-ui.png'))
                    await image.focus();await page.keyboard.type('x')
                    await expect(page.locator('#error')).to_contain_text('Click the website textbox')
                    await page.get_by_role('button',name='Continue',exact=True).click()
                    await expect(page.locator('#error')).to_contain_text('Complete the required website fields')
                    hold=fields(website).locator('#hold-control');box=await hold.bounding_box();size=await native_size();r=await image.bounding_box()
                    await page.mouse.move(r['x']+(box['x']+box['width']/2)*r['width']/size['width'],r['y']+(box['y']+box['height']/2)*r['height']/size['height'])
                    await page.mouse.down()
                    async def hold_state():return (await inspect_dom(website,['booking.fixture.test','identity.fixture.test'],"(()=>{const e=document.querySelector('#hold-control');return e?{held:e.getAttribute('data-held'),elapsed:e.getAttribute('data-elapsed'),clicks:e.getAttribute('data-clicks')}:{};})()"))[-1]['value']
                    for _ in range(50):
                        if (await hold_state()).get('held')=='yes':break
                        await asyncio.sleep(.02)
                    if (await hold_state()).get('held')!='yes':await page.screenshot(path=str(config.ROOT/'.runtime/held-input-debug.png'))
                    assert (await hold_state()).get('held')=='yes',(await page.locator('#error').inner_text(),host.controls[user].pointer_down)
                    await asyncio.sleep(.35)  # Intentional, real held input on a synthetic control.
                    await page.mouse.up()
                    for _ in range(50):
                        if (await hold_state()).get('held')=='no':break
                        await asyncio.sleep(.02)
                    held=await hold_state();assert held['held']=='no' and float(held['elapsed'])>=300 and held['clicks']=='1'
                    phone=fields(website).locator('#phone')
                    await click_website(phone)
                    await image.evaluate("e=>{const data=new DataTransfer();data.setData('text/plain','+1 202 ');e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}))}")
                    await page.locator('#website-keyboard').evaluate("e=>{e.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));e.value='555';e.dispatchEvent(new CompositionEvent('compositionend',{data:'555',bubbles:true}));e.dispatchEvent(new InputEvent('input',{data:'555',inputType:'insertCompositionText',bubbles:true}))}")
                    await page.keyboard.type(' 0100')
                    await expect(phone).to_have_value('+1 202 555 0100')
                    for size in [{'width':1200,'height':900},{'width':390,'height':840},{'width':1400,'height':1000}]:
                        await page.set_viewport_size(size);await native_size()
                        await expect(phone).to_have_value('+1 202 555 0100')
                        font=float((await inspect_dom(website,['booking.fixture.test','identity.fixture.test'],'parseFloat(getComputedStyle(document.body).fontSize)'))[-1]['value'])
                        rendered=font*(await image.bounding_box())['width']/website.viewport_size['width']
                        assert abs(rendered-16)<.1,'Website text was minified'
                    await image.dispatch_event('wheel',{'deltaY':350,'bubbles':True,'cancelable':True})
                    async def scroll_position():return (await inspect_dom(website,['booking.fixture.test','identity.fixture.test'],'scrollY'))[-1]['value']
                    for _ in range(50):
                        if await scroll_position()>0:break
                        await asyncio.sleep(.02)
                    assert await scroll_position()>0
                    await image.dispatch_event('wheel',{'deltaY':-800,'bubbles':True,'cancelable':True})
                    for _ in range(50):
                        if await scroll_position()==0:break
                        await asyncio.sleep(.02)
                    assert await scroll_position()==0
                    await page.screenshot(path=str(config.ROOT/'.runtime/browser-panel-native-size.png'))
                    await page.get_by_role('button',name='Continue',exact=True).click()
                await expect(page.get_by_role('button',name='Approve',exact=True)).to_be_visible()
                assert 'takeover' not in (await page.locator('body').get_attribute('class') or '')
                assert (await page.locator('#browser-view').bounding_box())['width']<=350
                await until(lambda:not host.controls)
                await page.get_by_role('button',name='Approve',exact=True).click()
                await until(lambda:service.active(user) and service.active(user)['status']=='input');await expect(page.locator('body')).to_have_class(__import__('re').compile(r'.*takeover.*'))
                manual=dict(service.active(user));assert manual['approval_id']!=info['approval_id']
                await page.reload();await expect(page.locator('body')).to_have_class(__import__('re').compile(r'.*takeover.*'))
                await expect(page.locator('#messages')).to_contain_text('Continue hands control back to the agent; Cancel stops this task.')
                await page.get_by_role('button',name='Continue',exact=True).click();await expect(page.locator('#error')).to_contain_text('any private login/verification step')
                await page.locator('#input').fill('654321');await page.locator('#input').press('Enter');await expect(page.locator('#error')).to_contain_text('in the browser')
                await click_website(fields(website).locator('#otp'))
                if entry!='chat':await page.keyboard.type('654321')
                else:
                    await page.keyboard.insert_text('654321')
                await expect(fields(website).locator('#otp')).to_have_value('654321')
                await click_website(fields(website).locator('#verify'))
                await expect(fields(website).get_by_role('heading',name='Signed in successfully')).to_be_visible()
                await page.get_by_role('button',name='Continue',exact=True).click();await expect(page.get_by_role('button',name='Approve',exact=True)).to_be_visible()
                assert manager.submissions==0
                await page.get_by_role('button',name='Approve',exact=True).click();await until(lambda:not service.active(user));await expect(page.locator('#messages')).to_contain_text('Reservation confirmed')
                assert manager.submissions==1 and not errors
                assert '654321' not in str(database.rows('SELECT * FROM messages'))+str(database.rows('SELECT * FROM events'))+str(database.rows('SELECT * FROM browser_use_actions'))
                assert not database.facts(user,inspect=True)
                assert 'takeover' not in (await page.locator('body').get_attribute('class') or '')
                assert await page.locator('#private-entry').count()==0 and await page.locator('#takeover-form').count()==0
                assert await page.locator('#website-keyboard').input_value()==''
                if entry!='chat':
                    assert '+1 202 555 0100' not in str(database.rows('SELECT * FROM messages'))+str(database.rows('SELECT * FROM browser_use_actions'))
                await chromium.close()
        finally:
            await service.close()
            if hasattr(manager,'simulation'):manager.simulation.cancel();await asyncio.gather(manager.simulation,return_exceptions=True)
            await host.close();await engine.client.close()
            if server:server.should_exit=True
            if server_task:await server_task
