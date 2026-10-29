"""Capped live chat → HTTPS UI → Browser Use → text approval acceptance, isolated storage."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
from urllib.parse import urlsplit

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import async_playwright,expect
import uvicorn

from personal_agent import config,browser_host as host_module,browser_tasks as task_module
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.db import Database
from personal_agent.service import Service
from personal_agent.memory import Memory
from validation.browser_fixtures import FixtureBrowsers
from memory_acceptance import BudgetModel,wait_until


class CappedEngine(Engine):
    def graph(self,user,run,context,max_calls=24,max_searches=3):return super().graph(user,run,context,max_calls=6,max_searches=0)


async def main():
    runtime,origin=config.RUNTIME,config.ORIGIN
    original,original_task=host_module.public_url,task_module.public_url
    report={'passed':False,'real_chat_model':config.MODEL,'real_browser_model':config.BROWSER_MODEL,'live_searches':0,'real_site_mutations':0}
    server=server_task=engine=host=service=None
    try:
        os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(runtime/'playwright')
        key=config.keychain_key()
        config.RUNTIME=Path(tempfile.mkdtemp(prefix='browser-use-ui-',dir=runtime));config.prepare()
        db=Database();db.seed();user=db.one("SELECT id FROM users WHERE username='user01'")['id']
        credentials=dict(line.split() for line in (config.RUNTIME/'initial-accounts.txt').read_text().splitlines())
        async def allow_fixture(url):
            if urlsplit(url).hostname!='shop.example':await original(url)
        host_module.public_url=task_module.public_url=allow_fixture
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
        config.ORIGIN=f'https://aster.localhost:{sock.getsockname()[1]}'
        host=FixtureBrowsers(db,headless=True)
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'checkpoints.sqlite')) as saver:
            engine=CappedEngine(db,host,saver,key);del key
            service=Service(db,engine);summaries=BudgetModel(engine.model);service.memory=Memory(db,summaries)
            server=uvicorn.Server(uvicorn.Config(create_app(db,service),ssl_certfile=str(runtime/'tls/server.pem'),ssl_keyfile=str(runtime/'tls/server.key'),log_level='error',access_log=False))
            server_task=asyncio.create_task(server.serve(sockets=[sock]));await wait_until(lambda:server.started)
            problems=[]
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(headless=True,chromium_sandbox=True)
                page=await browser.new_page(ignore_https_errors=False)
                page.on('pageerror',lambda error:problems.append(type(error).__name__))
                await page.goto(config.ORIGIN)
                await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01'])
                await page.locator('#password').press('Enter');await page.locator('#password-form').wait_for(state='visible')
                await page.locator('#new-password').fill(secrets.token_urlsafe(24));await page.locator('#new-password').press('Enter')
                await page.locator('#composer').wait_for(state='visible')
                assert await page.get_by_role('button').all_text_contents()==['New Chat','Logout']
                task="On the controlled official fixture https://shop.example/product select blue men's running shoes size 10, quantity 1, $80.00 and add to cart. Use Browser Use with shop.example as the domain. Stop before checkout."
                await page.locator('#input').fill(task);await page.locator('#input').press('Enter')
                await wait_until(lambda:service.active(user) and service.active(user)['status']=='approval',timeout=240)
                active=service.active(user);details=json.loads(active['approval'])
                assert details['action']['control']=='Add to Cart',details
                website=(await host.context(user)).pages[0]
                assert await website.evaluate('window.addCount')==0
                await expect(page.get_by_role('button',name='Approve',exact=True)).to_be_visible()
                assert active['approval_id'] not in await page.locator('#messages').inner_text()
                assert '"operation"' not in await page.locator('#messages').inner_text()
                await page.get_by_role('button',name='Approve',exact=True).click()
                await wait_until(lambda:not service.active(user) and not service.tasks,timeout=240)
                run=db.one('SELECT * FROM runs WHERE id=?',(active['id'],));assert run['status']=='completed',run
                result=json.loads(db.one('SELECT result FROM browser_use_tasks')['result']);assert result['status']=='verified',result
                assert await website.evaluate('window.addCount')==1 and await website.evaluate('window.purchaseCount')==0
                assert db.one("SELECT summary FROM sessions WHERE user_id=?",(user,))['summary']
                answer=db.one("SELECT content FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1")['content']
                assert 'Verified browser result:' in answer
                await expect(page.locator('#messages')).to_contain_text('Verified browser result:')
                assert not problems and not await page.locator('#error').inner_text()
                await page.screenshot(path=str(runtime/'browser-use-ui.png'))
                await page.get_by_role('button',name='Logout',exact=True).click();await page.locator('#login-form').wait_for(state='visible')
                assert (await page.goto(config.ORIGIN+'/api/me')).status==401
                await browser.close()
                report.update(passed=True,https_trust=True,approval_button=True,cart_added_once=True,no_checkout=True,summary_saved=True,
                              buttons=['New Chat','Logout'],browser_errors=problems,main_calls=db.one('SELECT model_calls FROM run_usage')['model_calls'],
                              browser_calls=db.one('SELECT model_calls FROM browser_use_tasks')['model_calls'],summary_calls=summaries.calls)
            await service.close();await engine.browser_tasks.close();await host.close();await engine.client.close()
    except Exception as exc:
        report.update(error_class=type(exc).__name__,error_detail=str(exc).splitlines()[0][:200])
    finally:
        if service:await service.close()
        if engine:await engine.browser_tasks.close();await engine.client.close()
        if host:await host.close()
        if server:server.should_exit=True
        if server_task:await server_task
        config.RUNTIME,config.ORIGIN=runtime,origin;host_module.public_url=original;task_module.public_url=original_task
        (runtime/'browser-use-ui-acceptance.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
