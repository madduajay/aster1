from datetime import datetime, timezone
import json

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.clock import clock_context
from personal_agent.agent import Engine
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel
from test_agent import ScriptedModel, ScriptedBrowser, wait_service


def test_phoenix_clock_uses_local_date_instead_of_utc():
    context=clock_context('America/Phoenix',now=datetime(2026,10,28,2,30,tzinfo=timezone.utc))
    assert context['date']=='2026-10-27'
    assert context['local_datetime']=='2026-10-27T19:30:00-07:00'
    assert context['weekday']=='Tuesday' and context['tomorrow']=='2026-10-28'


def test_date_rollover_and_explicit_timezone():
    instant=datetime(2026,1,1,1,tzinfo=timezone.utc)
    assert clock_context('America/Phoenix',now=instant)['date']=='2025-12-31'
    assert clock_context('America/Phoenix',now=instant)['tomorrow']=='2026-01-01'
    assert clock_context('Asia/Kolkata',now=instant)['local_datetime']=='2026-01-01T06:30:00+05:30'
    assert clock_context('America/New_York',now=datetime(2026,7,1,12,tzinfo=timezone.utc))['local_datetime'].endswith('-04:00')
    with pytest.raises(ValueError): clock_context('not/a/timezone')
    with pytest.raises(ValueError): clock_context(now=datetime(2026,1,1))


async def test_runtime_clock_is_in_every_agent_model_call(database,users,tmp_path,monkeypatch):
    import personal_agent.agent as module
    instant={'date':'2026-10-27','local_datetime':'2026-10-27T19:30:00-07:00','timezone':'America/Phoenix'}
    monkeypatch.setattr(module,'clock_context',lambda zone=None: instant)
    uid=users[0]['id'];sid=database.new_session(uid)
    model=ScriptedModel(responses=[
        AIMessage(content='',tool_calls=[{'name':'current_time','args':{},'id':'clock1','type':'tool_call'}]),
        AIMessage(content='It is October 27, 2026 in Phoenix.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'clock.sqlite')) as saver:
        engine=Engine(database,ScriptedBrowser(),saver,'sk-test',model=model)
        service=Service(database,engine); service.memory=Memory(database,SummaryModel())
        run=(await service.submit(uid,sid,'What is the current local date and time?'))['run_id']
        await wait_service(service,run)
        assert len(model.seen)==2
        for call in model.seen:
            assert any(m.type=='system' and '2026-10-27T19:30:00-07:00' in str(m.content) for m in call)
        assert 'current_time' in model.offered
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        await engine.client.close()
