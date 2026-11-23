"""Live text-to-image, visual review, edit and browser persistence acceptance."""
import asyncio
import json
import logging
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

import uvicorn
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import async_playwright

from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.db import Database
from personal_agent.service import Service


class NoBrowser:
    async def close_user(self,*args):pass
    async def delegate(self,*args,**kwargs):raise RuntimeError('Creative acceptance must not use a website')


async def main():
    for name in ('httpx','httpcore','deepagents.profiles.harness.harness_profiles'):logging.getLogger(name).setLevel(logging.ERROR)
    production,origin=config.RUNTIME,config.ORIGIN
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(production/'playwright'))
    output=production/'illustration-acceptance.json';artifacts=production/'illustration-acceptance-artifacts';artifacts.mkdir(mode=0o700,exist_ok=True)
    report={'status':'running','cases':[]};key=config.keychain_key()
    def save():output.write_text(json.dumps(report,indent=2));output.chmod(0o600)
    save()
    with tempfile.TemporaryDirectory(prefix='aster-illustration-acceptance-') as temporary:
        config.RUNTIME=Path(temporary);db=Database(config.RUNTIME/'app.sqlite');db.seed();db.execute('UPDATE users SET must_change=0')
        user=db.one("SELECT * FROM users WHERE username='user01'");uid=user['id']
        passwords=dict(line.split() for line in (config.RUNTIME/'initial-accounts.txt').read_text().splitlines())
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();config.ORIGIN='https://aster.localhost:'+str(sock.getsockname()[1])
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'checkpoints.sqlite')) as saver:
            engine=Engine(db,NoBrowser(),saver,key);service=Service(db,engine)
            server=uvicorn.Server(uvicorn.Config(create_app(db,service),ssl_certfile=str(production/'tls/server.pem'),ssl_keyfile=str(production/'tls/server.key'),log_level='error',access_log=False))
            running=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:await asyncio.sleep(.02)
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                    page=await browser.new_page(viewport={'width':1100,'height':950});await page.goto(config.ORIGIN)
                    await page.locator('#username').fill(user['username']);await page.locator('#password').fill(passwords[user['username']]);await page.locator('#password').press('Enter')
                    await page.locator('#composer').wait_for(state='visible')
                    sid=db.new_session(uid);await page.evaluate('sid=>openSession(sid)',sid)
                    for name,prompt in [('story','Write a short two-paragraph story with one actual watercolor illustration. Pip is a small white fox wearing a blue scarf. He helps one red bird beside a moonlit lake. Keep those details consistent in the text and image.'),
                                        ('prettier','Make the illustration prettier, with richer watercolor detail and softer moonlight. Keep the same white fox, blue scarf, red bird and setting.')]:
                        entry={'case':name,'status':'running'};report['cases'].append(entry);save();began=time.monotonic()
                        async with page.expect_response(lambda r:r.url.endswith('/api/runs') and r.request.method=='POST') as submitted:
                            await page.locator('#input').fill(prompt);await page.locator('#input').press('Enter')
                        rid=(await (await submitted.value).json())['run_id'];entry['run_id']=rid;save()
                        await asyncio.wait_for(asyncio.shield(service.tasks[rid]),245)
                        rows=db.rows('SELECT * FROM illustrations WHERE run_id=?',(rid,))
                        entry['images']=[{k:r[k] for k in ('id','scene_text','reference_id','status','review')} for r in rows]
                        entry['seconds']=round(time.monotonic()-began,2)
                        answer=db.one("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,));entry['reply']=answer['content'] if answer else ''
                        assert rows and all(r['status']=='verified' for r in rows),entry
                        if name=='prettier':assert rows[0]['reference_id'],'Refinement did not use the prior image'
                        await page.reload();await page.locator('#composer').wait_for(state='visible');await page.evaluate('sid=>openSession(sid)',sid)
                        images=page.locator('.message.assistant').last.locator('img.generated-illustration')
                        await images.first.wait_for(state='visible')
                        await page.wait_for_function('()=>Array.from(document.querySelectorAll(".message.assistant:last-child img.generated-illustration")).some(i=>i.complete&&i.naturalWidth>0)')
                        entry['rendered_images']=await images.count();assert entry['rendered_images']==len(rows),'Missing or duplicate inline image'
                        await page.set_viewport_size({'width':390,'height':844})
                        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'Mobile overflow'
                        await page.set_viewport_size({'width':1100,'height':950});await page.screenshot(path=str(artifacts/(name+'.png')),full_page=True)
                        for row in rows:
                            artifact=db.one('SELECT path FROM artifacts WHERE id=?',(row['id'],));shutil.copyfile(artifact['path'],artifacts/(name+'-'+row['id']+'.png'))
                        entry['status']='passed';save();print(json.dumps({'case':name,'status':'passed','seconds':entry['seconds'],'images':len(rows)}),flush=True)
                    await browser.close()
                report['status']='passed';save()
            except Exception as exc:
                report['status']='failed';report['error_class']=type(exc).__name__;save();raise
            finally:
                await service.close();server.should_exit=True;await running;await engine.client.close();config.RUNTIME,config.ORIGIN=production,origin
    print(json.dumps({'status':report['status'],'report':str(output)}),flush=True)


if __name__=='__main__':asyncio.run(main())
