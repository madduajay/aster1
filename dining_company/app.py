"""Dining inventory and a persistent event outbox, independent of Aster storage."""
import asyncio
import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel,Field
from temporalio.client import Client
from availability.company import Stock
from availability.security import Security
from fastapi import Request
from fastapi.responses import JSONResponse


class DiningQuery(BaseModel):
    request_id:str=Field(pattern=r'^aster-dining-[a-f0-9]{32}$')
    venue:str=Field(min_length=2,max_length=100)
    location:str=Field(min_length=2,max_length=80)
    date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    party_size:int=Field(ge=1,le=20)


class Inventory:
    def __init__(self,path,clock=time.time):
        self.path,self.clock=Path(path),clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests(
                id TEXT PRIMARY KEY,criteria TEXT NOT NULL,response TEXT NOT NULL,
                due REAL,sent REAL,last_error TEXT)''')
        self.path.chmod(0o600)

    def connect(self):return sqlite3.connect(self.path,timeout=10)

    def search(self,query:DiningQuery):
        criteria=query.model_dump();encoded=json.dumps(criteria,sort_keys=True)
        venue=unicodedata.normalize('NFKC',query.venue).casefold()
        carbone=bool(re.search(r'\bcarbone\b',venue))
        result={**criteria,'available':not carbone,'slots':[] if carbone else ['18:30','19:00','19:30']}
        with self.connect() as db:
            old=db.execute('SELECT criteria,response FROM requests WHERE id=?',(query.request_id,)).fetchone()
            if old:
                if old[0]!=encoded:raise ValueError('Request ID already belongs to different criteria')
                # Repair cached availability from the former exact-name rule.
                if carbone:
                    db.execute('UPDATE requests SET response=? WHERE id=?',(json.dumps(result),query.request_id))
                    return result
                return json.loads(old[1])
            db.execute('INSERT INTO requests VALUES(?,?,?,?,NULL,NULL)',
                (query.request_id,encoded,json.dumps(result),None))
        return result

    def subscribe(self,query:DiningQuery):
        result=self.search(query)
        if result['available']:return {**result,'watching':False}
        with self.connect() as db:
            db.execute('UPDATE requests SET due=COALESCE(due,?) WHERE id=?',(self.clock()+10,query.request_id))
        return {**result,'watching':True}

    async def deliver(self,client):
        with self.connect() as db:
            rows=db.execute('SELECT id,criteria FROM requests WHERE due<=? AND sent IS NULL',(self.clock(),)).fetchall()
        for request_id,encoded in rows:
            event={**json.loads(encoded),'event_id':'opening:'+request_id,'available':True,'slots':['19:00']}
            try:
                await client.get_workflow_handle(request_id).signal('availability_opened',event)
            except Exception as exc:
                with self.connect() as db:db.execute('UPDATE requests SET last_error=? WHERE id=?',(type(exc).__name__,request_id))
            else:
                with self.connect() as db:db.execute('UPDATE requests SET sent=?,last_error=NULL WHERE id=?',(self.clock(),request_id))


def create_company(path=None,client=None):
    runtime=Path(os.environ.get('DINING_COMPANY_RUNTIME',Path(os.environ.get('PERSONAL_AGENT_RUNTIME',Path(__file__).resolve().parent.parent/'.runtime'))/'company'))
    inventory=Inventory(path or runtime/'company.sqlite')
    security=Security(Path(path).parent if path else runtime.parent)
    stock=Stock(runtime/'dining-inventory.sqlite','dining',security)
    mcp=FastMCP('Company Dining',stateless_http=True,json_response=True,streamable_http_path='/')

    @mcp.tool()
    def search_dining(request_id:str='',venue:str='',location:str='',date:str='',party_size:int=0,query_id:str='',criteria:dict|None=None)->dict:
        """Check dining slots for a venue, city, date and party size. This does not register a watch."""
        if criteria is not None:return stock.search(query_id,criteria)
        return inventory.search(DiningQuery(request_id=request_id,venue=venue,location=location,date=date,party_size=party_size))

    @mcp.tool()
    def watch_dining(request_id:str='',venue:str='',location:str='',date:str='',party_size:int=0,query_id:str='',criteria:dict|None=None)->dict:
        """Register an availability watch for the correlated Temporal workflow."""
        if criteria is not None:return stock.watch(query_id,criteria)
        return inventory.subscribe(DiningQuery(request_id=request_id,venue=venue,location=location,date=date,party_size=party_size))

    @mcp.tool()
    def cancel_dining(query_id:str)->dict:return stock.cancel(query_id)

    async def publish(temporal):
        while True:
            try:await inventory.deliver(temporal)
            except sqlite3.OperationalError:pass
            await asyncio.sleep(.2)

    async def publish_stock():
        while True:
            await stock.deliver();await asyncio.sleep(.2)

    @asynccontextmanager
    async def lifespan(app):
        temporal=client or await Client.connect(os.environ.get('TEMPORAL_ADDRESS','127.0.0.1:7233'),namespace=os.environ.get('TEMPORAL_NAMESPACE','default'))
        async with mcp.session_manager.run():
            tasks=[asyncio.create_task(publish(temporal)),asyncio.create_task(publish_stock())]
            try:yield
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)

    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.inventory=inventory
    app.state.stock=stock
    @app.middleware('http')
    async def auth(request:Request,call_next):
        if request.url.path!='/health' and not security.authorize(request.headers):return JSONResponse({'detail':'Unauthorized'},401)
        return await call_next(request)
    @app.get('/health')
    def health():return {'service':'company-dining'}
    app.mount('/mcp',mcp.streamable_http_app())
    return app


def main():
    import uvicorn
    uvicorn.run(create_company(),host='127.0.0.1',port=int(os.environ.get('DINING_COMPANY_PORT','55469')),access_log=False,log_level='warning')


if __name__=='__main__':main()
