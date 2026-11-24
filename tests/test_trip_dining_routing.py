import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from availability.gateway import Gateway
from availability.trips import dining_followup,trip_dining_request
from personal_agent.agent import ResearchDecision
from personal_agent.travel_planning import TravelPlans
from test_agent import wait_service
from test_availability import service_for


def call(name,args,ident):
    return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':ident,'type':'tool_call'}])


@pytest.mark.parametrize('legacy_hotel',[False,True])
@pytest.mark.parametrize('meals',['',' for lunch and dinner'])
async def test_trip_dining_overrides_prepare_and_directly_calls_dining(database,users,monkeypatch,legacy_hotel,meals):
    replies=[call('delegate_dining',{'queries':[{'domain':'dining','criteria':{'location':'Aruba'}}]},'dining'),
        call('update_travel_plan',{'update':{}},'save'),AIMessage(content='Here are the checked dining options for your trip.')]
    service=service_for(database,replies)
    router=AsyncMock(return_value=ResearchDecision(requires_research=True,travel_intent='prepare'))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router))
    user=users[0]['id'];sid=database.new_session(user)
    mid=database.message(user,sid,'user','Trip to Aruba for four travelers, January 19–21, 2099.')
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('prior-trip',?,?,'completed',?)",(user,sid,time.time()))
    stay={'domain':'hotels','criteria':{'destination':'Aruba','check_in':'2099-01-19','check_out':'2099-01-21','guests':4,'rooms':2}}
    if legacy_hotel:
        service.availability.store.execute('INSERT INTO tasks(id,user_id,session_id,role,envelope,result,updated) VALUES(?,?,?,?,?,?,?)',('legacy-trip',user,sid,'travel',
            json.dumps({'source_message_id':mid,'queries':[stay]}),json.dumps({'results':[{**stay,'status':'completed','available':False,'options':[]}]}),time.time()))
    else:database.event('prior-trip','trip_requested',{'source_message':mid,'criteria':{'location':'Aruba','start':'2099-01-19','end':'2099-01-21','party_size':4}})
    captured=[]
    async def delegate(gateway,user,run,queries,role,*args,**kwargs):
        assert role=='dining'
        task=gateway.envelope(user,run,queries,role);captured.append(task)
        result={'results':[{'domain':q.domain,'criteria':q.criteria,'status':'completed','available':False,'options':[]} for q in task.queries]}
        gateway.db.event(run,'availability_result',result)
        return result
    monkeypatch.setattr(Gateway,'delegate',delegate)
    try:
        run=(await service.submit(user,sid,'Show me some dining options also for the travel above'+meals))['run_id']
        await wait_service(service,run)
        assert database.one('SELECT status FROM runs WHERE id=?',(run,))['status']=='completed', database.rows("SELECT kind,payload FROM events WHERE run_id=? AND kind IN ('diagnostic','error')",(run,))
        assert service.engine.model.choices[0]=={'type':'function','function':{'name':'delegate_dining'}}
        assert len(captured)==1
        queries=captured[0].queries
        assert len(queries)==(6 if meals else 9)
        assert {q.criteria['date'] for q in queries}=={'2099-01-19','2099-01-20','2099-01-21'}
        assert {q.criteria['party_size'] for q in queries}=={4}
        assert {q.criteria['location'] for q in queries}=={'Aruba'}
        assert all(q.research for q in queries)
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='research_required'",(run,))
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='trip_plan'",(run,))
    finally:await service.close();await service.engine.client.close()


@pytest.mark.parametrize('text',[
    'Show me dining etiquette for the trip.', 'Find dining gear for my trip.',
    'Book dinner for the trip.', 'Cancel the restaurant booking for my trip.',
    'Show me restaurant recommendations in Phoenix.', 'Show me travel documents.',
    'Plan a trip to Aruba with hotels and show dining options.',
])
def test_other_requests_do_not_trigger_trip_dining_override(text):
    assert not trip_dining_request(text)


@pytest.mark.parametrize('boundary',['other_account','other_chat','context_clear','new_trip'])
async def test_legacy_dining_context_respects_ownership_and_trip_boundaries(database,users,boundary):
    service=service_for(database,[AIMessage(content='Hi')])
    user=users[0]['id'];sid=database.new_session(user)
    mid=database.message(user,sid,'user','Travel to Aruba.')
    stay={'domain':'hotels','criteria':{'destination':'Aruba','check_in':'2099-01-19','check_out':'2099-01-21','guests':4}}
    record_user=users[1]['id'] if boundary=='other_account' else user
    record_chat=database.new_session(user) if boundary=='other_chat' else sid
    service.availability.store.execute('INSERT INTO tasks(id,user_id,session_id,role,envelope,result,updated) VALUES(?,?,?,?,?,?,?)',
        ('old-stay',record_user,record_chat,'travel',json.dumps({'source_message_id':mid,'queries':[stay]}),json.dumps({'results':[]}),time.time()))
    if boundary=='context_clear':database.execute('UPDATE sessions SET context_floor=? WHERE id=?',(mid,sid))
    current=database.message(user,sid,'user','Show dining options for my travel above.')
    if boundary=='new_trip':TravelPlans(database).activate(user,sid,current,reset=True)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('new-dining',?,?,'running',?)",(user,sid,time.time()))
    try:assert not dining_followup(database,service.availability.store,user,'new-dining','Show dining options for my travel above.',[])
    finally:await service.close();await service.engine.client.close()
