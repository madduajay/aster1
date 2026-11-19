from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine, ResearchDecision
from personal_agent.memory import Memory
from personal_agent.service import Service
from personal_agent.sources import vetted_text
from conftest import SummaryModel
from test_agent import ScriptedModel, ScriptedBrowser, wait_service


@pytest.mark.parametrize('question', ['Recommend places to stay near the center', 'yes pls do', 'again hotel options are missing'])
async def test_recommendation_router_forces_search_and_keeps_named_results(database, users, tmp_path, question):
    uid=users[0]['id'];sid=database.new_session(uid)
    database.message(uid,sid,'user','Plan a solo trip near Times Square, December 1 to 10, mid-range.')
    database.message(uid,sid,'assistant','I can give you hotel options.')
    router_call=AsyncMock(return_value=ResearchDecision(requires_research=True))
    router=SimpleNamespace(with_structured_output=lambda *a,**kw:SimpleNamespace(ainvoke=router_call))
    url='https://hotel.example/stay'
    provider=SimpleNamespace(output_text='Example Hotel near the center.',output=[SimpleNamespace(type='message',content=[
        SimpleNamespace(annotations=[SimpleNamespace(type='url_citation',url=url,title='Example Hotel')])])])
    search=AsyncMock(return_value=provider)
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{
        'query':'Find hotels for the stated trip','official_domains':['hotel.example']},'id':'search','type':'tool_call'}]),
        AIMessage(content='- [Example Hotel](https://hotel.example/stay) — a candidate for your trip.\n- [Other Hotel](https://other.example/stay) — an unverified candidate.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'routing.sqlite')) as saver:
        engine=Engine(database,ScriptedBrowser(),saver,'sk-test',model=model,research_model=router,
                      client=SimpleNamespace(responses=SimpleNamespace(create=search)))
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        run=(await service.submit(uid,sid,question))['run_id'];await wait_service(service,run)
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        assert model.choices[0]=={'type':'function','function':{'name':'web_search'}}
        search.assert_awaited_once();router_call.assert_awaited_once()
        assert question in str(router_call.call_args) and 'Times Square' in str(router_call.call_args)
        answer=database.rows("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id",(sid,))[-1]['content']
        assert '[Example Hotel]('+url+')' in answer
        assert '[Other Hotel (unverified link)](https://other.example/stay)' in answer


async def test_ordinary_chat_does_not_force_search(database, users, tmp_path):
    uid=users[0]['id'];sid=database.new_session(uid)
    router=SimpleNamespace(with_structured_output=lambda *a,**kw:SimpleNamespace(ainvoke=AsyncMock(return_value=ResearchDecision(requires_research=False))))
    model=ScriptedModel(responses=[AIMessage(content='Hello!')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'chat.sqlite')) as saver:
        engine=Engine(database,ScriptedBrowser(),saver,'sk-test',model=model,research_model=router)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        run=(await service.submit(uid,sid,'Hello'))['run_id'];await wait_service(service,run)
        assert model.choices[0] is None
        assert database.one('SELECT searches FROM run_usage WHERE run_id=?',(run,))['searches']==0
        await engine.client.close()


def test_unverified_hotel_rows_survive_repeated_render_filtering():
    draft='Here are the hotels:\n- [Example Hotel](https://hotel.example/stay)\n- [Another Hotel](https://other.example/)'
    clean=vetted_text(draft,[])
    assert clean.count('unverified link')==2
    assert 'Example Hotel' in clean and 'Another Hotel' in clean
    assert vetted_text(clean,[])==clean


async def test_invalid_search_arguments_stop_after_two_attempts(database,users,tmp_path):
    uid=users[0]['id'];sid=database.new_session(uid)
    router=SimpleNamespace(with_structured_output=lambda *a,**kw:SimpleNamespace(ainvoke=AsyncMock(return_value=ResearchDecision(requires_research=True))))
    invalid=lambda key:AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query':'Find hotels','official_domains':[]},'id':key,'type':'tool_call'}])
    model=ScriptedModel(responses=[invalid('bad1'),invalid('bad2'),AIMessage(content='Search could not run. Candidate: [Example Hotel](https://hotel.example/stay)')])
    search=AsyncMock(side_effect=AssertionError('Invalid scopes must never reach the API'))
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'bounded.sqlite')) as saver:
        engine=Engine(database,ScriptedBrowser(),saver,'sk-test',model=model,research_model=router,client=SimpleNamespace(responses=SimpleNamespace(create=search)))
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        run=(await service.submit(uid,sid,'Show hotel options'))['run_id'];await wait_service(service,run)
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        assert database.one('SELECT model_calls FROM run_usage WHERE run_id=?',(run,))['model_calls']==4
        # With no tools, LangChain invokes the base model directly rather than bind_tools.
        assert len(model.choices)==2
        assert 'Search arguments were rejected twice' in str(model.seen[-1])
        search.assert_not_awaited()
        answer=database.rows("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id",(sid,))[-1]['content']
        assert "couldn't verify" in answer and '[Example Hotel (unverified link)](https://hotel.example/stay)' in answer
