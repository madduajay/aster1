import json,time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from personal_agent.agent import Engine,ResearchDecision
from personal_agent.browser_tasks import BrowserTasks
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel,wait_service


def call(name,args,ident):return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':ident,'type':'tool_call'}])


@pytest.mark.parametrize('case',['details','listing_then_details','blocked_then_search'])
@pytest.mark.parametrize('retailer',['this retailer','https://retail.example'])
async def test_named_retailer_reads_product_pages_before_search(database,users,tmp_path,case,retailer):
    uid=users[0]['id'];sid=database.new_session(uid);order=[]
    items=[{'url':'https://retail.example/p/white-shirt-alpha','title':'White shirt Alpha'},
           {'url':'https://retail.example/p/white-shirt-beta','title':'White shirt Beta'}]
    class Reader(BrowserTasks):
        async def delegate(self,user,run,call_id,task,domains,inspect_only=False):
            order.append('browser');assert inspect_only and domains==['retail.example']
            assert 'distinct matching product-detail pages' in task and '2' in task
            sufficient=case=='details' or case=='listing_then_details' and order.count('browser')==2
            result={'status':'verified' if sufficient else ('blocked' if case=='blocked_then_search' else 'unverified'),'verification':{'success':sufficient},
                    'observed_pages':items if sufficient else [{'url':'https://retail.example/c/white-shirts','title':'White shirts'}],
                    'browser_followup':{'links':[s['url'] for s in items] if case=='listing_then_details' and not sufficient else [],'missing_information':['Two matching product-detail pages.'] if not sufficient else []}}
            database.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created,result) VALUES(?,?,?,?,?,?,1,'completed',?,?)",('task-'+str(len(order)),user,run,call_id,task,json.dumps(domains),time.time(),json.dumps(result)))
            database.event(run,'sources',items if sufficient else [{'url':'https://retail.example/c/white-shirts','title':'White shirts'}])
            return result
    manager=Reader(database,SimpleNamespace(),'sk-test',None)
    class Responses:
        async def create(self,**kwargs):
            order.append('search');assert case=='blocked_then_search'
            return SimpleNamespace(output_text='Two white shirts.',output=[SimpleNamespace(type='message',content=[SimpleNamespace(annotations=[SimpleNamespace(type='url_citation',**s) for s in items])])])
    responses=[call('browser_task',{'task':'Open https://retail.example/c/white-shirts','official_domains':['retail.example']},'first')]
    if case=='listing_then_details':responses.append(call('browser_task',{'task':'Read matching product links','official_domains':['retail.example']},'details'))
    if case=='blocked_then_search':responses.append(call('web_search',{'query':'retailer two white shirts','official_domains':['retail.example']},'search'))
    responses.append(AIMessage(content='- [White shirt Alpha](https://retail.example/p/white-shirt-alpha)\n- [White shirt Beta](https://retail.example/p/white-shirt-beta)'))
    model=ScriptedModel(responses=responses)
    router=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=AsyncMock(return_value=ResearchDecision(requires_research=True,browser_navigation=True,shopping_recommendations=True,shopping_browser_first=True))))
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'shopping.sqlite')) as saver:
        engine=Engine(database,SimpleNamespace(),saver,'sk-test',model=model,research_model=router,client=SimpleNamespace(responses=Responses()),browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        rid=(await service.submit(uid,sid,'Show me two white shirts from '+retailer))['run_id'];await wait_service(service,rid)
        assert database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='completed'
        assert order=={'details':['browser'],'listing_then_details':['browser','browser'],'blocked_then_search':['browser','search']}[case]
        assert model.choices[0]=={'type':'function','function':{'name':'browser_task'}}
        sources=database.one("SELECT payload FROM events WHERE run_id=? AND kind='item_detail_sources' ORDER BY id DESC LIMIT 1",(rid,))
        assert sources and len(json.loads(sources['payload']))==2
        answer=database.one("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))['content']
        assert 'couldn’t confirm' not in answer and 'White shirt Alpha' in answer


async def test_recreated_graph_keeps_shopping_browser_first_after_manual_pause(database,users,tmp_path):
    from langgraph.types import interrupt,Command
    from langchain_core.messages import HumanMessage
    uid=users[0]['id'];sid=database.new_session(uid);mid=database.message(uid,sid,'user','Show me two white shirts from this retailer')
    rid='shopping-resume';database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(rid,uid,sid,time.time()))
    items=[{'url':'https://retail.example/p/alpha','title':'White shirt Alpha'},{'url':'https://retail.example/p/beta','title':'White shirt Beta'}]
    class Reader(BrowserTasks):
        async def delegate(self,user,run,call_id,task,domains,inspect_only=False):
            assert inspect_only and 'distinct matching product-detail pages' in task
            interrupt('Complete the manual step')
            database.event(run,'sources',items)
            return {'status':'verified','verification':{'success':True},'observed_pages':items}
    model=ScriptedModel(responses=[call('browser_task',{'task':'Open https://retail.example/','official_domains':['retail.example']},'read'),AIMessage(content='Two white shirts are available to inspect.')])
    router=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=AsyncMock(return_value=ResearchDecision(requires_research=True,shopping_recommendations=True,shopping_browser_first=True))))
    context={'session_id':sid,'current_user_message':{'id':mid,'content':'Show me two white shirts from this retailer'}}
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'resume.sqlite')) as saver:
        manager=Reader(database,SimpleNamespace(),'sk-test',None)
        engine=Engine(database,SimpleNamespace(),saver,'sk-test',model=model,research_model=router,browser_tasks=manager)
        cfg={'configurable':{'thread_id':'shopping-resume'}}
        first=engine.graph(uid,rid,context)
        state=await first.ainvoke({'messages':[HumanMessage(content=json.dumps(context))]},cfg)
        assert state.get('__interrupt__')
        recreated=engine.graph(uid,rid,context)
        state=await recreated.ainvoke(Command(resume='completed'),cfg)
        assert not state.get('__interrupt__')
        assert len(database.rows("SELECT id FROM events WHERE run_id=? AND kind='browser_context_attempt'",(rid,)))==1
        assert database.one("SELECT id FROM events WHERE run_id=? AND kind='item_detail_sources'",(rid,))
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='browser_research_fallback'",(rid,))
        await engine.client.close()
