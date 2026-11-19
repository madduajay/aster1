import json,os
from unittest.mock import AsyncMock

import pytest

from personal_agent import config,browser_host as module
from personal_agent.browser_host import BrowserHost


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chromium requires explicit enablement')
@pytest.mark.parametrize('required',['required','aria-required="true"'])
async def test_locked_page_manual_entry_checks_required_fields_without_exposing_values(database,users,monkeypatch,required):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr(module,'public_url',AsyncMock())
    user=users[0]['id'];sid=database.new_session(user)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,approval_id,approval,created) VALUES('locked-form',?,?,'input','current',?,0)",
        (user,sid,json.dumps({'action':{'kind':'manual'}})))
    host=BrowserHost(database)
    try:
        host.set_scope(user,'locked-form',['form.fixture.test']);host.run_for_user[user]='locked-form'
        context=await host.context(user)
        async def fixture(route):await route.fulfill(content_type='text/html',body=f'''<label>First name<input id=first {required}></label><label>Last name<input id=last {required}></label><script>window.eval=()=>{{throw new Error('eval is disabled')}}</script>''')
        await context.route('https://form.fixture.test/**',fixture)
        page=context.pages[0];await page.goto('https://form.fixture.test/application')
        host.set_view(user,'locked-form',{'url':page.url})
        with pytest.raises(Exception,match='eval is disabled'):await page.evaluate('document.title')
        assert not await host.manual_ready(user,'locked-form')
        first=page.locator('#first');box=await first.bounding_box()
        await host.manual_action(user,'locked-form','current','click',x=box['x']+5,y=box['y']+5)
        assert (await host.input_focus(user,'locked-form','current'))['editable']
        await host.manual_action(user,'locked-form','current','type',text='Disposable')
        assert await first.input_value()=='Disposable'
        snapshot=await host.snapshot(user,'locked-form')
        assert 'Disposable' not in json.dumps(snapshot)
        assert not await host.manual_ready(user,'locked-form')
        second=page.locator('#last');box=await second.bounding_box()
        await host.manual_action(user,'locked-form','current','click',x=box['x']+5,y=box['y']+5)
        await host.manual_action(user,'locked-form','current','type',text='Test')
        assert await host.manual_ready(user,'locked-form')
        # The site's own eval remains disabled; no security setting was changed.
        with pytest.raises(Exception,match='eval is disabled'):await page.evaluate('document.title')
    finally:await host.close()


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Real Chromium requires explicit enablement')
async def test_generic_human_verification_keeps_manual_continue_paused(database,users,monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    monkeypatch.setattr(module,'public_url',AsyncMock())
    user=users[0]['id'];sid=database.new_session(user)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,approval_id,approval,created) VALUES('human-check',?,?,'input','current',?,0)",(user,sid,json.dumps({'action':{'kind':'manual'}})))
    host=BrowserHost(database)
    try:
        host.set_scope(user,'human-check',['verify.fixture.test']);host.run_for_user[user]='human-check'
        context=await host.context(user)
        async def fixture(route):await route.fulfill(content_type='text/html',body="<main>Quick verification. Press &amp; hold to confirm you're a human (and not a bot).</main>")
        await context.route('https://verify.fixture.test/**',fixture)
        page=context.pages[0];await page.goto('https://verify.fixture.test/')
        host.set_view(user,'human-check',{'url':page.url})
        assert not await host.manual_ready(user,'human-check')
        assert (await host.snapshot(user,'human-check'))['human_required']
    finally:await host.close()
