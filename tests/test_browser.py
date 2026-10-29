"""Chromium profile hosting tests; Browser Use is the only action executor."""
import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from personal_agent import browser_host as module,config
from personal_agent.browser_host import BrowserHost,public_url


async def test_private_destinations_rejected():
    for url in ['file:///etc/passwd','http://127.0.0.1','http://localhost','http://192.168.1.1','http://[::1]','https://example.com:55468']:
        with pytest.raises((ValueError,OSError)):await public_url(url)


async def test_dedicated_profiles_cdp_and_cookie_persistence(database,users,monkeypatch):
    if os.environ.get('RUN_BROWSER_TESTS')!='1':pytest.skip('Enable real Chromium tests')
    monkeypatch.setattr(module,'public_url',AsyncMock())
    host=BrowserHost(database,headless=True)
    first,second=users[0]['id'],users[1]['id']
    try:
        c1=await host.context(first);c2=await host.context(second)
        await c1.add_cookies([{'name':'session','value':'user01-only','url':'https://example.com','expires':2000000000}])
        assert (await c1.cookies('https://example.com'))[0]['value']=='user01-only'
        assert await c2.cookies('https://example.com')==[]
        assert await host.cdp_endpoint(first)!=await host.cdp_endpoint(second)
        await host.close_user(first)
        c1=await host.context(first)
        assert (await c1.cookies('https://example.com'))[0]['value']=='user01-only'
        assert (config.RUNTIME/'browser-profiles'/first).is_dir()
        with pytest.raises(LookupError):await host.context('../../outside')
    finally:await host.close()


async def test_profile_scope_requires_user_owned_run(database,users):
    host=BrowserHost(database);sid=database.new_session(users[0]['id'])
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('r',?,?,'running',0)",(users[0]['id'],sid))
    with pytest.raises(LookupError):host.set_scope(users[1]['id'],'r',['example.com'])
    host.set_scope(users[0]['id'],'r',['example.com'])
    with pytest.raises(ValueError):host.set_scope(users[0]['id'],'r',['outside.example'])


async def test_preview_target_snapshot_and_user_isolation(database,users,monkeypatch):
    if os.environ.get('RUN_BROWSER_TESTS')!='1':pytest.skip('Enable real Chromium tests')
    import base64
    host=BrowserHost(database)
    first,second=users[0]['id'],users[1]['id']
    sid=database.new_session(first)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('view-one',?,?,'completed',0)",(first,sid))
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('view-two',?,?,'running',1)",(first,sid))
    try:
        context=await host.context(first);page=context.pages[0]
        await page.set_content('<body style="margin:0;background:red;height:100vh">First private page</body>')
        client=await context.new_cdp_session(page)
        target=(await client.send('Target.getTargetInfo'))['targetInfo']['targetId'];await client.detach()
        other=await context.new_page();await other.set_content('<body style="margin:0;background:blue;height:100vh">Second private page</body>')
        host.run_for_user[first]='view-one';host.set_view(first,'view-one',{'url':page.url,'target_id':target})
        async def pixel(frame):
            return await page.evaluate("""async data=>{const i=new Image();i.src='data:image/jpeg;base64,'+data;await i.decode();const c=document.createElement('canvas');c.width=i.width;c.height=i.height;const x=c.getContext('2d');x.drawImage(i,0,0);return Array.from(x.getImageData(10,100,1,1).data)}""",base64.b64encode(frame).decode())
        initial=await host.preview(first,'view-one');color=await pixel(initial)
        assert color[0]>240 and color[2]<20  # Must show the target tab, not the newer blue tab.
        await host.freeze_view(first,'view-one')
        client=await context.new_cdp_session(other)
        target=(await client.send('Target.getTargetInfo'))['targetInfo']['targetId'];await client.detach()
        host.run_for_user[first]='view-two';host.set_view(first,'view-two',{'url':other.url,'target_id':target})
        current=await pixel(await host.preview(first,'view-two'))
        assert current[2]>240 and current[0]<20
        assert await host.preview(first,'view-one')==initial
        with pytest.raises(LookupError):await host.preview(second,'view-one')
        await host.close_user(first)
        assert not host.view_info(first,'view-one')['available']
    finally:await host.close()
