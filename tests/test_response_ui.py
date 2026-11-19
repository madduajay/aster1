"""Render streamed and saved responses without interpreting untrusted HTML."""
import os
import json

import pytest
from playwright.async_api import async_playwright, expect

from personal_agent import config


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS') != '1', reason='Enable real Chromium tests')
async def test_response_cards_lists_and_safe_formatting(monkeypatch, tmp_path):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(config.ROOT / '.runtime/playwright'))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
        page = await browser.new_page(viewport={'width': 1200, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        async def serve(route):
            path = route.request.url.split('aster.test', 1)[1]
            if path.startswith('/api/'):
                await route.fulfill(status=401, content_type='application/json', body='{}')
            else:
                file = config.ROOT / ('static/index.html' if path == '/' else path.lstrip('/'))
                await route.fulfill(path=str(file))

        await page.route('https://aster.test/**', serve)
        await page.goto('https://aster.test/')
        await expect(page.locator('#login')).to_be_visible()
        await page.evaluate("""() => {
            $('login').hidden=true; $('chat').hidden=false; $('composer').hidden=false;
            message('user','Show me a short summary.');
            window.response=message('assistant','Here are the highlights.');
            updateAnswer(response,'Here are the highlights.\\n\\n## **First option**\\n## Second option\\n\\n- Fast setup\\n- [Official site](https://example.com)\\n\\nA short closing paragraph with `code`.');
        }""")
        response = page.locator('.message.assistant')
        await expect(response.locator('li')).to_have_count(4)
        await expect(response.locator('strong')).to_have_text('First option')
        await expect(response.locator('a')).to_have_attribute('href', 'https://example.com')
        assert '##' not in await response.inner_text()
        assert await response.locator('p').first.evaluate('(p)=>getComputedStyle(p).backgroundColor') == 'rgb(255, 255, 255)'
        assert await page.locator('body').evaluate('(p)=>getComputedStyle(p).backgroundColor') == 'rgb(242, 243, 245)'
        await page.screenshot(path=str(tmp_path / 'response-desktop.png'))
        await page.set_viewport_size({'width': 390, 'height': 844})
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.screenshot(path=str(tmp_path / 'response-mobile.png'))
        await page.evaluate("""() => updateAnswer(response,'<img src=x onerror="window.injected=true">\\n\\n[Unsafe](javascript:alert(1))\\n\\n3. Third\\n4. Fourth\\n\\n```html\\n<b>literal</b>\\n```')""")
        await expect(response.locator('img,script,a')).to_have_count(0)
        await expect(response.locator('ol')).to_have_attribute('start', '3')
        await expect(response.locator('pre code')).to_have_text('<b>literal</b>')
        assert not await page.evaluate('Boolean(window.injected)')
        from personal_agent.sources import vetted_text
        hotel='https://hotel.example/stay?utm_source=openai'
        clean=vetted_text('Here are the hotels:\n- **[Test Hotel](https://hotel.example/stay)**\n- **[Other Hotel](https://other.example/stay)**',[{'url':hotel,'title':'Test Hotel'}])
        await page.evaluate('(text)=>updateAnswer(response,text)',clean)
        await expect(page.get_by_role('link',name='Test Hotel',exact=True)).to_have_attribute('href',hotel)
        await expect(page.get_by_role('link',name='Other Hotel (unverified link)',exact=True)).to_be_visible()
        await expect(response.locator('li')).to_have_count(2)
        assert not errors
        await browser.close()


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS') != '1', reason='Enable real Chromium tests')
async def test_image_stays_in_composer_until_question_is_sent(database, users, credentials, monkeypatch, tmp_path):
    import asyncio
    import socket
    import uvicorn
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from personal_agent.agent import Engine
    from personal_agent.app import create_app
    from personal_agent.memory import Memory
    from personal_agent.service import Service
    from conftest import SummaryModel
    from test_agent import ScriptedModel, ScriptedBrowser
    from test_attachments import PNG

    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(config.ROOT / '.runtime/playwright'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    model=ScriptedModel(responses=[AIMessage(content='I can see the attached image.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'draft-checkpoints.sqlite')) as saver:
        engine=Engine(database,ScriptedBrowser(),saver,'sk-test',model=model)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False))
        task=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(200):
                if server.started:break
                await asyncio.sleep(.01)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True,chromium_sandbox=True)
                page=await browser.new_page(viewport={'width':390,'height':844})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.goto(config.ORIGIN)
                await page.locator('#username').fill('user01');await page.locator('#password').fill(credentials['user01']);await page.locator('#password').press('Enter')
                await page.locator('#password-form').wait_for(state='visible')
                await page.locator('#new-password').fill('controlled-image-draft-password');await page.locator('#new-password').press('Enter')
                await expect(page.locator('#composer')).to_be_visible()
                await page.locator('#input').fill('What is in this picture?')
                files=[{'name':'sample.png','mimeType':'image/png','buffer':PNG},
                       {'name':'notes.txt','mimeType':'text/plain','buffer':b'Notes'}]
                async with page.expect_file_chooser() as chooser:
                    await page.get_by_role('button',name='Attach files',exact=True).click()
                await (await chooser.value).set_files(files)
                await expect(page.locator('#draft-attachments .draft-file')).to_have_count(2)
                await expect(page.locator('#draft-attachments img')).to_be_visible()
                await expect(page.locator('#draft-attachments img')).not_to_have_js_property('naturalWidth',0)
                assert database.one('SELECT COUNT(*) AS n FROM artifacts')['n']==0
                await expect(page.locator('.message')).to_have_count(0)
                await page.get_by_role('button',name='Remove notes.txt',exact=True).click()
                await expect(page.locator('#draft-attachments .draft-file')).to_have_count(1)
                await expect(page.locator('#input')).to_have_value('What is in this picture?')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.screenshot(path=str(config.ROOT/'.runtime/composer-image-mobile.png'))
                await page.set_viewport_size({'width':1200,'height':900})
                await page.screenshot(path=str(config.ROOT/'.runtime/composer-image-desktop.png'))
                # A failed message send retains the question and preview. Retrying reuses the staged upload.
                fail_next=True
                async def fail_once(route):
                    nonlocal fail_next
                    if fail_next:
                        fail_next=False
                        await route.fulfill(status=503,content_type='application/json',body='{"detail":"Try sending again"}')
                    else:await route.continue_()
                await page.route('**/api/runs',fail_once)
                await page.locator('#input').press('Enter')
                await expect(page.locator('#error')).to_have_text('Try sending again')
                await expect(page.locator('#draft-attachments img')).to_be_visible()
                await expect(page.locator('#input')).to_have_value('What is in this picture?')
                assert database.one('SELECT COUNT(*) AS n FROM artifacts')['n']==1
                assert database.one('SELECT COUNT(*) AS n FROM message_attachments')['n']==0
                await page.locator('#input').press('Enter')
                await expect(page.locator('.message.assistant')).to_have_text('I can see the attached image.')
                await expect(page.locator('#draft-attachments')).to_be_hidden()
                await expect(page.locator('.message.user')).to_have_count(1)
                await expect(page.locator('.message.user')).to_contain_text('What is in this picture?')
                await expect(page.locator('.message.user img')).to_be_visible()
                assert database.one('SELECT COUNT(*) AS n FROM artifacts')['n']==1
                assert database.one('SELECT COUNT(*) AS n FROM message_attachments')['n']==1
                assert any(b.get('type')=='image_url' for m in model.seen[0] if isinstance(m.content,list) for b in m.content)
                await page.reload()
                await expect(page.locator('.message.user img')).to_be_visible()
                await expect(page.locator('.message.user')).to_have_count(1)
                assert not errors
                await browser.close()
        finally:
            await service.close();await engine.client.close()
            server.should_exit=True;await task
