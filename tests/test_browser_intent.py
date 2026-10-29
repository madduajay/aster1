from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine
from personal_agent.browser_intent import requests_page_open
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel,wait_service


@pytest.mark.parametrize('text',[
    'can you show me the main page of target',
    'can you show the main page of americanexpress',
    'Please open the homepage of a new company',
    'Could you please visit https://docs.python.org',
    'go to example.org',
    'Navigate to the official website for this company',
    'Show me https://example.org/catalog',
    'load the web page from the last message',
])
def test_explicit_page_requests_require_navigation(text):assert requests_page_open(text)


@pytest.mark.parametrize('text',[
    'what are the best deals for a walking shoe on amazon',
    'Show me a link to the Target homepage',
    'Show me the URL for their official website',
    'Show me how to open a website',
    'Explain how website navigation works',
    'Please do not open this website',
    "Show me the main page link, don't open it",
    'What is on the main page of example.org?',
    'Open the door',
    'Hello',
])
def test_research_links_and_non_browser_requests_keep_normal_routing(text):assert not requests_page_open(text)


async def test_page_request_forces_browser_once_then_allows_final_answer(database,users,tmp_path):
    user=users[0]['id'];sid=database.new_session(user)
    model=ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Open https://example.org/ in the browser panel','official_domains':['example.org']},'id':'open-page','type':'tool_call'}]),
        AIMessage(content='The page is open in the browser panel.')])
    delegate=AsyncMock(return_value={'status':'verified','verification':{'success':True}})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'navigation.sqlite')) as saver:
        engine=Engine(database,SimpleNamespace(),saver,'sk-controlled',model=model,browser_tasks=SimpleNamespace(delegate=delegate))
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        run=(await service.submit(user,sid,'Can you show me the main page of example.org?'))['run_id'];await wait_service(service,run)
        assert model.choices==[{'type':'function','function':{'name':'browser_task'}},None]
        assert delegate.await_count==1
        assert delegate.call_args.args[4]==['example.org']
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        await engine.client.close()


async def test_unknown_website_can_ask_for_clarification_after_one_blocked_attempt(database,users,tmp_path):
    user=users[0]['id'];sid=database.new_session(user)
    model=ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Open the requested website','official_domains':[]},'id':'unknown-page','type':'tool_call'}]),
        AIMessage(content='Which official website URL should I open?')])
    delegate=AsyncMock()
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'unknown.sqlite')) as saver:
        engine=Engine(database,SimpleNamespace(),saver,'sk-controlled',model=model,browser_tasks=SimpleNamespace(delegate=delegate))
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        run=(await service.submit(user,sid,'Show me the website for that unknown company'))['run_id'];await wait_service(service,run)
        delegate.assert_not_awaited()
        assert model.choices==[{'type':'function','function':{'name':'browser_task'}},None]
        assert 'Which official website' in database.one('SELECT content FROM messages ORDER BY id DESC')['content']
        await engine.client.close()
