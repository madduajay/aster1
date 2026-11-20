import asyncio
import socket
import uuid
from types import SimpleNamespace

import uvicorn
from temporalio.testing import WorkflowEnvironment
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from dining_company.app import create_company
from personal_agent import config
from personal_agent.dining import Dining
from personal_agent.agent import Engine
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel,StubBrowsers
from test_agent import ScriptedModel,wait_service


class FakeTemporal:
    def __init__(self):self.requests={};self.workflow_types=[]
    async def start_workflow(self,run,request,**kwargs):
        self.requests[kwargs['id']]=request;self.workflow_types.append(run.__qualname__)
    def get_workflow_handle(self,wid):
        async def query(method):return {'available':False,'slots':[]}
        return SimpleNamespace(query=query)


def lookup_messages(venue='Carbone',location='New York',date='2099-12-01',party_size=2,reply=None):
    return [AIMessage(content='',tool_calls=[{'name':'search_dining','args':{
        'venue':venue,'location':location,'date':date,'party_size':party_size},'id':uuid.uuid4().hex,'type':'tool_call'}]),
        AIMessage(content=reply or f'{venue} is currently unavailable for {party_size} in {location}. I’ll let you know when a table opens up.')]


def agent_service(database,responses=None):
    model=ScriptedModel(responses=responses or lookup_messages())
    engine=Engine(database,StubBrowsers(),InMemorySaver(),'sk-test',model=model)
    service=Service(database,engine);service.memory=Memory(database,SummaryModel())
    return service


def fake_company(service):
    calls=[]
    async def call(name,query):
        calls.append((name,query))
        available=query['venue']!='Carbone'
        return {**query,'available':available,'slots':['18:30','19:00','19:30'] if available else []}
    service.dining.call_company=call;service.dining.client=FakeTemporal()
    return calls


async def submit_agent(service,user,session,text):
    result=await service.submit(user,session,text)
    await wait_service(service,result['run_id'])
    assert service.db.one('SELECT status FROM runs WHERE id=?',(result['run_id'],))['status']=='completed'
    return {**result,'reply':service.db.rows("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id",(session,))[-1]['content']}


def submit_client(client,service,sid,text):
    result=client.post('/api/runs',json={'session_id':sid,'text':text})
    assert result.status_code==200
    client.portal.call(wait_service,service,result.json()['run_id'])
    return client.get('/api/sessions/'+sid).json()['messages'][-1]['content']


async def emit_opening(service):
    request=list(service.dining.client.requests.values())[-1]
    event={**request['company_query'],'available':True,'slots':['19:00'],'event_id':'opening:'+request['company_query']['request_id']}
    await service.dining.publish_opening({'request':request,'event':event})


async def start_dining_runtime(service,path):
    env=await WorkflowEnvironment.start_local(download_dest_dir=str(config.ROOT/'.runtime/bin'),
        dev_server_database_filename=str(path/'temporal.sqlite'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
    port=sock.getsockname()[1]
    company=create_company(path/'company.sqlite',client=env.client)
    server=uvicorn.Server(uvicorn.Config(company,log_level='error',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:break
        await asyncio.sleep(.01)
    service.dining=Dining(service,client=env.client,company_url=f'http://127.0.0.1:{port}/mcp/',task_queue='dining-'+uuid.uuid4().hex)
    service.engine.dining=service.dining
    await service.dining.start()
    async def close():
        await service.dining.close();server.should_exit=True;await task;await env.shutdown()
    close.company=company;close.environment=env
    return close
