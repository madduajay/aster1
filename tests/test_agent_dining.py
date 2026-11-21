import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from personal_agent.agent import ResearchDecision
from dining_test_helpers import agent_service,lookup_messages,fake_company,submit_agent,FakeTemporal,emit_opening


async def test_agent_calls_company_first_and_starts_no_workflow_for_available_venue(database,users):
    service=agent_service(database,lookup_messages(venue='Nobu',reply='Nobu has tables at 18:30, 19:00, 19:30.'))
    calls=fake_company(service)
    router_call=AsyncMock(return_value=ResearchDecision(requires_research=False,dining_availability=True))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router_call))
    uid=users[0]['id'];sid=database.new_session(uid)
    try:
        result=await submit_agent(service,uid,sid,'Check Nobu availability in New York on 2099-12-01 for 2.')
        assert result['run_id'] and '18:30' in result['reply']
        assert [name for name,_ in calls]==['search_dining']
        assert not service.dining.client.requests
        assert not database.rows('SELECT id FROM background_jobs')
        assert service.engine.model.choices[0]=={'type':'function','function':{'name':'delegate_dining'}}
        assert database.one('SELECT searches FROM run_usage')['searches']==0
        assert database.one("SELECT status FROM dining_watches")['status']=='available'
    finally:await service.close();await service.engine.client.close()


async def test_agent_lookup_precedes_unavailable_only_workflow_and_retry_reuses_request(database,users):
    service=agent_service(database,lookup_messages()+lookup_messages(venue='CARBONE'))
    calls=fake_company(service);uid=users[0]['id'];sid=database.new_session(uid)
    original=service.dining.client.start_workflow
    async def start(*args,**kwargs):
        assert calls and calls[-1][0]=='search_dining'
        await original(*args,**kwargs)
    service.dining.client.start_workflow=start
    try:
        result=await submit_agent(service,uid,sid,'Check Carbone availability in New York on 2099-12-01 for 2.')
        await submit_agent(service,uid,sid,'Check CARBONE availability in New York on 2099-12-01 for 2.')
        assert len(service.dining.client.requests)==1
        assert set(service.dining.client.workflow_types)=={'DiningWatchWorkflow.run'}
        assert [name for name,_ in calls]==['search_dining','search_dining']
        assert calls[0][1]==calls[1][1]
        messages=service.engine.model.seen[1]
        assert any(call['name']=='delegate_dining' for m in messages for call in getattr(m,'tool_calls',[]))
        assert any(m.type=='tool' and json.loads(m.content).get('watch_status')=='active' for m in messages)
        assert not service.background.snapshot(uid)['notifications']
    finally:await service.close();await service.engine.client.close()


async def test_ordinary_agent_reply_does_not_call_company_or_temporal(database,users):
    service=agent_service(database,[AIMessage(content='Carbone serves Italian food.')]);calls=fake_company(service)
    try:
        uid=users[0]['id'];await submit_agent(service,uid,database.new_session(uid),'Tell me about Carbone.')
        assert not calls and not service.dining.client.requests
    finally:await service.close();await service.engine.client.close()


async def test_missing_user_details_do_not_query_company_or_start_workflow(database,users):
    service=agent_service(database,lookup_messages(location='',date='',party_size=0,reply='Please provide location, date and party size.'))
    calls=fake_company(service)
    try:
        uid=users[0]['id'];await submit_agent(service,uid,database.new_session(uid),'Is Carbone available?')
        assert not calls and not service.dining.client.requests
        assert database.one('SELECT status FROM dining_watches')['status']=='needs_details'
    finally:await service.close();await service.engine.client.close()


async def test_failed_company_lookup_does_not_create_a_temporal_watch(database,users):
    service=agent_service(database,lookup_messages(reply='I could not check availability.'))
    fake_company(service);service.dining.call_company=AsyncMock(side_effect=ConnectionError('controlled'))
    try:
        uid=users[0]['id'];result=await submit_agent(service,uid,database.new_session(uid),'Check Carbone availability in New York on 2099-12-01 for 2.')
        assert 'could not be checked' in result['reply']
        assert not service.dining.client.requests and not database.rows('SELECT id FROM background_jobs')
    finally:await service.close();await service.engine.client.close()


async def test_temporal_start_retry_does_not_repeat_agent_lookup(database,users):
    service=agent_service(database);calls=fake_company(service)
    service.dining.client=SimpleNamespace(start_workflow=AsyncMock(side_effect=ConnectionError('controlled')))
    try:
        uid=users[0]['id'];await submit_agent(service,uid,database.new_session(uid),'Check Carbone availability in New York on 2099-12-01 for 2.')
        job=service.background.claim('dining_start');assert job
        service.dining.client=FakeTemporal();await service.dining.dispatch(job)
        assert len(service.dining.client.requests)==1 and len(calls)==1
        assert database.one('SELECT state FROM background_jobs')['state']=='complete'
        with pytest.raises(LookupError):
            await service.dining.search_for_agent(users[1]['id'],database.one('SELECT id FROM runs')['id'],None,'Carbone')
    finally:await service.close();await service.engine.client.close()


async def test_agent_can_cancel_the_users_dining_watch(database,users):
    service=agent_service(database,lookup_messages()+[
        AIMessage(content='',tool_calls=[{'name':'cancel_dining_watch','args':{'venue':'Carbone'},'id':'cancel-watch','type':'tool_call'}]),
        AIMessage(content='Stopped your Carbone watch.')])
    calls=fake_company(service)
    try:
        uid=users[0]['id'];sid=database.new_session(uid)
        await submit_agent(service,uid,sid,'Check Carbone availability in New York on 2099-12-01 for 2.')
        await submit_agent(service,uid,sid,'Stop my Carbone availability alerts.')
        assert database.one('SELECT status FROM dining_watches')['status']=='cancelled'
        assert len(calls)==1
    finally:await service.close();await service.engine.client.close()


def dining_discovery_provider():
    url='https://resy.com/cities/scottsdale-az/venues/peacock'
    return SimpleNamespace(output_text='Peacock Indian Bar & Grill is a candidate. A search snippet mentions 7 PM.',
        output=[SimpleNamespace(type='message',content=[SimpleNamespace(annotations=[
            SimpleNamespace(type='url_citation',url=url,title='Peacock Indian Bar & Grill')])])])


async def test_restaurant_discovery_must_be_followed_by_company_lookup(database,users):
    discover=AIMessage(content='',tool_calls=[{'name':'web_search','args':{
        'query':'Find Indian restaurants near Phoenix','official_domains':['resy.com']},'id':'discover','type':'tool_call'}])
    service=agent_service(database,[discover]+lookup_messages(venue='Peacock Indian Bar & Grill',location='Scottsdale',
        reply='Peacock: 18:30, 19:00, 19:30.'))
    calls=fake_company(service)
    decision=AsyncMock(return_value=ResearchDecision(requires_research=True,dining_availability=True))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=decision))
    service.engine.client.responses.create=AsyncMock(return_value=dining_discovery_provider())
    try:
        uid=users[0]['id'];result=await submit_agent(service,uid,database.new_session(uid),
            'Find Indian restaurants around Phoenix with tables on 2099-12-01 for 2.')
        assert service.engine.model.choices[:2]==[
            {'type':'function','function':{'name':'web_search'}},
            {'type':'function','function':{'name':'delegate_dining'}}]
        assert calls[0][1]['venue']=='Peacock Indian Bar & Grill'
        assert '18:30' in result['reply'] and not service.dining.client.requests
    finally:await service.close();await service.engine.client.close()


async def test_web_only_dining_times_are_blocked_even_if_model_skips_mcp(database,users):
    discover=AIMessage(content='',tool_calls=[{'name':'web_search','args':{
        'query':'Find Indian restaurants near Phoenix','official_domains':['resy.com']},'id':'discover','type':'tool_call'}])
    service=agent_service(database,[discover,AIMessage(content='Peacock has a confirmed 7:00 PM table for 2.\n')])
    calls=fake_company(service)
    decision=AsyncMock(return_value=ResearchDecision(requires_research=True,dining_availability=True))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=decision))
    service.engine.client.responses.create=AsyncMock(return_value=dining_discovery_provider())
    try:
        uid=users[0]['id'];result=await submit_agent(service,uid,database.new_session(uid),
            'Find Indian restaurants around Phoenix with a table for 2 on 2099-12-01 at 7 PM.')
        assert not calls and '7:00 PM' not in result['reply']
        assert "couldn't check dining availability" in result['reply']
        tokens=database.rows("SELECT payload FROM events WHERE run_id=? AND kind='token'",(result['run_id'],))
        assert all('confirmed 7:00 PM' not in row['payload'] for row in tokens)
    finally:await service.close();await service.engine.client.close()


def booking_messages():
    return [AIMessage(content='',tool_calls=[{'name':'browser_task','args':{
        'task':'Book Carbone New York for 2 on 2099-12-01 at 19:00 using https://carbonenewyork.com/.',
        'official_domains':['carbonenewyork.com']},'id':'book-table','type':'tool_call'}]),
        AIMessage(content='The booking flow is open.')]


@pytest.mark.parametrize('followup',['book now','reserve it','go ahead and book'])
async def test_booking_after_opening_uses_browser_without_another_company_check(database,users,followup):
    service=agent_service(database,lookup_messages()+booking_messages());calls=fake_company(service)
    decisions=AsyncMock(side_effect=[ResearchDecision(requires_research=False,dining_availability=True),
        ResearchDecision(requires_research=False,dining_booking=True)])
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=decisions))
    delegate=AsyncMock(return_value={'status':'verified','verification':{'success':True}})
    service.engine.browser_tasks=SimpleNamespace(delegate=delegate)
    try:
        uid=users[0]['id'];sid=database.new_session(uid)
        await submit_agent(service,uid,sid,'Check Carbone availability in New York on 2099-12-01 for 2.')
        await emit_opening(service)
        result=await submit_agent(service,uid,sid,followup)
        delegate.assert_awaited_once()
        assert '19:00' in delegate.call_args.args[3] and 'for 2' in delegate.call_args.args[3]
        assert len(calls)==1 and len(service.dining.client.requests)==1
        assert database.one('SELECT COUNT(*) n FROM dining_watches')['n']==1
        assert not database.rows("SELECT id FROM events WHERE run_id=? AND kind IN ('dining_required','dining_lookup')",(result['run_id'],))
        assert 'search_dining' not in service.engine.model.offered
        assert followup in str(decisions.call_args) and 'A table has opened up' in str(decisions.call_args)
    finally:await service.close();await service.engine.client.close()


async def test_booking_request_cannot_invoke_company_even_if_model_attempts_it(database,users):
    service=agent_service(database,lookup_messages()[:1]+booking_messages());calls=fake_company(service)
    decision=AsyncMock(return_value=ResearchDecision(requires_research=False,dining_availability=True,dining_booking=True))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=decision))
    service.engine.browser_tasks=SimpleNamespace(delegate=AsyncMock(return_value={'status':'verified','verification':{'success':True}}))
    try:
        uid=users[0]['id'];sid=database.new_session(uid)
        database.message(uid,sid,'user','Carbone New York, 2099-12-01, 19:00, for 2.')
        await submit_agent(service,uid,sid,'Book now')
        assert not calls and not service.dining.client.requests
        assert not database.rows('SELECT id FROM dining_watches')
        service.engine.browser_tasks.delegate.assert_awaited_once()
        messages=service.engine.model.seen[1]
        assert any(m.type=='tool' and json.loads(m.content).get('status')=='blocked' for m in messages)
    finally:await service.close();await service.engine.client.close()
