import json
import time
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from availability.contracts import Task, WatchRequest, criteria
from availability.subagent import Agent
from personal_agent.agent import Engine, ResearchDecision
from personal_agent.db import Database
from personal_agent.memory import Memory
from personal_agent.service import Service
from personal_agent.travel_planning import DraftUpdate, TravelPlans, planning_disclosure, DEFAULTS


def test_itinerary_disclosure_labels_defaults_and_full_proposed_dates():
    result=planning_disclosure({'assumptions':DEFAULTS},
        {'departure_date':'2027-01-19','return_date':'2027-01-25'},
        {'start':'2027-01-19','end':'2027-01-25'})
    assert '**Planning defaults (assumed):**' in result
    assert 'economy cabin' in result and 'mid luxury (4-star) hotels' in result
    assert '2 travelers' in result and '1 room' in result
    assert '**Travel dates (proposed; assumed dates):** Jan 19, 2027 → Jan 25, 2027.' in result


def test_disclosure_does_not_call_explicit_choices_defaults():
    result=planning_disclosure({'assumptions':DEFAULTS},{},
        {'start':'2027-01-19','end':'2027-01-25'},
        '2 travelers, 1 room, business class, 5 star hotels')
    assert 'Planning defaults' not in result
    assert 'economy' not in result and '4-star' not in result
    assert '**Travel dates:** Jan 19, 2027 → Jan 25, 2027.' in result


def test_exploration_disclosure_keeps_dates_unselected_or_proposed():
    draft={'assumptions':DEFAULTS}
    assert '**Travel dates:** Not selected yet.' in planning_disclosure(draft,{})
    draft['windows']=[{'start_date':'2027-02-01','end_date':'2027-02-07'}]
    result=planning_disclosure(draft,{})
    assert 'Proposed date options (not confirmed)' in result
    assert 'Feb 01, 2027 → Feb 07, 2027' in result
from conftest import SummaryModel, StubBrowsers
from test_agent import ScriptedModel, ScriptedBrowser, wait_service
from test_availability import H, F, service_for

TODAY = date(2090, 1, 1)
URL = 'https://visitportugal.com/plan'
SOURCES = [{'url': URL, 'title': 'Portugal travel guide'}]


def call(name, args, ident):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': ident, 'type': 'tool_call'}])


def seed(database, users, text='I have two weeks, warm weather and good food; choose something.'):
    user = users[0]['id']; session = database.new_session(user)
    mid = database.message(user, session, 'user', text)
    plans = TravelPlans(database)
    plans.activate(user, session, mid)
    return plans, user, session, mid


def destination(name='Portugal'):
    return {'name': name, 'reason': 'Food and a relaxed pace', 'tradeoff': 'Cooler evenings', 'source_urls': [URL]}


def confirmed(field, value, mid, evidence):
    return {'field': field, 'value': value, 'source_message_id': mid, 'evidence': evidence}


def test_draft_survives_restart_is_scoped_and_does_not_promote_assumptions(database, users):
    plans, user, session, mid = seed(database, users)
    patch = DraftUpdate(confirmed=[confirmed('duration_days', 14, mid, 'two weeks')], destinations=[destination()])
    draft = plans.update(user, session, mid, patch, SOURCES, TODAY)
    assert draft['assumptions'] == {'cabin': 'economy', 'hotel_quality': 'mid luxury (4-star)', 'travelers': 2, 'rooms': 1}
    assert draft['confirmed']['duration_days']['value'] == 14
    reloaded = TravelPlans(Database(database.path)).load(user, session)
    assert reloaded == draft
    assert plans.load(user, database.new_session(user)) is None
    with pytest.raises(LookupError):
        plans.load(users[1]['id'], session)
    assert database.facts(user, inspect=True) == []


def test_draft_corrections_win_and_clear_stale_windows(database, users):
    plans, user, session, mid = seed(database, users, 'Portugal, two weeks.')
    windows = [{'start_date': '2090-05-01', 'end_date': '2090-05-14', 'label': 'best_overall_fit',
                'reason': 'Spring visit', 'source_urls': [URL]}]
    plans.update(user, session, mid, DraftUpdate(confirmed=[confirmed('duration_days', 14, mid, 'two weeks')],
                 destinations=[destination()], windows=windows), SOURCES, TODAY)
    correction = database.message(user, session, 'user', 'Actually, seven days in Japan.')
    draft = plans.update(user, session, correction, DraftUpdate(confirmed=[
        confirmed('duration_days', 7, correction, 'seven days'), confirmed('destination', 'Japan', correction, 'Japan')]), SOURCES, TODAY)
    assert not draft['destinations'] and not draft['windows'] and not draft['selected']
    draft = plans.update(user, session, correction, DraftUpdate(confirmed=[confirmed('duration_days', 14, mid, 'two weeks')]), SOURCES, TODAY)
    assert draft['confirmed']['duration_days']['value'] == 7


@pytest.mark.parametrize('invalid', ['foreign_source', 'assistant_source', 'false_quote', 'assumption_as_confirmed', 'no_selection', 'unknown_candidate', 'unverified_url', 'false_completion'])
def test_draft_rejects_ungrounded_data_atomically(database, users, invalid):
    plans, user, session, mid = seed(database, users, 'Show travel ideas.')
    before = plans.load(user, session)
    other_session = database.new_session(users[1]['id'])
    foreign = database.message(users[1]['id'], other_session, 'user', 'business')
    assistant = database.message(user, session, 'assistant', 'business')
    update = {
        'foreign_source': {'confirmed': [confirmed('cabin', 'business', foreign, 'business')]},
        'assistant_source': {'confirmed': [confirmed('cabin', 'business', assistant, 'business')]},
        'false_quote': {'confirmed': [confirmed('cabin', 'business', mid, 'I fly business')]},
        'assumption_as_confirmed': {'confirmed': [confirmed('cabin', 'economy', mid, 'travel ideas')]},
        'no_selection': {'destinations': [destination()], 'selected': {'destination': 'Portugal', 'source_message_id': mid, 'evidence': 'travel ideas'}},
        'unknown_candidate': {'selected': {'destination': 'Japan', 'source_message_id': mid, 'evidence': 'travel ideas'}},
        'unverified_url': {'destinations': [{**destination(), 'source_urls': ['https://invented.example/']}]},
        'false_completion': {'preparation': [{'category': 'forms', 'title': 'Submit visa application', 'status': 'completed'}]},
    }[invalid]
    with pytest.raises(ValueError):
        plans.update(user, session, mid, DraftUpdate.model_validate(update), SOURCES, TODAY)
    assert plans.load(user, session) == before


def test_context_clear_and_forgotten_evidence_hide_old_draft(database, users):
    plans, user, session, mid = seed(database, users)
    database.execute('UPDATE sessions SET context_floor=? WHERE id=?', (mid, session))
    assert plans.load(user, session) is None
    new = database.message(user, session, 'user', 'Start another trip.')
    assert plans.activate(user, session, new)['created_message_id'] == new
    database.execute("INSERT INTO context_exclusions(message_id,reason) VALUES(?,'forgotten')", (new,))
    assert plans.load(user, session) is None


@pytest.mark.parametrize('start,end,label', [
    ('2089-12-01', '2089-12-07', 'best_overall_fit'),
    ('2090-05-01', '2090-05-15', 'best_overall_fit'),
    ('2091-05-01', '2091-05-07', 'seasonal_fit'),
])
def test_windows_enforce_future_horizon_and_equal_duration(database, users, start, end, label):
    plans, user, session, mid = seed(database, users)
    with pytest.raises(ValueError):
        plans.update(user, session, mid, DraftUpdate(windows=[{'start_date': start, 'end_date': end,
            'label': label, 'reason': 'Season', 'source_urls': [URL]}]), SOURCES, TODAY)


def test_choose_and_flexible_windows_remain_provisional(database, users):
    plans, user, session, mid = seed(database, users)
    patch = DraftUpdate(destinations=[destination()], selected={'destination': 'Portugal', 'source_message_id': mid, 'evidence': 'choose something'})
    draft = plans.update(user, session, mid, patch, SOURCES, TODAY)
    assert draft['selected']['destination'] == 'Portugal'
    assert 'destination' not in draft['confirmed']
    draft = plans.update(user, session, mid, DraftUpdate(windows=[{'start_date': '2090-05-01', 'end_date': '2090-05-07',
        'label': 'best_overall_fit', 'reason': 'Spring', 'source_urls': [URL]}]), SOURCES, TODAY)
    assert 'window_label' not in draft['selected'] and not draft['confirmed']


async def run_planning(database, users, intent, user_text, responses, **decision_flags):
    model = ScriptedModel(responses=responses)
    router_call = AsyncMock(return_value=ResearchDecision(requires_research=intent not in {'none','execute'}, travel_intent=intent, **decision_flags))
    router = SimpleNamespace(with_structured_output=lambda *a, **k: SimpleNamespace(ainvoke=router_call))
    found = SimpleNamespace(output_text='Portugal, Japan and Costa Rica are candidates; dates/prices need research.',
        output=[SimpleNamespace(type='message', content=[SimpleNamespace(annotations=[SimpleNamespace(type='url_citation', **s) for s in SOURCES])])])
    engine = Engine(database, StubBrowsers(), InMemorySaver(), 'sk-test', model=model, research_model=router,
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=found))))
    service = Service(database, engine); service.memory = Memory(database, SummaryModel())
    uid = users[0]['id']; sid = database.new_session(uid)
    run = (await service.submit(uid, sid, user_text))['run_id']
    await wait_service(service, run)
    return service, model, router_call, uid, sid, run


@pytest.mark.parametrize('itinerary',[False,True])
async def test_undated_trip_researches_chooses_and_shares_dates_before_inventory(database,users,monkeypatch,itinerary):
    from availability.gateway import Gateway
    monkeypatch.setattr('personal_agent.agent.clock_context',lambda:{'date':TODAY.isoformat()})
    captured=[]
    async def check(gateway,user,run,queries,role,*args,**kwargs):
        task=gateway.envelope(user,run,queries,role)
        captured.append(task)
        result={'results':[{'domain':q.domain,'criteria':q.criteria,'status':'completed','available':False,'options':[]} for q in task.queries]}
        gateway.db.event(run,'availability_result',result)
        return result
    monkeypatch.setattr(Gateway,'delegate',check)
    window={'start_date':'2090-05-01','end_date':'2090-05-07','label':'best_overall_fit',
            'reason':'Suitable sunny season according to the tourism guide','source_urls':[URL]}
    queries=[{'domain':'flights','criteria':{'origin':'Phoenix','destination':'Portugal'}},
             {'domain':'hotels','criteria':{'destination':'Portugal'}}]
    sightseeing=AsyncMock(return_value={'answer':'Lisbon sights','sources':SOURCES,'attractions':[{'name':'Belém Tower','source_url':URL,'period':'morning'}]})
    monkeypatch.setattr('availability.activities.search',sightseeing)
    def compose(db,gateway,user,run,*args,**kwargs):
        db.event(run,'trip_plan',{})
        return {'status':'completed'}
    monkeypatch.setattr('availability.presentation.compose',compose)
    monkeypatch.setattr('availability.presentation.render',lambda plan:'A sourced itinerary.')
    replies=[call('web_search',{'query':'Portugal best sunny travel season','official_domains':['visitportugal.com']},'season'),
        call('update_travel_plan',{'update':{'windows':[window]}},'dates'),
        call('delegate_travel',{'queries':queries},'inventory')]
    if itinerary:replies += [call('web_search',{'query':'Lisbon sights','official_domains':['visitportugal.com']},'sights'),call('compose_trip',{'days':[]},'compose')]
    replies += [AIMessage(content='I chose May 1–7 for seasonal fit; you can change these proposed dates. No matching inventory was returned.')]
    service,model,router,user,sid,run=await run_planning(database,users,'check_availability','Phoenix AZ',replies,
        trip_destination='Portugal',travel_availability=True,trip_itinerary=itinerary)
    try:
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        assert [c['function']['name'] for c in model.choices[:3]]==['web_search','update_travel_plan','delegate_travel']
        assert len(captured)==1
        flight,hotel=captured[0].queries
        assert flight.criteria['departure_date']==hotel.criteria['check_in']=='2090-05-01'
        assert flight.criteria['return_date']==hotel.criteria['check_out']=='2090-05-07'
        assert flight.criteria['cabin']=='economy'
        draft=TravelPlans(database).load(user,sid)
        assert 'start_date' not in draft['confirmed'] and 'window_label' not in draft['selected']
        reply=database.rows("SELECT content FROM messages WHERE session_id=? AND role='assistant'",(sid,))[-1]['content']
        assert 'proposed; assumed dates' in reply and 'May 01, 2090 → May 07, 2090' in reply
        assert 'Please provide departure date' not in reply
        assert sightseeing.await_count==int(itinerary)
        if itinerary:assert [c['function']['name'] for c in model.choices[:5]]==['web_search','update_travel_plan','delegate_travel','web_search','compose_trip']
    finally:await service.close()


def test_proposed_window_survives_restart_and_explicit_correction_clears_it(database,users):
    plans,user,sid,mid=seed(database,users,'Choose Portugal for a week.')
    plans.update(user,sid,mid,DraftUpdate(windows=[{'start_date':'2090-05-01','end_date':'2090-05-07',
        'label':'best_overall_fit','reason':'Seasonal fit','source_urls':[URL]}]),SOURCES,TODAY)
    assert plans.propose_window(user,sid,mid,'Portugal',TODAY)['start']=='2090-05-01'
    draft=TravelPlans(Database(database.path)).load(user,sid)
    assert 'proposed; assumed dates' in planning_disclosure(draft,{})
    assert not draft['confirmed']
    correction=database.message(user,sid,'user','Actually start on 2090-06-01.')
    draft=plans.update(user,sid,correction,DraftUpdate(confirmed=[confirmed('start_date','2090-06-01',correction,'2090-06-01')]),[],TODAY)
    assert not draft['windows'] and 'departure_date' not in draft['assumptions']


@pytest.mark.parametrize('intent,text', [('explore', 'Somewhere warm with good food for two weeks. Just choose.'),
                                       ('compare', 'I can take two weeks off; show options for Japan without exact dates.')])
async def test_loose_requests_research_before_questions_without_availability(database, users, intent, text):
    replies = [call('web_search', {'query': text, 'official_domains': ['visitportugal.com']}, 'research'),
               call('update_travel_plan', {'update': {'destinations': [destination()]}}, 'save'),
               AIMessage(content='My pick is [Portugal](https://visitportugal.com/plan). Assuming economy, midrange hotels, two travelers and one room. What airport would you leave from?')]
    service, model, router, uid, sid, run = await run_planning(database, users, intent, text, replies,
        trip_itinerary=True, travel_availability=True, dining_availability=True)
    try:
        assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'
        assert model.choices[0]['function']['name'] == 'web_search'
        assert not {'compose_trip', 'delegate_dining'} & set(model.offered)
        if intent == 'explore':assert 'delegate_travel' not in model.offered
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind IN ('trip_requested','availability_required','availability_pending')", (run,))
        draft = TravelPlans(database).load(uid, sid)
        assert draft['destinations'][0]['name'] == 'Portugal' and not draft['confirmed']
        assert 'travel_draft' in json.loads(router.call_args.args[0][-1].content)
        metrics = json.loads(database.one("SELECT payload FROM events WHERE run_id=? AND kind='travel_response_metrics'", (run,))['payload'])
        assert metrics['has_options'] and not metrics['clarification_only'] and metrics['seconds_to_first_options'] >= 0
        assert not service.availability.store.rows('SELECT * FROM subscriptions')
    finally:await service.close()


async def test_unrelated_turn_does_not_expose_update_tool_or_resume_draft(database, users):
    service, model, router, uid, sid, run = await run_planning(database, users, 'none', 'What is 2+2?', [AIMessage(content='4')])
    try:
        assert 'update_travel_plan' not in model.offered
        assert TravelPlans(database).load(uid, sid) is None
        assert database.rows("SELECT content FROM messages WHERE session_id=? AND role='assistant'", (sid,))[-1]['content']=='4'
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='availability_pending'", (run,))
    finally:await service.close()


async def test_prepare_reuses_research_and_saves_checklist_without_execution(database, users):
    replies = [call('web_search', {'query': 'Portugal travel preparation', 'official_domains': ['visitportugal.com']}, 'research'),
               call('update_travel_plan', {'update': {'preparation': [{'category': 'gear', 'title': 'Check existing walking shoes'},
                    {'category': 'documents', 'title': 'Check official entry requirements'}]}}, 'save'),
               AIMessage(content='Start with your existing walking shoes and check official entry requirements; I have not submitted anything.')]
    service, model, router, uid, sid, run = await run_planning(database, users, 'prepare', 'Help prepare for Portugal.', replies)
    try:
        assert len(TravelPlans(database).load(uid, sid)['preparation']) == 2
        assert not {'delegate_travel', 'delegate_dining', 'compose_trip'} & set(model.offered)
        assert not service.availability.store.rows('SELECT * FROM monitors')
    finally:await service.close()


async def test_compare_without_origin_does_not_turn_into_required_inventory_questions(database,users):
    replies=[call('web_search',{'query':'Portugal seasons','official_domains':['visitportugal.com']},'search'),
             call('delegate_travel',{'queries':[{'domain':'flights','criteria':{'destination':'Portugal'}}]},'incomplete'),
             call('update_travel_plan',{'update':{'destinations':[destination()]}},'draft'),
             AIMessage(content='[Portugal](https://visitportugal.com/plan) remains my pick. Spring and autumn are candidate seasons; flight prices need a departure airport. Where would you leave from?')]
    service,model,router,user,sid,run=await run_planning(database,users,'compare','Portugal sounds good, but I do not know when.',replies)
    try:
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='availability_pending'",(run,))
        messages=[m.content for m in model.seen[-1] if m.type=='tool']
        assert any('planning_gap' in content for content in messages)
        reply=database.rows("SELECT content FROM messages WHERE session_id=? AND role='assistant'",(sid,))[-1]['content']
        assert 'Portugal' in reply and 'Please provide' not in reply
    finally:await service.close()


async def test_observe_only_cannot_create_any_watch_schedule_or_workflow(database, users):
    service = service_for(database, [AIMessage(content='Hi')]); gateway = service.availability
    user = users[0]['id']; sid = database.new_session(user)
    mid = database.message(user, sid, 'user', 'Compare Fifth Avenue hotel options in New York.')
    run = 'observe-only-request'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)", (run, user, sid, time.time()))
    database.event(run, 'travel_intent', {'intent': 'compare'})
    gateway.check = AsyncMock(return_value={'results': [{'domain': 'hotels', 'criteria': H, 'status': 'completed',
        'available': False, 'query_id': 'av-unavailable'}], 'research': []})
    gateway.parent = AsyncMock(side_effect=AssertionError('No Temporal workflows'))
    gateway.create_monitor = AsyncMock(side_effect=AssertionError('No Temporal schedules'))
    try:
        result = await gateway.delegate(user, run, [{'domain': 'hotels', 'criteria': H}], 'travel')
        assert result['watch_policy'] == 'observe_only'
        assert gateway.check.call_args.args[0].watch_policy == 'observe_only'
        gateway.parent.assert_not_awaited(); gateway.create_monitor.assert_not_awaited()
        for table in ['subscriptions', 'monitors', 'delivery', 'changes']:
            assert not gateway.store.rows('SELECT * FROM '+table)
        assert not database.rows('SELECT * FROM dining_watches')
        with pytest.raises(ValueError):
            await gateway.delegate(user, run, [{'domain': 'hotels', 'criteria': H}], 'travel', monitor=True)
        task = Task(request_id='observe-task', user_id=user, session_id=sid, source_message_id=mid,
            queries=[{'domain': 'hotels', 'criteria': H}], watch_policy='observe_only')
        gateway.store.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', (task.request_id, user, sid, 'travel', task.model_dump_json(), json.dumps(result), time.time()))
        assert gateway.pending(user, sid) == [] and gateway.recent(user, sid) == []
        agent = Agent('travel', database.path.parent, ScriptedModel(responses=[AIMessage(content='Hi')]), InMemorySaver())
        with pytest.raises(ValueError):
            await agent.watch(WatchRequest(request_id=task.request_id, user_id=user, parent_workflow_id='av-main-test'))
    finally:await service.close();await service.engine.client.close()


def test_provider_validation_stays_strict_and_envelope_defaults_cabin(database, users):
    assert 'cabin' in criteria('flights', {})[1]
    # Gateway normalizes planning defaults before crossing the strict provider boundary.
    from availability.gateway import Gateway
    db = database; user = users[0]['id']; sid = db.new_session(user)
    db.message(user, sid, 'user', 'Check flights from JFK to Dubai on December 1, 2099.')
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('default-cabin',?,?,'running',?)", (user, sid, time.time()))
    gateway = Gateway(SimpleNamespace(db=db))
    query = gateway.envelope(user, 'default-cabin', [{'domain': 'flights', 'criteria': {k:v for k,v in F.items() if k!='cabin'}}], 'travel').queries[0]
    assert query.criteria['cabin'] == 'economy'
    assert not criteria('flights', query.criteria)[1]
    source=db.message(user,sid,'user','I always prefer business class for flights.')
    db.put_fact(user,'I always prefer business class for flights.',source,'preference.flight.cabin',evidence='I always prefer business class for flights.')
    db.message(user,sid,'user','Check flights from JFK to Dubai on December 1, 2099.')
    query=gateway.envelope(user,'default-cabin',[{'domain':'flights','criteria':{k:v for k,v in F.items() if k!='cabin'}}],'travel').queries[0]
    assert query.criteria['cabin']=='economy'


def test_session_cabin_choice_wins_over_permanent_preference(database,users):
    plans,user,sid,mid=seed(database,users,'For this trip I want premium economy.')
    plans.update(user,sid,mid,DraftUpdate(confirmed=[confirmed('cabin','premium economy',mid,'premium economy')]),[],TODAY)
    database.put_fact(user,'I normally prefer business class.',mid,'preference.flight.cabin',evidence='I normally prefer business class.')
    database.message(user,sid,'user','Check flights from JFK to Dubai on December 1, 2099.')
    run='session-cabin-choice'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
    database.event(run,'travel_intent',{'intent':'check_availability'})
    from availability.gateway import Gateway
    gateway=Gateway(SimpleNamespace(db=database))
    query=gateway.envelope(user,run,[{'domain':'flights','criteria':{k:v for k,v in F.items() if k!='cabin'}}],'travel').queries[0]
    assert query.criteria['cabin']=='premium economy'


def test_legacy_pending_flight_no_longer_requests_cabin(database,users):
    from availability.gateway import Gateway
    user=users[0]['id'];sid=database.new_session(user)
    mid=database.message(user,sid,'user','Check flights from Phoenix to Mexico.')
    gateway=Gateway(SimpleNamespace(db=database))
    raw={k:v for k,v in F.items() if k!='cabin'}
    task=Task(request_id='legacy-flight-request',user_id=user,session_id=sid,source_message_id=mid,queries=[{'domain':'flights','criteria':raw}])
    result={'results':[{'domain':'flights','criteria':raw,'status':'needs_input','missing_fields':['cabin']}]}
    gateway.store.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task.request_id,user,sid,'travel',task.model_dump_json(),json.dumps(result),time.time()))
    for records in (gateway.pending(user,sid),gateway.recent(user,sid)):
        assert records[0]['criteria']['cabin']=='economy'
        assert records[0]['status']=='ready_for_check' and records[0]['missing_fields']==[]
    assert json.loads(gateway.store.one('SELECT result FROM tasks')['result'])==result


async def test_trip_form_execution_keeps_browser_approval_without_dining_delegation(database,users):
    user=users[0]['id'];sid=database.new_session(user);browser=ScriptedBrowser()
    model=ScriptedModel(responses=[call('browser_task',{'task':'Fill the requested trip arrival form at https://example.com/arrival','official_domains':['example.com']},'form'),
        call('update_travel_plan',{'update':{'preparation':[{'category':'forms','title':'Arrival form','status':'requested'}]}},'draft'),
        AIMessage(content='The approved form action completed.')])
    route=AsyncMock(return_value=ResearchDecision(requires_research=False,travel_intent='execute',shopping_recommendations=True,shopping_browser_first=True))
    router=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=route))
    engine=Engine(database,StubBrowsers(),InMemorySaver(),'sk-test',model=model,research_model=router,browser_tasks=browser)
    service=Service(database,engine);service.memory=Memory(database,SummaryModel())
    try:
        run=(await service.submit(user,sid,'Fill my arrival form for this trip.'))['run_id'];await wait_service(service,run)
        paused=service.active(user)
        assert paused['status']=='approval' and not browser.completed
        assert 'do not reinterpret it as a dining booking' in str(model.seen[0])
        assert not {'delegate_dining','delegate_travel'} & set(model.offered)
        await service.resume(user,run,'approve',paused['approval_id']);await wait_service(service,run)
        assert len(browser.completed)==1
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed'
        assert not service.availability.store.rows('SELECT * FROM subscriptions')
    finally:await service.close();await engine.client.close()


async def test_empty_research_explains_gap_without_inventing_options(database, users):
    replies = [call('web_search', {'query': 'Portugal travel', 'official_domains': ['visitportugal.com']}, 'search'),
               call('update_travel_plan', {'update': {}}, 'save'), AIMessage(content='I could not verify travel windows or prices.')]
    model=ScriptedModel(responses=replies)
    router=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=AsyncMock(return_value=ResearchDecision(requires_research=True,travel_intent='compare'))))
    create=AsyncMock(return_value=SimpleNamespace(output_text='No verified evidence.',output=[]))
    engine=Engine(database,StubBrowsers(),InMemorySaver(),'sk-test',model=model,research_model=router,
                  client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    service=Service(database,engine);service.memory=Memory(database,SummaryModel())
    uid=users[0]['id'];sid=database.new_session(uid)
    try:
        run=(await service.submit(uid,sid,'Compare travel dates for Portugal.'))['run_id'];await wait_service(service,run)
        create.assert_awaited_once()
        assert not TravelPlans(database).load(uid, sid)['windows']
        assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'
        reply=database.rows("SELECT content FROM messages WHERE session_id=? AND role='assistant'",(sid,))[-1]['content']
        assert "couldn't verify" in reply and '$' not in reply
    finally:await service.close()


async def test_followup_resumes_saved_draft_after_unrelated_turn(database,users):
    first=[call('web_search',{'query':'Portugal trips','official_domains':['visitportugal.com']},'search'),
           call('update_travel_plan',{'update':{'destinations':[destination()]}},'draft'),
           AIMessage(content='My pick is [Portugal](https://visitportugal.com/plan). Assuming economy and midrange hotels. What airport would you leave from?')]
    service,model,router,user,sid,run=await run_planning(database,users,'explore','Choose somewhere warm for a trip.',first)
    original=TravelPlans(database).load(user,sid)
    try:
        router.return_value=ResearchDecision(requires_research=False,travel_intent='none')
        model.responses=[AIMessage(content='4')];model.i=0
        unrelated=(await service.submit(user,sid,'What is 2+2?'))['run_id'];await wait_service(service,unrelated)
        assert TravelPlans(database).load(user,sid)==original
        assert 'update_travel_plan' not in model.offered
        next_mid=database.one('SELECT MAX(id) n FROM messages')['n']+1
        router.return_value=ResearchDecision(requires_research=True,travel_intent='compare')
        model.responses=[call('web_search',{'query':'Portugal seasons','official_domains':['visitportugal.com']},'seasons'),
                         call('update_travel_plan',{'update':{'selected':{'destination':'Portugal','source_message_id':next_mid,'evidence':'Portugal sounds good'}}},'select'),
                         AIMessage(content='Portugal is selected for planning; I will keep dates flexible. No booking has been made.')];model.i=0
        followup=(await service.submit(user,sid,'Portugal sounds good; dates are flexible.'))['run_id'];await wait_service(service,followup)
        draft=TravelPlans(database).load(user,sid)
        assert draft['selected']['destination']=='Portugal'
        assert draft['destinations']==original['destinations']
        received=json.loads(router.call_args.args[0][-1].content)['travel_draft']
        assert received['destinations'][0]['name']=='Portugal'
        assert not service.availability.store.rows('SELECT * FROM monitors')
    finally:await service.close()
