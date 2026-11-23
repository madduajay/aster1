"""Preview questions inspect first; abandoned browser context cannot return."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine,ResearchDecision
from personal_agent.browser_tasks import BrowserTasks
from personal_agent.browser_intent import browser_preview_run
from personal_agent.memory import Memory
from personal_agent.service import Service,PreviewRelation
from conftest import SummaryModel
from test_agent import ScriptedModel,wait_service


class Host:
    def view_info(self,*args):return {}


def seed_preview(db,uid):
    sid=db.new_session(uid)
    first=db.message(uid,sid,'user','Open the card benefits page')
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('source',?,?,'completed',?)",(uid,sid,time.time()))
    last=db.message(uid,sid,'assistant','Old browser scratch: benefits are cached here.')
    result={'status':'verified','verification':{'success':True},'url':'https://example.com/benefits','title':'Card benefits'}
    db.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created,result) VALUES('old-task',?,'source','old-call','Read card benefits','[\"example.com\"]',0,'completed',?,?)",(uid,time.time(),json.dumps(result)))
    db.execute("UPDATE sessions SET summary='Old browser scratch summary',covered=? WHERE id=?",(last,sid))
    db.execute("INSERT INTO summary_snapshots VALUES(?,?,'Old browser scratch summary')",(sid,last))
    return sid,first,last


def call(name,args,ident):return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':ident,'type':'tool_call'}])


@pytest.mark.parametrize('sufficient',[True,False])
@pytest.mark.parametrize('reply',['benefits by card please','yes pls do'])
async def test_preview_inspected_before_search_and_fallback_only_when_insufficient(database,users,tmp_path,sufficient,reply):
    uid=users[0]['id'];order=[];host=Host()
    class Reader(BrowserTasks):
        async def delegate(self,user,run,call_id,task,domains,inspect_only=False):
            order.append('browser')
            assert inspect_only and domains==['example.com']
            assert reply in task and 'existing previewed website' in task
            assert 'Model-proposed task' in task
            if reply=='yes pls do':assert 'I can help compare the room rates and cancellation terms.' in task
            result={'status':'verified' if sufficient else 'unverified','verification':{'success':sufficient,'reason':'Enough content' if sufficient else 'Missing card-specific details'},'url':'https://example.com/benefits'}
            database.execute("INSERT INTO browser_use_tasks(id,user_id,run_id,call_id,objective,domains,inspect_only,status,created,result) VALUES(?,?,?,?,?,?,1,'completed',?,?)",('fresh-'+run,user,run,call_id,task,json.dumps(domains),time.time(),json.dumps(result)))
            return result
    manager=Reader(database,host,'sk-test',None);sid,_,last=seed_preview(database,uid)
    database.execute('UPDATE messages SET content=? WHERE id=?',('I can help compare the room rates and cancellation terms.',last))
    class Responses:
        async def create(self,**kwargs):
            order.append('search')
            return SimpleNamespace(output_text='Card-specific benefits are listed.',output=[SimpleNamespace(type='message',content=[SimpleNamespace(annotations=[SimpleNamespace(type='url_citation',url='https://example.com/cards',title='Card benefits')])])])
    responses=[call('browser_task',{'task':'Model-proposed task','official_domains':['wrong.example']},'inspect')]
    if not sufficient:responses.append(call('web_search',{'query':'card benefits','official_domains':['example.com']},'fallback'))
    responses.append(AIMessage(content='Card-specific benefits are listed.'))
    model=ScriptedModel(responses=responses)
    router=SimpleNamespace(with_structured_output=lambda schema,*a,**k:SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(relevant=True) if schema.__name__=='PreviewRelation' else ResearchDecision(requires_research=True))))
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'graph.sqlite')) as saver:
        engine=Engine(database,host,saver,'sk-test',model=model,research_model=router,client=SimpleNamespace(responses=Responses()),browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        rid=(await service.submit(uid,sid,reply))['run_id'];await wait_service(service,rid)
        assert database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='completed'
        assert order==(['browser'] if sufficient else ['browser','search'])
        assert model.choices[0]=={'type':'function','function':{'name':'browser_task'}}
        if not sufficient:
            assert model.choices[1]=={'type':'function','function':{'name':'web_search'}}
            answer=database.one("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))['content']
            assert 'couldn’t confirm' not in answer and 'Card-specific' in answer
        assert bool(database.one("SELECT id FROM events WHERE run_id=? AND kind='browser_research_fallback'",(rid,))) is not sufficient


async def test_topic_change_retires_preview_and_scratch_memory_without_deleting_history(database,users,tmp_path):
    uid=users[0]['id'];host=Host();manager=BrowserTasks(database,host,'sk-test',None)
    sid,first,last=seed_preview(database,uid)
    fact,_=database.put_fact(uid,'Prefers tea',first,'preference.drink',evidence='Prefers tea')
    model=ScriptedModel(responses=[AIMessage(content='Hello!'),call('browser_task',{'task':'Open a fresh card benefits page','official_domains':['example.com']},'fresh'),AIMessage(content='Fresh benefits page opened.')])
    manager.delegate=AsyncMock(return_value={'status':'verified','verification':{'success':True}})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'graph.sqlite')) as saver:
        engine=Engine(database,host,saver,'sk-test',model=model,browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        rid=(await service.submit(uid,sid,'hello'))['run_id'];await wait_service(service,rid)
        assert browser_preview_run(database,uid,sid) is None
        assert database.one("SELECT id FROM events WHERE run_id='source' AND kind='browser_context_retired'")
        assert database.one('SELECT message_id FROM context_exclusions WHERE message_id=?',(last,))
        assert database.one('SELECT content FROM messages WHERE id=?',(last,))  # Visible history remains.
        assert any(f['id']==fact for f in database.facts(uid))
        assert 'Old browser scratch' not in str(service.memory.context(uid,sid,''))
        rid2=(await service.submit(uid,sid,'Open the card benefits website again'))['run_id'];await wait_service(service,rid2)
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='browser_context'",(rid2,))
        assert manager.delegate.await_count==1
        assert manager.delegate.call_args.args[2].startswith(rid2+':')
        assert manager.delegate.call_args.args[5] is False
        assert 'Old browser scratch' not in str(model.seen)
        await engine.client.close()


async def test_semantic_relation_handles_card_names_without_shared_preview_words(database,users,tmp_path):
    uid=users[0]['id'];host=Host();manager=BrowserTasks(database,host,'sk-test',None)
    sid,_,_=seed_preview(database,uid)
    manager.delegate=AsyncMock(return_value={'status':'verified','verification':{'success':True}})
    model=ScriptedModel(responses=[call('browser_task',{'task':'Compare the two cards','official_domains':['example.com']},'read'),AIMessage(content='The two cards differ in their benefits.')])
    class Router:
        def with_structured_output(self,schema,**kwargs):
            return SimpleNamespace(ainvoke=AsyncMock(return_value=PreviewRelation(relevant=True) if schema is PreviewRelation else ResearchDecision(requires_research=True)))
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'semantic.sqlite')) as saver:
        engine=Engine(database,host,saver,'sk-test',model=model,research_model=Router(),browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        rid=(await service.submit(uid,sid,'Compare Gold versus Platinum'))['run_id'];await wait_service(service,rid)
        assert json.loads(database.one("SELECT payload FROM events WHERE run_id=? AND kind='browser_context_relation'",(rid,))['payload'])=={'relevant':True}
        assert browser_preview_run(database,uid,sid)=='source'
        assert manager.delegate.await_count==1 and manager.delegate.call_args.args[5] is True
        assert 'Compare Gold versus Platinum' in manager.delegate.call_args.args[3]
        assert model.choices[0]=={'type':'function','function':{'name':'browser_task'}}
        await engine.client.close()


@pytest.mark.parametrize('second_sufficient',[True,False])
async def test_unexplored_detail_links_get_browser_continuation_before_search(database,users,tmp_path,second_sufficient):
    uid=users[0]['id'];host=Host();order=[]
    class Reader(BrowserTasks):
        async def delegate(self,user,run,call_id,task,domains,inspect_only=False):
            order.append('browser')
            assert inspect_only
            if len(order)==1:
                return {'status':'unverified','verification':{'success':False},'browser_followup':{'links':['https://example.com/cards/amber']}}
            assert 'Optional navigation hints, not requirements' in task and 'https://example.com/cards/amber' in task
            return {'status':'verified' if second_sufficient else 'unverified','verification':{'success':second_sufficient},'browser_followup':{'links':['https://example.com/cards/amber']}}
    manager=Reader(database,host,'sk-test',None);sid,_,_=seed_preview(database,uid)
    class Responses:
        async def create(self,**kwargs):
            order.append('search')
            return SimpleNamespace(output_text='Card benefits found.',output=[SimpleNamespace(type='message',content=[SimpleNamespace(annotations=[SimpleNamespace(type='url_citation',url='https://example.com/cards',title='Card benefits')])])])
    responses=[call('browser_task',{'task':'Read hub','official_domains':['example.com']},'hub'),
               call('browser_task',{'task':'Explore detail links','official_domains':['example.com']},'details')]
    if not second_sufficient:responses.append(call('web_search',{'query':'Card benefits','official_domains':['example.com']},'search'))
    responses.append(AIMessage(content='Card benefits found.'))
    model=ScriptedModel(responses=responses)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'linked.sqlite')) as saver:
        engine=Engine(database,host,saver,'sk-test',model=model,client=SimpleNamespace(responses=Responses()),browser_tasks=manager)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        rid=(await service.submit(uid,sid,'summarize benefits by card'))['run_id'];await wait_service(service,rid)
        assert order==(['browser','browser'] if second_sufficient else ['browser','browser','search'])
        assert model.choices[:2]==[{'type':'function','function':{'name':'browser_task'}}]*2
        assert len(database.rows("SELECT id FROM events WHERE run_id=? AND kind='browser_context_attempt'",(rid,)))==2
