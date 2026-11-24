import os

import pytest

from personal_agent import config
from test_attachments import PNG


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real image decode and browser event dispatch')
@pytest.mark.parametrize('fit_only',[True,False])
async def test_live_frame_restores_hidden_preview_and_preserves_load_handler(fit_only):
    from playwright.async_api import async_playwright,expect
    binary=config.ROOT/'.runtime/playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell'
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(executable_path=str(binary),headless=True)
        try:
            page=await browser.new_page()
            await page.route('https://frames.fixture.test/**',lambda r:r.fulfill(content_type='text/html',body='<div style="width:640px;height:480px"><img id="frame" hidden style="visibility:hidden"></div>'))
            await page.goto('https://frames.fixture.test/')
            await page.add_script_tag(content=(config.ROOT/'static/browser-control.js').read_text())
            await page.evaluate('''fitOnly=>{
                window.WebSocket=class {constructor(){window.channel=this;} close(){}};
                const image=document.querySelector('#frame');
                window.previewLoads=0;window.previewHandler=()=>window.previewLoads++;
                image.onload=window.previewHandler;
                window.view=new BrowserControlView('fixture','input',image,()=>{},()=>{});
                window.connected=view.connect();
                channel.onmessage({data:JSON.stringify({type:'ready',width:640,height:480,fit_only:fitOnly})});
            }''',fit_only)
            await page.evaluate('''bytes=>channel.onmessage({data:new Blob([new Uint8Array(bytes)],{type:'image/png'})})''',list(PNG))
            await expect(page.locator('#frame')).to_be_visible()
            assert await page.evaluate("document.querySelector('#frame').style.visibility")=='visible'
            assert await page.evaluate('window.previewLoads')==1
            assert await page.evaluate("document.querySelector('#frame').onload===previewHandler")
            if fit_only:assert await page.evaluate('view.frameReady')
            await page.evaluate('view.close()')
            assert await page.evaluate("document.querySelector('#frame').onload===previewHandler")
            previous=await page.locator('#frame').get_attribute('src')
            await page.evaluate('''bytes=>channel.onmessage({data:new Blob([new Uint8Array(bytes)],{type:'image/png'})})''',list(PNG))
            assert await page.locator('#frame').get_attribute('src')==previous
        finally:await browser.close()
