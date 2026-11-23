from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine
from personal_agent.browser_intent import requests_page_open
from personal_agent.browser_intent import related_browser_followup
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel,wait_service


def test_short_confirmation_uses_only_visible_same_chat_offer(database,users):
    import json
    from personal_agent.browser_intent import preview_request
    user=users[0]['id'];sid=database.new_session(user)
    other=database.new_session(user)
    database.message(user,other,'assistant','An unrelated offer in another chat.')
    database.message(user,sid,'assistant','I can compare the best room and rate for December 2–10, 2026.')
    mid=database.message(user,sid,'user','yes pls do')
    args=(database,user,sid,{'id':mid,'content':'yes pls do'},'Compare rooms and rates')
    resolved=json.loads(preview_request(*args))
    assert 'December 2–10, 2026' in resolved['preceding_assistant_offer']
    assert 'unrelated' not in resolved['preceding_assistant_offer']
    database.execute('UPDATE sessions SET context_floor=? WHERE id=?',(mid-1,sid))
    assert 'preceding_assistant_offer' not in json.loads(preview_request(*args))


def test_short_confirmation_context_respects_chat_and_clearing(database,users):
    import json
    from personal_agent.browser_intent import preview_request
    user=users[0]['id'];sid=database.new_session(user)
    offer=database.message(user,sid,'assistant','I can compare room rates for Dec 2–10, 2026, for 2 adults.')
    mid=database.message(user,sid,'user','yes pls do')
    message={'id':mid,'content':'yes pls do'}
    result=json.loads(preview_request(database,user,sid,message,'Compare room rates'))
    assert result['preceding_assistant_offer'].startswith('I can compare room rates')
    assert result['resolved_goal']=='Compare room rates'
    database.execute('UPDATE sessions SET context_floor=? WHERE id=?',(offer,sid))
    assert 'preceding_assistant_offer' not in json.loads(preview_request(database,user,sid,message,'Compare room rates'))
    other=database.new_session(user)
    assert 'preceding_assistant_offer' not in json.loads(preview_request(database,user,other,message,'Compare room rates'))


@pytest.mark.parametrize('text,related',[
    ('show me all card benefits',True),('What about dining benefits?',True),
    ('tell me more',True),('summarize it',True),('Explain that page',True),
    ('hello',False),('Find blue shoes instead',False),('What is the weather in Paris this weekend?',False),
    ('Show hotels in Dubai',False),('Open https://other.example/',False),
])
def test_preview_followups_do_not_retain_unrelated_topics(text,related):
    assert related_browser_followup(text,'Open the official Amex card benefits page and summarize its benefits') is related








@pytest.mark.parametrize('text',['hello','Hi!','hey there','Thanks','Good morning'])
def test_greetings_move_on_from_website_input(text):
    from personal_agent.browser_intent import starts_new_request
    assert starts_new_request(text,'manual')
    assert not starts_new_request('654321','manual')


@pytest.mark.parametrize('text',[
    'can you show me the main page of target',
    'can you show the main page of americanexpress',
    'Please open the homepage of a new company',
    'Could you please visit https://docs.python.org',
    'go to example.org',
    'Navigate to the official website for this company',
    'Show me https://example.org/catalog',
    'load the web page from the last message',
    'can you take me to the application to fillout',
    'Open the application form',
    'show americanexpress, login and then tell me my card balance',
    'Show another provider, sign in and read my account details',
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


@pytest.mark.parametrize('text,expected',[
    ('can you take me to the application to fillout',True),
    ('Open the application form so I can fill it out',True),
    ('Open the homepage and fill out the form for me',False),
    ('Open the website, then submit my details',False),
    ('Open the website and book a table',False),
    ('take me to amex card benefits page and summarize all card benefits',False),
    ('Open https://example.com and check my balance',False),
    ('Open the website, then read the benefits and explain them',False),
    ('Fill out the application form',False)])
def test_navigation_only_excludes_following_mutations(text,expected):
    from personal_agent.browser_intent import navigation_only_request
    assert navigation_only_request(text)==expected


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


@pytest.mark.parametrize('text',[
    'show americanexpress, login and then tell me my card balance',
    'Open Example Service',
])
async def test_site_actions_delegate_despite_prior_inability_claims(database,users,tmp_path,text):
    from personal_agent.agent import ResearchDecision
    uid=users[0]['id'];sid=database.new_session(uid)
    database.message(uid,sid,'assistant','I cannot open websites or read your account. Do it yourself.')
    model=ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Open the requested provider and read the requested balance after manual sign-in','official_domains':['example.org']},'id':'account-read','type':'tool_call'}]),
        AIMessage(content='Your requested balance is $123.45.')])
    router=AsyncMock(return_value=ResearchDecision(requires_research=False,browser_navigation=True))
    delegate=AsyncMock(return_value={'status':'verified','verification':{'success':True},'result':'Requested balance: $123.45'})
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'account.sqlite')) as saver:
        engine=Engine(database,SimpleNamespace(),saver,'sk-test',model=model,
            browser_tasks=SimpleNamespace(delegate=delegate),
            research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router)))
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        run=(await service.submit(uid,sid,text))['run_id'];await wait_service(service,run)
        assert model.choices==[{'type':'function','function':{'name':'browser_task'}},None]
        delegate.assert_awaited_once()
        assert '123.45' in database.one('SELECT content FROM messages ORDER BY id DESC')['content']
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
