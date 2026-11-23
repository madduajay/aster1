import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine, ResearchDecision
from personal_agent.memory import Memory
from personal_agent.service import Service
from personal_agent.steering import pending, request_messages
from conftest import SummaryModel, StubBrowsers
from test_agent import ScriptedModel, wait_service
from test_task_replacement import setup_service, QuietService


def search(ident, query):
    return AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query':query,'official_domains':['example.com']},'id':ident,'type':'tool_call'}])


async def test_additions_join_inflight_graph_and_preserve_completed_tool_once(database,users,tmp_path):
    uid=users[0]['id'];sid=database.new_session(uid)
    entered=asyncio.Event();release=asyncio.Event();queries=[]
    async def fetch(**kwargs):
        queries.append(kwargs['input'])
        if len(queries)==1:entered.set();await release.wait()
        source=SimpleNamespace(type='url_citation',url='https://example.com/options',title='Options')
        return SimpleNamespace(output_text='Verified options for the requested trip and dining.',output=[SimpleNamespace(type='message',content=[SimpleNamespace(annotations=[source])])])
    model=ScriptedModel(responses=[search('first','Original trip research'),search('combined','Trip and restaurants and photos'),
        AIMessage(content='The trip includes restaurants and sourced photos. [Options](https://example.com/options)')])
    router_call=AsyncMock(return_value=ResearchDecision(requires_research=True))
    router=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router_call))
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'steering.sqlite')) as saver:
        engine=Engine(database,StubBrowsers(),saver,'sk-test',model=model,research_model=router,
                      client=SimpleNamespace(responses=SimpleNamespace(create=fetch)))
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            run=(await service.submit(uid,sid,'Find a trip.'))['run_id']
            await asyncio.wait_for(entered.wait(),5)
            second=await service.submit(uid,sid,'Also include restaurants.')
            third=await service.submit(uid,sid,'And include photos.')
            assert second['run_id']==third['run_id']==run
            assert service.active(uid)['status']=='running' and pending(database,run)
            release.set();await wait_service(service,run)
            assert queries==['Original trip research','Trip and restaurants and photos']
            assert database.one('SELECT status,error FROM runs WHERE id=?',(run,))=={'status':'completed','error':None}
            assert not pending(database,run)
            assert len(database.rows('SELECT * FROM runs WHERE session_id=?',(sid,)))==1
            assert [m['content'] for m in request_messages(database,uid,run)]==['Find a trip.','Also include restaurants.','And include photos.']
            assert len(router_call.await_args_list)==2
            context=json.loads(router_call.await_args_list[-1].args[0][-1].content)
            assert len(context['active_request_messages'])==3
            assert context['completed_work'] and 'Verified options' in str(context['completed_work'])
            assert 'Find a trip.' in str(model.seen[-1]) and 'include photos' in str(model.seen[-1])
            assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='done' AND payload LIKE '%cancelled%'",(run,))
        finally:release.set();await service.close()


async def test_immediate_followup_before_execute_starts_keeps_original(database,users,tmp_path):
    uid=users[0]['id'];sid=database.new_session(uid)
    model=ScriptedModel(responses=[AIMessage(content='Original task with the additional constraint.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'immediate.sqlite')) as saver:
        engine=Engine(database,StubBrowsers(),saver,'sk-test',model=model)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            first=await service.submit(uid,sid,'Plan the original task.')
            second=await service.submit(uid,sid,'Keep it under $100.')
            assert first['run_id']==second['run_id']
            await wait_service(service,first['run_id'])
            assert 'Plan the original task.' in str(model.seen[0])
            assert 'Keep it under $100.' in str(model.seen[0])
            assert not pending(database,first['run_id'])
        finally:await service.close();await engine.client.close()


async def test_addition_during_digest_is_processed_before_done(database,users,tmp_path):
    uid=users[0]['id'];sid=database.new_session(uid);entered=asyncio.Event();release=asyncio.Event()
    model=ScriptedModel(responses=[AIMessage(content='First result.'),AIMessage(content='First result plus the added detail.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'digest.sqlite')) as saver:
        engine=Engine(database,StubBrowsers(),saver,'sk-test',model=model)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        normal=service.digest;count=0
        async def digest(*args):
            nonlocal count
            count+=1
            if count==1:entered.set();await release.wait()
            await normal(*args)
        service.digest=digest
        try:
            run=(await service.submit(uid,sid,'Original request'))['run_id'];await asyncio.wait_for(entered.wait(),5)
            merged=await service.submit(uid,sid,'Add a detail.')
            assert merged['run_id']==run
            release.set();await wait_service(service,run)
            # A continuation is scheduled after the original coroutine's finalizer.
            await wait_service(service,run)
            assert not service.active(uid) and not pending(database,run)
            assert 'added detail' in database.one("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))['content']
        finally:release.set();await service.close();await engine.client.close()


async def test_stop_cancels_merged_work_and_other_chat_does_not_merge(database,users):
    uid=users[0]['id'];service,sid=setup_service(database,uid,'running')
    try:
        merged=await service.submit(uid,sid,'Also include photos.')
        assert merged['merged']
        await service.submit(uid,sid,'/stop')
        assert database.one("SELECT status FROM runs WHERE id='old'")['status']=='cancelled'
        assert not service.active(uid)
    finally:await service.close()
    database.execute("UPDATE runs SET status='running' WHERE id='old'")
    try:
        other=database.new_session(uid)
        result=await service.submit(uid,other,'Different chat task')
        assert result['run_id']!='old' and not result.get('merged')
        assert database.one("SELECT status FROM runs WHERE id='old'")['status']=='cancelled'
    finally:await service.close()


def test_draft_upload_can_join_running_chat_but_not_another_chat_or_pause(client,database,users,credentials):
    from conftest import authenticate
    user=users[0]['id'];sid=database.new_session(user);other=database.new_session(user)
    authenticate(client,credentials)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('active-upload',?,?,'running',0)",(user,sid))
    client.app.state.service.active=lambda uid:database.one("SELECT * FROM runs WHERE user_id=? AND status IN ('running','resuming','input','approval')",(uid,))
    uploaded=client.post('/api/uploads?draft=true&session_id='+sid,content=b'Extra trip constraints',headers={'X-Filename':'constraints.txt'})
    assert uploaded.status_code==200
    assert client.post('/api/uploads?draft=true&session_id='+other,content=b'Other chat').status_code==409
    database.execute("UPDATE runs SET status='input' WHERE id='active-upload'")
    assert client.post('/api/uploads?draft=true&session_id='+sid,content=b'Paused').status_code==409


async def test_merged_availability_keeps_original_counts_and_named_resource(database,users):
    from test_availability import H
    user=users[0]['id'];sid=database.new_session(user)
    service=QuietService(database,SimpleNamespace(model=None,browsers=StubBrowsers()))
    try:
        run=(await service.submit(user,sid,'Check The Plaza in New York December 1 to 3, 2099, for 1 guest, 1 room.'))['run_id']
        await service.submit(user,sid,'Also include photos.')
        q=service.availability.envelope(user,run,[{'domain':'hotels','criteria':{**H,'hotel':'The Plaza','guests':9,'rooms':9}}],'travel').queries[0]
        assert q.criteria['hotel']=='The Plaza' and q.criteria['guests']==q.criteria['rooms']==1
        await service.submit(user,sid,'Actually, 3 guests.')
        q=service.availability.envelope(user,run,[{'domain':'hotels','criteria':{**H,'hotel':'The Plaza','guests':1,'rooms':1}}],'travel').queries[0]
        assert q.criteria['guests']==3 and q.criteria['rooms']==1
    finally:await service.close()
