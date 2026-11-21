"""Public venue descriptions and cached photos from matching source pages."""
import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin,urlsplit

import filetype
import httpx
from .contracts import canonical,digest,leaves

LOCKS={}
SLOTS=asyncio.Semaphore(8)


def item_page(url):
    path=urlsplit(url).path.casefold()
    if '/products/' in path or '/product/' in path or '/dp/' in path or '/ip/' in path:return True
    return path not in {'','/','/s','/search'} and not re.search(r'/(?:c|s|search|category|categories|collections)(?:/|$)',path)


def item_identity(url):
    from personal_agent.sources import canonical_url
    parsed=urlsplit(url);target=re.search(r'/-/A-(\d+)',parsed.path,re.I)
    return 'target:'+target[1] if target and parsed.hostname in {'target.com','www.target.com'} else canonical_url(url)


async def check_item_sources(sources):
    """Reject confirmed dead/redirected items; bot-blocked images can fall back."""
    async def one(source):
        if not item_page(source['url']):return False
        try:
            async with SLOTS:raw,final=await fetch(source['url'],1_500_000)
            return item_page(final)
        except httpx.HTTPStatusError as exc:return exc.response.status_code not in {404,410}
        except Exception:return True
    checks=await asyncio.gather(*(one(source) for source in sources))
    return [s for s,ok in zip(sources,checks) if ok],[s['url'] for s,ok in zip(sources,checks) if item_page(s['url']) and not ok]


class Page(HTMLParser):
    def __init__(self):super().__init__();self.meta={};self.title='';self.in_title=False;self.in_json=False;self.json_parts=[];self.images=[]
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=='meta':self.meta[(values.get('property') or values.get('name') or '').lower()]=values.get('content','')
        if tag=='title':self.in_title=True
        if tag=='img':
            image=values.get('data-src') or values.get('src','')
            if re.search(r'\.(?:jpe?g|webp)(?:\?|$)',image,re.I) and not decorative(image):self.images.append(image)
        if tag=='script' and values.get('type')=='application/ld+json':self.in_json=True;self.json_parts.append('')
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
        if tag=='script':self.in_json=False
    def handle_data(self,data):
        if self.in_title:self.title+=data
        if self.in_json:self.json_parts[-1]+=data

    def entity(self,name):
        def walk(value):
            if isinstance(value,list):
                for item in value:yield from walk(item)
            elif isinstance(value,dict):
                if value.get('name') and matches(name,value['name']) and value.get('image'):yield value
                for key in ('@graph','mainEntity','itemListElement','item'):
                    if key in value:yield from walk(value[key])
        for text in self.json_parts:
            try:
                found=next(walk(json.loads(text)),None)
                if found:return found
            except (ValueError,TypeError):continue
        return {}


async def public_url(url):
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None,443}:raise ValueError('Nonpublic photo source')
    addresses=await asyncio.to_thread(socket.getaddrinfo,parsed.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):raise ValueError('Nonpublic photo source')


async def fetch(url,maximum):
    async with httpx.AsyncClient(timeout=4,trust_env=False,headers={'User-Agent':'Aster venue preview/1.0'}) as client:
        for _ in range(4):
            await public_url(url)
            async with client.stream('GET',url) as response:
                if response.is_redirect:
                    url=urljoin(url,response.headers['location']);continue
                response.raise_for_status();data=bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    html='text/html' in response.headers.get('content-type','')
                    if len(data)>maximum:
                        if html:return bytes(data[:maximum]),url
                        raise ValueError('Preview too large')
                return bytes(data),url
    raise ValueError('Too many preview redirects')


def key(domain,name,city):return digest([domain,canonical(name),canonical(city)])


def cached(store,domain,name,city):
    row=store.one('SELECT value,updated FROM venue_metadata WHERE id=?',(key(domain,name,city),))
    value=json.loads(row['value']) if row and row['updated']>time.time()-86400 else None
    return None if value and decorative(value.get('image_source_url','')) else value


def decorative(url):
    return bool(re.search(r'(?:logo|favicon|sprite|icon|picto|fleche|arrow|facebook|instagram|youtube|flag)[/_.-]',url,re.I))


def matches(name,title):
    words={w for w in re.findall(r'\w+',canonical(name)) if len(w)>=3 and w not in {'the','hotel','restaurant','paris','york','new','dining','and','bar','cafe'}}
    return bool(words) and sum(w in canonical(title) for w in words)>=min(2,len(words))


async def details(store,domain,name,city,sources,description=''):
    venue_key=key(domain,name,city);lock=LOCKS.setdefault((str(store.path),venue_key),asyncio.Lock())
    async with lock:
        old=cached(store,domain,name,city)
        if old:return old
        noun={'hotels':'Hotel','dining':'Restaurant'}.get(domain,'Item')
        result={'photo_url':'/api/venue-photos/'+venue_key,'description':description[:180] or noun+(' in '+city if city else '')+'.','image_id':None}
        for source in sources[:2]:
            try:
                async with SLOTS:
                    async with asyncio.timeout(7):
                        raw,url=await fetch(source['url'],1_500_000);page=Page();page.feed(raw.decode('utf-8',errors='ignore'))
                        entity=page.entity(name)
                        if not matches(name,page.meta.get('og:title',page.title)) and not entity:continue
                        if not description:result['description']=re.sub(r'\s+',' ',re.sub(r'<[^>]+>','',page.meta.get('og:description') or page.meta.get('description') or entity.get('description') or result['description']))[:180]
                        photo=page.meta.get('og:image') or page.meta.get('twitter:image') or entity.get('image')
                        if isinstance(photo,list):photo=photo[0] if photo else None
                        if isinstance(photo,dict):photo=photo.get('url') or photo.get('contentUrl')
                        if not photo or decorative(photo):photo=next(iter(page.images),None)
                        if not photo:continue
                        image,image_url=await fetch(urljoin(url,photo),2_000_000)
                        kind=filetype.guess(image)
                        if not kind or kind.mime not in {'image/jpeg','image/png','image/webp','image/gif'}:continue
                        image_id=hashlib.sha256(image).hexdigest();path=store.path.parent/'venue-photos'/image_id
                        path.parent.mkdir(mode=0o700,exist_ok=True);path.write_bytes(image);path.chmod(0o600)
                        result.update(image_id=image_id,media=kind.mime,image_source_url=image_url,source_url=url)
                        break
            except Exception:continue
        store.execute('INSERT INTO venue_metadata VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value,updated=excluded.updated',(venue_key,json.dumps(result),time.time()))
        return result


async def enrich(store,results):
    jobs={}
    for result in leaves(results):
        if result['domain'] not in {'hotels','dining'}:continue
        c=result['criteria'];city=c.get('destination') or c.get('location','')
        for option in result.get('options',[]):
            venue_key=key(result['domain'],option['name'],city)
            jobs.setdefault(venue_key,(result['domain'],option['name'],city,result.get('sources',[]),option.get('description') or result.get('description','')))
    async def one(item):
        try:await details(store,*item)
        except Exception:pass
    try:
        async with asyncio.timeout(15):await asyncio.gather(*(one(item) for item in jobs.values()))
    except TimeoutError:pass
    for result in leaves(results):
        if result['domain'] not in {'hotels','dining'}:continue
        c=result['criteria'];city=c.get('destination') or c.get('location','')
        for option in result.get('options',[]):
            value=cached(store,result['domain'],option['name'],city) or {}
            option.update(photo_url=value.get('photo_url','/api/venue-photos/'+key(result['domain'],option['name'],city)),description=value.get('description') or option.get('description') or ('Hotel' if result['domain']=='hotels' else 'Restaurant')+(' in '+city if city else '')+'.')


async def decorate_items(store,text,sources):
    """Add lightweight previews to source-backed recommendation rows."""
    from personal_agent.sources import MARKDOWN_LINK,canonical_url
    verified={canonical_url(s['url']):s for s in sources};rows=[];seen=set()
    lines=text.splitlines()
    for index,line in enumerate(lines):
        match=MARKDOWN_LINK.search(line)
        if not match or match[2] in seen:continue
        source=verified.get(canonical_url(match[2]));unverified='unverified link' in match[1].casefold()
        if not source and not unverified:continue
        prefix=line[:match.start()]
        if not re.fullmatch(r'\s*(?:[-*+]\s*|\d+[.)]\s*|\|\s*)?(?:\*\*)?',prefix):continue
        name=re.sub(r'\s+\((?:independent source|unverified link)\)','',match[1]);name=re.sub(r'[*`]+','',name)
        if len(name)<5:continue
        if urlsplit(match[2]).hostname in {'www.target.com','target.com'} and not urlsplit(match[2]).path.startswith('/p/'):continue
        remainder=line[match.end():];tail=''
        if line.lstrip().startswith('|') and '|' in remainder:remainder,tail=remainder.split('|',1);tail='|'+tail
        description=re.sub(r'\s+',' ',remainder.strip(' *—–:-'))[:180]
        seen.add(match[2]);rows.append((index,prefix.replace('**',''),match[0],name,description,tail,source))
    async def one(row):
        index,prefix,link,name,description,tail,source=row
        # Public metadata cache never stores personalized recommendation prose.
        value=await details(store,'items',name,'',[source]) if source else {'photo_url':'/api/venue-photos/'+key('unverified-items',name,link),'description':'Unverified item link.'}
        lines[index]=prefix+'![Item photo]('+value['photo_url']+') '+link+' — '+(description or value['description'])+(' '+tail if tail else '')
    try:
        async with asyncio.timeout(15):await asyncio.gather(*(one(row) for row in rows))
    except TimeoutError:pass
    return '\n'.join(lines)
