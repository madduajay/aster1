import asyncio
import hashlib
import json
import os
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from availability.media import details,public_url,key,decorate_items,check_item_sources,item_identity
from availability.store import Store
from availability.gateway import semantic
from personal_agent import config
from personal_agent.app import create_app
from conftest import authenticate
from test_attachments import PNG
from test_availability import service_for
from test_agent import wait_service
from personal_agent.agent import ResearchDecision
from unittest.mock import AsyncMock
import httpx


async def test_matching_public_page_photo_is_cached_once(tmp_path,monkeypatch):
    calls=[]
    async def fetch(url,maximum):
        calls.append(url)
        return (b'<meta property="og:title" content="Venue Alpha"><meta property="og:description" content="A compact French bistro."><meta property="og:image" content="https://images.example/photo.jpg">','https://venue.example') if url=='https://venue.example' else (PNG,url)
    monkeypatch.setattr('availability.media.fetch',fetch);store=Store(tmp_path/'availability.sqlite')
    value=await details(store,'dining','Venue Alpha','Paris',[{'url':'https://venue.example'}])
    assert value['image_id'] and value['description']=='A compact French bistro.'
    assert (tmp_path/'venue-photos'/value['image_id']).read_bytes()==PNG
    assert await details(store,'dining','Venue Alpha','Paris',[{'url':'https://venue.example'}])==value
    assert len(calls)==2


async def test_unrelated_page_image_is_not_used(tmp_path,monkeypatch):
    async def fetch(url,maximum):return b'<title>Best restaurants directory</title><meta property="og:image" content="https://images.example/unrelated.png">',url
    monkeypatch.setattr('availability.media.fetch',fetch)
    value=await details(Store(tmp_path/'availability.sqlite'),'dining','Venue Alpha','Paris',[{'url':'https://example.com/directory'}])
    assert value['image_id'] is None and value['description']=='Restaurant in Paris.'


async def test_decorative_metadata_uses_matching_page_photo(tmp_path,monkeypatch):
    calls=[]
    async def fetch(url,maximum):
        calls.append(url)
        page=b'<title>Secret de Paris Hotel</title><meta property="og:image" content="/fleche_bottom_home.png"><img src="/logo_footer.jpg"><img src="/chambre-orsay.jpg">'
        return (page,url) if url=='https://hotel.example/' else (PNG,url)
    monkeypatch.setattr('availability.media.fetch',fetch)
    value=await details(Store(tmp_path/'availability.sqlite'),'hotels','Secret de Paris Hotel','Paris',[{'url':'https://hotel.example/'}])
    assert value['image_source_url']=='https://hotel.example/chambre-orsay.jpg'
    assert calls==['https://hotel.example/','https://hotel.example/chambre-orsay.jpg']


async def test_deleted_products_are_rejected_without_treating_bot_blocks_as_missing_inventory(monkeypatch):
    urls=['https://www.target.com/p/-/A-11111111','https://www.target.com/p/-/A-22222222']
    async def fetch(url,maximum):
        status=404 if url==urls[0] else 403
        response=httpx.Response(status,request=httpx.Request('GET',url));response.raise_for_status()
    monkeypatch.setattr('availability.media.fetch',fetch)
    good,bad=await check_item_sources([{'url':url} for url in urls])
    assert bad==urls[:1] and good==[{'url':urls[1]}]
    assert item_identity(urls[0])==item_identity('https://www.target.com/p/some-name/-/A-11111111?utm_source=test')


async def test_jsonld_photo_fallback_is_tied_to_the_named_item(tmp_path,monkeypatch):
    page=b'<title>Store</title><script type="application/ld+json">{"@type":"Product","name":"Linen Shirt Alpha","description":"A linen shirt.","image":["https://images.example/shirt.png"]}</script>'
    async def fetch(url,maximum):return (page,url) if url=='https://store.example/item' else (PNG,url)
    monkeypatch.setattr('availability.media.fetch',fetch)
    value=await details(Store(tmp_path/'availability.sqlite'),'items','Linen Shirt Alpha','',[{'url':'https://store.example/item'}])
    assert value['image_id'] and value['description']=='A linen shirt.'


@pytest.mark.parametrize('url',['http://example.com/image','https://user:password@example.com/image','https://127.0.0.1/image','https://private.example/image'])
async def test_private_or_unsafe_photo_sources_are_rejected(monkeypatch,url):
    monkeypatch.setattr('availability.media.socket.getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError):await public_url(url)


async def test_product_preview_keeps_clickable_name_first_and_only_verified_links(tmp_path,monkeypatch):
    url='https://www.target.com/p/linen-shirt/-/A-12345678'
    async def fetch(address,maximum):
        return (b'<title>Target Linen Shirt</title><meta property="og:image" content="https://images.example/shirt.png">',address) if address==url else (PNG,address)
    monkeypatch.setattr('availability.media.fetch',fetch)
    text='- [Target Linen Shirt]('+url+') — A relaxed linen shirt.\n- [Unknown shirt](https://unknown.example/item) — Not sourced.'
    value=await decorate_items(Store(tmp_path/'availability.sqlite'),text,[{'url':url}])
    assert '![Item photo](/api/venue-photos/' in value and '[Target Linen Shirt]('+url+') — A relaxed linen shirt.' in value
    assert value.count('/api/venue-photos/')==1 and 'Unknown shirt' in value
    cached=Store(tmp_path/'availability.sqlite').one('SELECT value FROM venue_metadata')
    assert 'A relaxed linen shirt' not in cached['value']


async def test_shopping_retries_category_sources_before_recommending_items(database,users):
    uid=users[0]['id'];sid=database.new_session(uid)
    category='https://www.target.com/c/linen-shirts';urls=['https://www.target.com/p/shirt-a/-/A-12345678','https://www.target.com/p/shirt-b/-/A-23456789']
    def provider(text,links):return SimpleNamespace(output_text=text,output=[SimpleNamespace(type='message',content=[SimpleNamespace(annotations=[SimpleNamespace(type='url_citation',url=url,title='Target shirts') for url in links])])])
    search=AsyncMock(side_effect=[provider('Target linen shirt category.',[category]),provider('Shirt Alpha and Shirt Beta.',urls)])
    tool=lambda call:AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query':'Find specific Target linen shirt product pages','official_domains':['target.com']},'id':call,'type':'tool_call'}])
    service=service_for(database,[tool('first'),tool('second'),AIMessage(content='- [Shirt Alpha]('+urls[0]+') — A linen shirt.\n- [Shirt Beta]('+urls[1]+') — A second linen shirt.')])
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=AsyncMock(return_value=ResearchDecision(requires_research=True,shopping_recommendations=True))))
    service.engine.client=SimpleNamespace(responses=SimpleNamespace(create=search))
    try:
        rid=(await service.submit(uid,sid,'Show me two linen shirts from Target'))['run_id'];await wait_service(service,rid)
        text=database.one("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))['content']
        assert search.await_count==2 and category not in text and text.count('/api/venue-photos/')==2
        assert service.engine.model.choices[:2]==[{'type':'function','function':{'name':'web_search'}}]*2
    finally:await service.close()


def test_photo_enrichment_does_not_trigger_availability_change():
    plain={'status':'completed','available':True,'options':[{'name':'Venue Alpha','times':['19:00']} ]}
    rich={**plain,'options':[{**plain['options'][0],'photo_url':'/api/venue-photos/'+'a'*64,'description':'A French bistro.'}]}
    assert semantic(plain)==semantic(rich)


def test_cached_photo_endpoint_requires_login_and_serves_only_registered_images(database,users,credentials):
    service=service_for(database,[AIMessage(content='Hi')]);venue_key='a'*64;image_id=hashlib.sha256(PNG).hexdigest()
    directory=config.RUNTIME/'venue-photos';directory.mkdir();(directory/image_id).write_bytes(PNG)
    service.availability.store.execute('INSERT INTO venue_metadata VALUES(?,?,?)',(venue_key,json.dumps({'image_id':image_id,'media':'image/png'}),0))
    with TestClient(create_app(database,service),base_url=config.ORIGIN,headers={'origin':config.ORIGIN}) as client:
        assert client.get('/api/venue-photos/'+venue_key).status_code==401
        authenticate(client,credentials)
        response=client.get('/api/venue-photos/'+venue_key)
        assert response.status_code==200 and response.content==PNG
        assert response.headers['content-type']=='image/png'
        assert client.get('/api/venue-photos/'+'b'*64).status_code==404
        assert client.get('/api/venue-photos/not-a-key').status_code==404


@pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Enable real Chromium preview layout check')
async def test_compact_photo_rows_links_fallback_and_reload(database,users,credentials,monkeypatch):
    import uvicorn
    from playwright.async_api import async_playwright,expect
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH',str(config.ROOT/'.runtime/playwright'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();monkeypatch.setattr(config,'ORIGIN',f'https://aster.localhost:{sock.getsockname()[1]}')
    service=service_for(database,[AIMessage(content='Hi')]);uid=users[0]['id'];database.execute('UPDATE users SET must_change=0')
    sid=database.new_session(uid);image_id=hashlib.sha256(PNG).hexdigest();directory=config.RUNTIME/'venue-photos';directory.mkdir();(directory/image_id).write_bytes(PNG)
    venue_key='a'*64;path='/api/venue-photos/'+venue_key
    service.availability.store.execute('INSERT INTO venue_metadata VALUES(?,?,?)',(venue_key,json.dumps({'image_id':image_id,'media':'image/png'}),0))
    content='| Hotel | Stay | Room |\n| --- | --- | --- |\n| ![Venue photo]('+path+') [Hotel Alpha](https://example.com/hotel) — A short hotel description. | Dec 3–21 | Deluxe |\n\n- ![Item photo]('+path+') [Target Linen Shirt — Goodfellow](https://www.target.com/p/-/A-12345678) — A relaxed linen shirt.\n- ![Item photo](/api/venue-photos/'+'b'*64+') [Other item](https://example.com/item) — A short description.'
    content+='\n\n- **Breakfast:** ![Venue photo]('+path+') [Stomping Grounds](https://example.com/breakfast)\n- ![Venue photo]\\('+path+'\\) [Escaped venue](https://example.com/escaped) — A short description.\n- ![External photo]\\(https://example.com/external.jpg\\)'
    database.message(uid,sid,'assistant',content)
    server=uvicorn.Server(uvicorn.Config(create_app(database,service),ssl_certfile=str(config.ROOT/'.runtime/tls/server.pem'),ssl_keyfile=str(config.ROOT/'.runtime/tls/server.key'),log_level='error',access_log=False));running=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:await asyncio.sleep(.01)
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True,chromium_sandbox=True);page=await browser.new_page(viewport={'width':1100,'height':900})
            await page.goto(config.ORIGIN);await page.locator('#username').fill(users[0]['username']);await page.locator('#password').fill(credentials[users[0]['username']]);await page.locator('#password').press('Enter')
            for reload in [False,True]:
                if reload:await page.reload()
                await expect(page.locator('.venue-preview')).to_have_count(4)
                await expect(page.locator('.venue-photo-placeholder')).to_have_count(1)
                await expect(page.locator('img.venue-photo')).to_have_count(4)
                await expect(page.locator('img[src="https://example.com/external.jpg"]')).to_have_count(0)
                assert await page.locator('.venue-photo').first.evaluate('(img)=>img.complete && img.naturalWidth>0 && img.clientWidth<=88')
                assert await page.get_by_role('link',name='Target Linen Shirt — Goodfellow',exact=True).get_attribute('href')=='https://www.target.com/p/-/A-12345678'
                await expect(page.locator('.venue-description').first).to_have_text('A short hotel description.')
            await page.set_viewport_size({'width':390,'height':844})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(config.ROOT/'.runtime/compact-venue-previews.png'),full_page=True);await browser.close()
    finally:server.should_exit=True;await running;await service.close();await service.engine.client.close()
