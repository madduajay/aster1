import asyncio
import json
from types import SimpleNamespace

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import interrupt
from pydantic import Field

from personal_agent.agent import Engine
from personal_agent.memory import Digest, Memory
from personal_agent.service import Service
from conftest import SummaryModel


class ScriptedModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)
    offered: list = Field(default_factory=list)
    choices: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.offered = [getattr(t, 'name', '') for t in tools]
        self.choices.append(kwargs.get('tool_choice'))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class ScriptedBrowser:
    def __init__(self):
        self.completed = []

    def set_scope(self, user, run, domains):
        self.scope = domains

    async def delegate(self,user,run,call_id,task,domains,inspect_only=False):
        self.scope=domains
        decision=interrupt({'type':'browser_approval','action':{'operation':'browser_task','url':'https://example.com','task':task}})
        if decision!='approve':return {'status':'rejected'}
        self.completed.append((user,call_id,task))
        return {'status':'verified','verification':{'success':True}}


async def wait_service(service, run):
    task = service.tasks.get(run)
    if task:
        await asyncio.wait_for(task, 15)


async def test_real_deepagents_context_and_session_digest(database, users, tmp_path):
    uid = users[0]['id']; sid = database.new_session(uid)
    database.message(uid, sid, 'user', 'Find quiet Phoenix hotels')
    database.message(uid, sid, 'assistant', 'Here are two quiet Phoenix hotels')
    model = ScriptedModel(responses=[AIMessage(content='The downtown hotel fits.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite')) as saver:
        engine = Engine(database, ScriptedBrowser(), saver, 'sk-test', model=model)
        service = Service(database, engine)
        digest = SummaryModel(Digest(summary='User chose a quiet downtown Phoenix hotel.'))
        service.memory = Memory(database, digest)
        run = (await service.submit(uid, sid, 'Which one is downtown?'))['run_id']
        await wait_service(service, run)
        assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'
        assert 'quiet Phoenix hotels' in str(model.seen[0])
        assert 'Which one is downtown?' in str(model.seen[0])
        assert set(model.offered) <= {'web_search', 'browser_task', 'current_time', 'write_todos'}
        assert digest.inputs
        assert database.owned_session(uid, sid)['summary'].endswith('User chose a quiet downtown Phoenix hotel.')
        await engine.client.close()


async def test_approval_survives_service_restart(database, users, tmp_path):
    uid = users[0]['id']; sid = database.new_session(uid)
    browser = ScriptedBrowser()
    model = ScriptedModel(responses=[
        AIMessage(content='', tool_calls=[{'name': 'browser_task', 'args': {'task':'Perform the requested website task','official_domains':['example.com']}, 'id': 'action1', 'type': 'tool_call'}]),
        AIMessage(content='The approved action completed.')])
    path = str(tmp_path / 'graph.sqlite')
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        engine = Engine(database, browser, saver, 'sk-test', model=model, browser_tasks=browser)
        service = Service(database, engine)
        service.memory = Memory(database, SummaryModel())
        run = (await service.submit(uid, sid, 'Perform the test action'))['run_id']
        await wait_service(service, run)
        pending = service.active(uid)
        assert pending['status'] == 'approval' and browser.completed == []
        await engine.client.close()
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        engine = Engine(database, browser, saver, 'sk-test', model=model, browser_tasks=browser)
        reloaded = Service(database, engine)
        reloaded.memory = Memory(database, SummaryModel())
        reloaded.recover()
        await reloaded.submit(uid, sid, f"approve {pending['approval_id']}")
        await wait_service(reloaded, run)
        assert len(browser.completed) == 1
        assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'
        await engine.client.close()


async def test_rejection_never_executes_browser_task(database, users, tmp_path):
    uid = users[0]['id']; sid = database.new_session(uid)
    browser = ScriptedBrowser()
    model = ScriptedModel(responses=[
        AIMessage(content='', tool_calls=[{'name': 'browser_task', 'args': {'task':'Perform the requested website task','official_domains':['example.com']}, 'id': 'action2', 'type': 'tool_call'}]),
        AIMessage(content='I did not perform the action.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite')) as saver:
        engine = Engine(database, browser, saver, 'sk-test', model=model, browser_tasks=browser)
        service = Service(database, engine); service.memory = Memory(database, SummaryModel())
        run = (await service.submit(uid, sid, 'Perform test action'))['run_id']
        await wait_service(service, run)
        approval = service.active(uid)
        await service.submit(uid, sid, f"reject {approval['approval_id']}")
        await wait_service(service, run)
        assert browser.completed == []
        await engine.client.close()


async def test_provider_error_retains_communication(database, users, tmp_path):
    uid = users[0]['id']; sid = database.new_session(uid)
    class FailedEngine:
        model = None
        def graph(self, *args):
            raise RuntimeError('Controlled provider failure')
    service = Service(database, FailedEngine())
    run = (await service.submit(uid, sid, 'Do not lose this request'))['run_id']
    await wait_service(service, run)
    rows = database.rows('SELECT * FROM messages WHERE session_id=? ORDER BY id', (sid,))
    assert rows[0]['content'] == 'Do not lose this request'
    assert 'conversation is saved' in rows[-1]['content']
    assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'failed'


async def test_search_uses_responses_and_keeps_sources(database, users, tmp_path):
    uid = users[0]['id']; sid = database.new_session(uid)
    class FakeResponses:
        args = None
        async def create(self, **kwargs):
            self.args = kwargs
            return SimpleNamespace(output_text='Example evidence', output=[SimpleNamespace(type='message', content=[
                SimpleNamespace(annotations=[SimpleNamespace(type='url_citation', url='https://example.com/', title='Example')])])])
    responses = FakeResponses()
    model = ScriptedModel(responses=[
        AIMessage(content='', tool_calls=[{'name':'web_search','args':{'query':'test query','official_domains':['example.com']},'id':'search1','type':'tool_call'}]),
        AIMessage(content='Example evidence [Example](https://example.com/).')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite')) as saver:
        engine = Engine(database, ScriptedBrowser(), saver, 'sk-test', model=model, client=SimpleNamespace(responses=responses))
        service = Service(database, engine); service.memory = Memory(database, SummaryModel())
        run = (await service.submit(uid, sid, 'Search for test query'))['run_id']
        await wait_service(service, run)
        assert responses.args['model'] == 'gpt-5.4-mini'
        assert responses.args['tools'][0]['type'] == 'web_search'
        assert responses.args['max_tool_calls'] == 1
        assert responses.args['input'] == 'test query'
        assert responses.args['tools'][0]['filters']['allowed_domains'] == ['example.com']
        assert responses.args['reasoning']['effort'] == 'low'
        final = database.rows('SELECT content FROM messages WHERE session_id=? ORDER BY id', (sid,))[-1]['content']
        assert 'https://example.com/' in final


async def test_chat_and_memory_keep_gpt41_mini(database):
    from personal_agent import config
    engine = Engine(database, ScriptedBrowser(), None, 'sk-test')
    assert engine.model.model_name == config.MODEL == 'gpt-4.1-mini'
    assert Memory(database, engine.model).model is engine.model
    assert config.SEARCH_MODEL == 'gpt-5.4-mini'
    await engine.client.close()


async def test_backlog_summary_keeps_current_message_verbatim(database, users, tmp_path):
    uid = users[0]['id']; sid = database.new_session(uid)
    for n in range(7):
        database.message(uid, sid, 'user', f'Historical request {n}: ' + 'x' * 15000)
    current = 'CURRENT REQUEST: ' + 'v' * 2000
    model = ScriptedModel(responses=[AIMessage(content='Current request handled.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite')) as saver:
        engine = Engine(database, ScriptedBrowser(), saver, 'sk-test', model=model)
        service = Service(database, engine); service.memory = Memory(database, SummaryModel())
        run = (await service.submit(uid, sid, current))['run_id']
        await wait_service(service, run)
        supplied = json.loads(model.seen[0][-1].content.split('\n', 1)[1])
        assert supplied['current_user_message']['content'] == current
        assert supplied['summary']
        assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'
        await engine.client.close()


async def test_stop_is_saved_and_run_cancelled(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    class WaitingGraph:
        async def astream(self, *args, **kwargs):
            await asyncio.Event().wait()
            yield None
    class WaitingEngine:
        model = None
        def graph(self, *args):
            return WaitingGraph()
    service = Service(database, WaitingEngine())
    run = (await service.submit(uid, sid, 'Wait for cancellation'))['run_id']
    await asyncio.sleep(0)
    await service.submit(uid, sid, '/stop')
    assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'cancelled'
    assert database.one("SELECT id FROM messages WHERE session_id=? AND role='user' AND content='/stop'", (sid,))


