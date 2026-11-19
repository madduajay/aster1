"""Replacement retires the old graph/worker before a new run can acquire a profile."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine
from personal_agent.browser_intent import starts_new_request
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel, ScriptedBrowser, wait_service


class QuietService(Service):
    """Hold a run indefinitely without spending tokens or touching websites."""
    async def execute(self, *args, **kwargs):
        await asyncio.Event().wait()


def setup_service(db, uid, status='input', kind='manual'):
    manager=SimpleNamespace(cancel_run=AsyncMock())
    host=SimpleNamespace(manual_ready=AsyncMock(return_value=True),release_control=AsyncMock())
    service=QuietService(db,SimpleNamespace(model=None,browser_tasks=manager,browsers=host))
    sid=db.new_session(uid)
    pending={'type':'browser_input','action':{'kind':kind,'prompt':'Which time?' if kind=='information' else 'Complete login'}}
    db.execute('INSERT INTO runs(id,user_id,session_id,status,created,approval_id,approval) VALUES(?,?,?,?,0,?,?)',('old',uid,sid,status,'pending',json.dumps(pending)))
    return service,sid


@pytest.mark.parametrize('status',['running','resuming','approval','input'])
async def test_new_prompt_cancels_every_active_state_and_revokes_old_controls(database,users,status):
    uid=users[0]['id'];service,sid=setup_service(database,uid,status)
    try:
        result=await service.submit(uid,sid,'Find a different restaurant')
        old=database.one("SELECT * FROM runs WHERE id='old'")
        assert old['status']=='cancelled' and old['approval_id'] is None and old['approval'] is None
        assert service.active(uid)['id']==result['run_id']
        service.engine.browser_tasks.cancel_run.assert_awaited_once_with(uid,'old')
        with pytest.raises(LookupError):await service.resume(uid,'old','approve','pending')
        with pytest.raises(LookupError):await service.respond_input(uid,'old','pending','continue')
        assert not database.one("SELECT id FROM messages WHERE role='user' AND content='Cancel'")
    finally:await service.close()


async def test_new_chat_waits_for_cleanup_preserves_history_and_permanent_memory(database,users):
    uid=users[0]['id'];service,sid=setup_service(database,uid)
    mid=database.message(uid,sid,'user','Remember I prefer quiet venues')
    database.put_fact(uid,'I prefer quiet venues',mid,'explicit.venues',evidence='Remember I prefer quiet venues')
    entered=asyncio.Event();release=asyncio.Event()
    async def cleanup(*args):entered.set();await release.wait()
    service.engine.browser_tasks.cancel_run=AsyncMock(side_effect=cleanup)
    request=asyncio.create_task(service.new_session(uid));await entered.wait()
    assert not request.done()
    # The next request cannot start while old profile cleanup is pending.
    next_request=asyncio.create_task(service.submit(uid,sid,'Show new options'))
    await asyncio.sleep(0)
    assert not next_request.done()
    release.set();fresh=await request;await next_request
    assert fresh!=sid and not database.rows('SELECT id FROM messages WHERE session_id=?',(fresh,))
    assert database.one('SELECT id FROM messages WHERE id=?',(mid,))
    assert database.facts(uid,inspect=True)[0]['text']=='I prefer quiet venues'
    await service.close()


async def test_unauthorized_or_stale_bound_request_never_cancels_current_run(database,users):
    uid=users[0]['id'];service,sid=setup_service(database,uid)
    foreign=database.new_session(users[1]['id'])
    for destination,input_id in [(foreign,None),(sid,'stale')]:
        with pytest.raises(LookupError):await service.submit(uid,destination,'Find shoes',pending_input_id=input_id)
        assert service.active(uid)['id']=='old'
    service.engine.browser_tasks.cancel_run.assert_not_awaited()
    await service.close()


async def test_cancel_before_execute_starts_and_concurrent_submissions(database,users):
    uid=users[0]['id'];service,sid=setup_service(database,uid,'running')
    service.tasks['old']=asyncio.create_task(service.execute(uid,sid,'old'))
    try:
        a,b=await asyncio.gather(service.submit(uid,sid,'First replacement'),service.submit(uid,sid,'Second replacement'))
        assert database.one("SELECT status FROM runs WHERE id='old'")['status']=='cancelled'
        assert database.one('SELECT status FROM runs WHERE id=?',(a['run_id'],))['status']=='cancelled'
        assert service.active(uid)['id']==b['run_id']
        assert [c.args[1] for c in service.engine.browser_tasks.cancel_run.await_args_list]==['old',a['run_id']]
    finally:await service.close()


async def test_bound_information_and_continue_resume_but_new_instruction_replaces(database,users):
    uid=users[0]['id'];service,sid=setup_service(database,uid,kind='information')
    try:
        resumed=await service.submit(uid,sid,'7 PM',pending_input_id='pending')
        assert resumed['run_id']=='old' and service.active(uid)['status']=='resuming'
        service.engine.browser_tasks.cancel_run.assert_not_awaited()
    finally:await service.close()
    database.execute("UPDATE runs SET status='input' WHERE id='old'")
    service,sid2=service,sid
    try:
        replaced=await service.submit(uid,sid2,'Can you open another website?',pending_input_id='pending')
        assert replaced['run_id']!='old' and service.active(uid)['id']==replaced['run_id']
    finally:await service.close()


async def test_manual_code_is_never_logged_as_new_prompt(database,users):
    uid=users[0]['id'];service,sid=setup_service(database,uid)
    with pytest.raises(ValueError,match='browser'):
        await service.submit(uid,sid,'654321',pending_input_id='pending')
    assert service.active(uid)['id']=='old'
    assert not database.rows("SELECT id FROM messages WHERE content LIKE '%654321%'")
    resumed=await service.submit(uid,sid,'continue',pending_input_id='pending')
    assert resumed['run_id']=='old'
    await service.close()


async def test_replacement_uses_real_graph_new_prompt_and_session_context(database,users,tmp_path):
    uid=users[0]['id'];sid=database.new_session(uid)
    browser=ScriptedBrowser();browser.cancel_run=AsyncMock()
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'browser_task','args':{'task':'Perform consequential test action','official_domains':['example.com']},'id':'old-call','type':'tool_call'}]),AIMessage(content='Here is the new answer.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'replacement.sqlite')) as saver:
        engine=Engine(database,browser,saver,'sk-test',model=model,browser_tasks=browser)
        service=Service(database,engine);service.memory=Memory(database,SummaryModel())
        try:
            old=(await service.submit(uid,sid,'Perform the previous action'))['run_id'];await wait_service(service,old)
            assert service.active(uid)['status']=='approval'
            fresh=await service.new_session(uid)
            new=(await service.submit(uid,fresh,'Which colors are available?'))['run_id'];await wait_service(service,new)
            assert database.one('SELECT status FROM runs WHERE id=?',(old,))['status']=='cancelled'
            assert database.one('SELECT status FROM runs WHERE id=?',(new,))['status']=='completed'
            assert browser.completed==[]
            assert 'Which colors are available?' in str(model.seen[-1])
            assert 'Perform the previous action' not in str(model.seen[-1])
            assert database.owned_session(uid,fresh)['summary']
        finally:await service.close();await engine.client.close()


@pytest.mark.parametrize('text',['Show me other shoes','what about other states','please find a table','Can you open the website?','/memory','start again'])
def test_information_prompt_routing_is_website_independent(text):
    assert starts_new_request(text)


@pytest.mark.parametrize('text',['7 PM','+1 202 555 0100','blue','654321','Jane Doe','yes'])
def test_information_answers_remain_bound(text):
    assert not starts_new_request(text)
