"""Independent Company inventory and durable signed-webhook delivery."""
import asyncio
import json
import os
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date,timedelta

import httpx
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from .contracts import AGENT_PORTS,COMPANY_PORTS,criteria,digest,role_for,identity
from .security import Security
from .store import Store


def normalized(value):
    return re.sub(r'[^a-z0-9]+',' ',unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower()).strip()


def exception_delay(domain,c):
    city=normalized(c.get('location') or c.get('destination',''))
    ny=any(x in city.split() for x in ('nyc','manhattan')) or 'new york' in city or city=='ny'
    if domain=='dining' and 'carbone' in normalized(c.get('venue','')).split() and ny:return 10
    hotel=identity('hotels',c).get('hotel','') if domain=='hotels' else ''
    if domain=='hotels' and hotel=='fifth avenue hotel' and (ny or not city):return 20
    if domain=='flights' and normalized(c.get('carrier','')) in {'emirates','ek','emirates airlines','emirates airline'} and normalized(c.get('cabin','')) in {'first','first class','first class cabin'} and (city in {'dxb','dubai','dubai uae'} or city.startswith('dubai ')):return 30
    return 0


def options(domain,c):
    if domain=='dining':
        from .trips import MEALS
        names=[c['venue']] if c.get('venue') else (['Peacock Indian Bar & Grill','Tamarind'] if 'indian' in normalized(c.get('cuisine','')) else ['L’Artusi','Via Carota'])
        slots=list(MEALS.get(c.get('meal'),('19:00','19:30')))
        if c.get('preferred_time'):slots=[c['preferred_time']]
        return [{'name':name,'location':c['location'],'date':c['date'],'party_size':c['party_size'],'meal':c.get('meal','dinner'),'times':slots} for name in names]
    if domain=='hotels':
        nightly=225 if c.get('stars',4)==4 else 350
        nights=(date.fromisoformat(c['check_out'])-date.fromisoformat(c['check_in'])).days
        return [{'name':c.get('hotel') or c['destination']+' Hotel','destination':c.get('destination','New York' if exception_delay(domain,c) else ''),'room_type':'Deluxe room','check_in':c['check_in'],'check_out':c['check_out'],'guests':c['guests'],'rooms':c['rooms'],'stars':c.get('stars'),'nightly_rate':nightly,'total_price':nightly*nights*c['rooms'],'currency':'USD'}]
    from .routes import schedule,connections,airline
    if not airline(c.get('carrier','Emirates')):raise ValueError('Invalid operating carrier')
    option={'carrier':c.get('carrier') or 'Emirates','flight_number':'EK202' if normalized(c.get('carrier','emirates')) in {'emirates','ek'} else 'FL101','origin':c['origin'],'destination':c['destination'],'cabin':c['cabin'],'passengers':c['passengers']}
    option['connections']=connections(c.get('connections',[]));option.update(schedule({**c,'connections':option['connections']}))
    option['fare_per_person']=4500 if 'business' in c['cabin'].casefold() else (12500 if 'first' in c['cabin'].casefold() else 1200)
    option['total_price']=option['fare_per_person']*c['passengers'];option['currency']='USD'
    if c.get('return_date'):option.update(return_origin=c['destination'],return_destination=c['origin'])
    return [option]


class Stock:
    def __init__(self,path,domain,security,clock=time.time):
        self.store=Store(path);self.domain=domain;self.security=security;self.clock=clock
        self.store.execute('CREATE TABLE IF NOT EXISTS inventory(id TEXT PRIMARY KEY,criteria TEXT,result TEXT,due REAL,delivered INTEGER DEFAULT 0,cancelled INTEGER DEFAULT 0)')
        self.reconcile_inventory()

    def reconcile_inventory(self):
        """Repair missed initial exceptions without resetting opened inventory."""
        for row in self.store.rows('SELECT id,criteria,result FROM inventory'):
            result=json.loads(row['result'])
            if result.get('available') and not result.get('revision',0) and exception_delay(self.domain,json.loads(row['criteria'])):
                result.update(available=False,options=[],checked_at=self.clock())
                self.store.execute('UPDATE inventory SET result=?,due=NULL,delivered=0 WHERE id=?',(json.dumps(result),row['id']))

    def search(self,query_id,raw):
        c,missing=criteria(self.domain,raw)
        if missing:raise ValueError('Missing: '+', '.join(missing))
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM inventory WHERE id=?',(query_id,)).fetchone()
            if old and identity(self.domain,json.loads(old['criteria']))!=identity(self.domain,c):raise ValueError('Query criteria mismatch')
            result=json.loads(old['result']) if old else {'query_id':query_id,'domain':self.domain,'criteria':c,'available':not exception_delay(self.domain,c),'revision':0}
            if not result.get('revision',0) and exception_delay(self.domain,c):result['available']=False
            metadata={**(json.loads(old['criteria']) if old else {}),**c}
            result.update(criteria=c,status='completed',checked_at=self.clock(),provenance='Company '+self.domain,options=options(self.domain,metadata) if result['available'] else [])
            db.execute('INSERT INTO inventory(id,criteria,result) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result',(query_id,json.dumps(c),json.dumps(result)))
        return result

    def watch(self,query_id,raw):
        result=self.search(query_id,raw)
        if not result['available']:
            self.store.execute('UPDATE inventory SET due=COALESCE(due,?),cancelled=0 WHERE id=?',(self.clock()+exception_delay(self.domain,result['criteria']),query_id))
        return {**result,'watching':not result['available']}

    def cancel(self,query_id):
        self.store.execute('UPDATE inventory SET cancelled=1 WHERE id=?',(query_id,));return {'cancelled':True}

    async def deliver(self,transport=None):
        for row in self.store.rows('SELECT * FROM inventory WHERE due<=? AND delivered=0 AND cancelled=0',(self.clock(),)):
            result=json.loads(row['result']);c=json.loads(row['criteria'])
            result.update(available=True,revision=1,options=options(self.domain,c),checked_at=self.clock())
            # Persist the transition before delivery; retries and polling see the opened inventory.
            self.store.execute('UPDATE inventory SET result=? WHERE id=?',(json.dumps(result),row['id']))
            event={**result,'criteria_hash':digest(identity(self.domain,c)),'event_id':'opening:'+row['id']}
            body=json.dumps(event,separators=(',',':')).encode()
            url=f'http://127.0.0.1:{AGENT_PORTS[role_for(self.domain)]}/webhooks/company/{self.domain}'
            try:
                async with httpx.AsyncClient(timeout=5,transport=transport) as client:
                    response=await client.post(url,content=body,headers={**self.security.sign(self.domain,body),'Content-Type':'application/json'})
                    response.raise_for_status()
            except (httpx.HTTPError,OSError):continue
            self.store.execute('UPDATE inventory SET delivered=1 WHERE id=?',(row['id'],))


def add_tools(mcp,stock):
    domain=stock.domain
    def search(query_id:str,criteria:dict)->dict:return stock.search(query_id,criteria)
    def watch(query_id:str,criteria:dict)->dict:return stock.watch(query_id,criteria)
    def cancel(query_id:str)->dict:return stock.cancel(query_id)
    mcp.add_tool(search,name='search_'+domain,description='Search current '+domain+' availability. Required criteria must be supplied.')
    mcp.add_tool(watch,name='watch_'+domain,description='Watch an unavailable logical query for an opening. Internal workflow use only.')
    mcp.add_tool(cancel,name='cancel_'+domain,description='Cancel an availability watch. Internal workflow use only.')


def create_company(domain,runtime=None):
    runtime=Path(runtime or os.environ.get('PERSONAL_AGENT_RUNTIME',Path(__file__).resolve().parent.parent/'.runtime'))
    security=Security(runtime);stock=Stock(runtime/'company'/f'{domain}-inventory.sqlite',domain,security)
    mcp=FastMCP('Company '+domain,stateless_http=True,json_response=True,streamable_http_path='/');add_tools(mcp,stock)
    async def deliver():
        while True:
            await stock.deliver();await asyncio.sleep(.2)
    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            task=asyncio.create_task(deliver())
            try:yield
            finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    @app.middleware('http')
    async def auth(request:Request,call_next):
        if request.url.path!='/health' and not security.authorize(request.headers):return JSONResponse({'detail':'Unauthorized'},401)
        return await call_next(request)
    @app.get('/health')
    def health():return {'service':'company-'+domain}
    app.mount('/mcp',mcp.streamable_http_app());app.state.stock=stock
    return app


if __name__=='__main__':
    import sys
    import uvicorn
    domain=sys.argv[1]
    if domain not in COMPANY_PORTS:raise SystemExit('Unknown Company domain')
    uvicorn.run(create_company(domain),host='127.0.0.1',port=COMPANY_PORTS[domain],access_log=False,log_level='warning')
