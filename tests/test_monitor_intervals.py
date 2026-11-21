import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from temporalio.testing import WorkflowEnvironment
from personal_agent import config
from personal_agent.agent import ResearchDecision
from availability.contracts import Task,digest
from availability.intervals import requested,seconds,label
from test_availability import D,service_for
from test_agent import wait_service


@pytest.mark.parametrize('prompt,expected',[
    ('check every 10 mins',600),('every ten minutes',600),('every 1 minute',60),
    ('every 30 seconds',60),('every 0.5 minutes',60),('every 0 minutes',60),
    ('every 2 hours',7200),('every half an hour',1800),('hourly',3600),
    ('notify me when available',None)])
def test_requested_interval_and_minimum(prompt,expected):assert requested(prompt)==expected


def test_interval_defaults_and_customer_wording():
    assert seconds({})==300 and seconds({'monitor_interval_seconds':1})==60
    assert label(600)=='10 minutes' and label(60)=='1 minute'


async def test_cadence_is_bound_to_user_text_not_tool_arguments(database,users):
    uid=users[0]['id'];sid=database.new_session(uid)
    database.message(uid,sid,'user','Check Carbone every 10 mins')
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('cadence-run',?,?,'running',?)",(uid,sid,time.time()))
    service=service_for(database,[AIMessage(content='Hi')])
    try:
        task=service.availability.envelope(uid,'cadence-run',[{'domain':'dining','criteria':D,'interval_seconds':5}],'dining')
        assert task.preferences['monitor_interval_seconds']==600
    finally:await service.close();await service.engine.client.close()


async def test_chat_acknowledges_actual_ten_minute_cadence(database,users):
    uid=users[0]['id'];sid=database.new_session(uid)
    model=[AIMessage(content='',tool_calls=[{'name':'delegate_dining','args':{'queries':[{'domain':'dining','criteria':D}],'monitor':True},'id':'monitor','type':'tool_call'}]),AIMessage(content='Monitoring started.')]
    service=service_for(database,model)
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=AsyncMock(return_value=ResearchDecision(requires_research=False,dining_availability=True,monitoring_requested=True))))
    async def delegate(user,run,*args,**kwargs):
        result={'status':'monitoring','interval_seconds':600,'results':[{'domain':'dining','criteria':D,'status':'completed','available':True,'options':[{'name':'Carbone','times':['19:00']}]}]}
        database.event(run,'availability_result',result);return result
    service.availability.delegate=delegate
    try:
        rid=(await service.submit(uid,sid,'Check Carbone every 10 mins for 2 people on December 1, 2099'))['run_id']
        await wait_service(service,rid)
        answer=database.one("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))['content']
        assert 'every 10 minutes' in answer and 'five minutes' not in answer
    finally:await service.close();await service.engine.client.close()


@pytest.mark.parametrize('explicit,expected',[(False,600),(True,300)])
async def test_existing_cadence_changes_only_when_user_requests_a_new_one(database,users,explicit,expected):
    uid=users[0]['id'];sid=database.new_session(uid);source=database.message(uid,sid,'user','Keep checking Carbone')
    normalized=[{'domain':'dining','criteria':D}];mid=digest([uid,sid,normalized])[:40]
    task=Task(request_id='cadence-followup-task',user_id=uid,session_id=sid,source_message_id=source,queries=normalized,preferences={'monitor_interval_seconds':600})
    service=service_for(database,[AIMessage(content='Hi')]);gateway=service.availability
    gateway.store.execute('INSERT INTO monitors(id,user_id,session_id,envelope,expires,state,schedule_id) VALUES(?,?,?,?,?,?,?)',(mid,uid,sid,task.model_dump_json(),time.time()+3600,'active','av-schedule-'+mid))
    gateway.check=AsyncMock(return_value={'results':[]});gateway.prepare_watches=AsyncMock();gateway.ensure_schedule=AsyncMock()
    try:
        task.preferences={'monitor_interval_seconds':300,'monitor_interval_explicit':explicit}
        response=await gateway.create_monitor(task,'')
        assert response['interval_seconds']==expected
        saved=json.loads(gateway.store.one('SELECT envelope FROM monitors WHERE id=?',(mid,))['envelope'])
        assert saved['preferences']['monitor_interval_seconds']==expected
    finally:await service.close();await service.engine.client.close()


@pytest.mark.skipif(os.environ.get('RUN_AVAILABILITY_TESTS')!='1',reason='Enable native Temporal schedule checks')
async def test_native_schedule_migrates_legacy_cadence_and_enforces_one_minute(database,users):
    uid=users[0]['id'];sid=database.new_session(uid);source=database.message(uid,sid,'user','Check Carbone every 10 mins')
    environment=await WorkflowEnvironment.start_local(download_dest_dir=str(config.ROOT/'.runtime/bin'))
    service=service_for(database,[AIMessage(content='Hi')]);gateway=service.availability;gateway.client=environment.client
    task=Task(request_id='cadence-native-request',user_id=uid,session_id=sid,source_message_id=source,queries=[{'domain':'dining','criteria':D}],preferences={'monitor_interval_seconds':300})
    mid='cadence-native-monitor';anchor=time.time();expires=anchor+7200
    gateway.store.execute('INSERT INTO monitors(id,user_id,session_id,envelope,expires,state,schedule_id,anchor) VALUES(?,?,?,?,?,?,?,?)',(mid,uid,sid,task.model_dump_json(),expires,'active','av-schedule-'+mid,anchor))
    try:
        await gateway.ensure_schedule(mid)
        handle=environment.client.get_schedule_handle('av-schedule-'+mid)
        assert (await handle.describe()).schedule.spec.intervals[0].every.total_seconds()==300
        # Original releases had no cadence field; recover it from the owned
        # originating user message, then update the existing native schedule.
        task.preferences={};gateway.store.execute('UPDATE monitors SET envelope=? WHERE id=?',(task.model_dump_json(),mid))
        await gateway.ensure_schedule(mid)
        assert (await handle.describe()).schedule.spec.intervals[0].every.total_seconds()==600
        row=gateway.store.one('SELECT * FROM monitors WHERE id=?',(mid,))
        assert 585<row['next_check']-anchor<615
        assert gateway.monitors(uid)[0]['interval_seconds']==600
        task.preferences={'monitor_interval_seconds':1};gateway.store.execute('UPDATE monitors SET envelope=? WHERE id=?',(task.model_dump_json(),mid))
        await gateway.ensure_schedule(mid)
        assert (await handle.describe()).schedule.spec.intervals[0].every.total_seconds()==60
        assert gateway.monitors(uid)[0]['interval_seconds']==60
    finally:await service.close();await service.engine.client.close();await environment.shutdown()
