import json
import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
import pytest

from personal_agent.agent import Engine, ResearchDecision
from personal_agent.service import Service
from personal_agent.travel_planning import itinerary_details
from conftest import StubBrowsers
from test_agent import ScriptedModel


def test_known_destination_uses_disclosed_dates_instead_of_exploration():
    details=itinerary_details('Plan a trip to Dubai',date(2026,11,22),allow_defaults=True)
    assert details['start']=='2026-11-29' and details['end']=='2026-12-05'
    assert details['assumptions']=={'departure_date':'2026-11-29','return_date':'2026-12-05'}
    assert itinerary_details('Plan a trip somewhere',date(2026,11,22),allow_defaults=True) is None
    assert itinerary_details('Plan a trip to Dubai, flexible dates',date(2026,11,22),allow_defaults=True) is None


class StrictToolModel(ScriptedModel):
    """Match the provider's rejection of a forced tool absent from its schema."""
    offered_history:list=Field(default_factory=list)
    def bind_tools(self, tools, **kwargs):
        self.offered_history.append({t.name for t in tools})
        choice=kwargs.get('tool_choice')
        if isinstance(choice,dict):
            assert choice['function']['name'] in {t.name for t in tools}
        return super().bind_tools(tools,**kwargs)


def call(name,args,ident):
    return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':ident,'type':'tool_call'}])


async def test_composer_can_enable_dining_after_router_filtered_it_out(database,users,monkeypatch):
    user=users[0]['id'];sid=database.new_session(user)
    mid=database.message(user,sid,'user','Show a day-by-day trip to a beach destination.')
    run='itinerary-missing-dining'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
    travel=[{'domain':'hotels','criteria':{'destination':'Beach City','check_in':'2099-01-01','check_out':'2099-01-07'}}]
    dining=[{'domain':'dining','criteria':{'location':'Beach City','date':'2099-01-01'}}]
    model=StrictToolModel(responses=[call('delegate_travel',{'queries':travel},'travel'),
        call('compose_trip',{'days':[]},'compose1'),call('delegate_dining',{'queries':dining},'dining'),
        call('compose_trip',{'days':[]},'compose2'),AIMessage(content='Combined itinerary completed.')])
    route=AsyncMock(return_value=ResearchDecision(requires_research=False,trip_itinerary=True,travel_availability=True,dining_availability=False))
    router=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=route))
    engine=Engine(database,StubBrowsers(),InMemorySaver(),'sk-test',model=model,research_model=router)
    service=Service(database,engine);roles=[];compositions=[]
    async def delegate(user,run,queries,role,*args,**kwargs):
        roles.append(role)
        response={'results':[{'domain':queries[0]['domain'],'criteria':queries[0]['criteria'],'status':'completed','available':True,'options':[]}]}
        database.event(run,'availability_result',response)
        return response
    def compose(*args,**kwargs):
        compositions.append(1)
        if len(compositions)==1:return {'status':'needs_checks','category':'dining','message':'Meals need checks.'}
        database.event(run,'trip_plan',{'days':[]})
        return {'status':'completed'}
    engine.availability.delegate=delegate
    monkeypatch.setattr('availability.presentation.compose',compose)
    context={'session_id':sid,'current_user_message':{'id':mid,'content':'Show a day-by-day trip to a beach destination.'}}
    try:
        graph=engine.graph(user,run,context)
        state=await graph.ainvoke({'messages':[HumanMessage(content=json.dumps(context))]},
            {'configurable':{'thread_id':'itinerary-filtered-dining'},'recursion_limit':30})
        assert roles==['travel','dining'] and len(compositions)==2
        assert state['messages'][-1].content=='Combined itinerary completed.'
        forced=[c['function']['name'] for c in model.choices if isinstance(c,dict)]
        assert forced==['delegate_travel','compose_trip','delegate_dining','compose_trip']
        assert not database.one("SELECT id FROM events WHERE run_id=? AND kind='tool_routing_gap'",(run,))
    finally:await service.close();await engine.client.close()


@pytest.mark.parametrize('text,start,end,destination',[
    ('Plan a trip to Dubai from Phoenix, Dec 1st to Dec 10th, day by day.','2026-12-01','2026-12-10','Dubai'),
    ('Plan a trip to Tokyo January 19 to January 26, with an itinerary.','2027-01-19','2027-01-26','Tokyo'),
    ('Plan a trip to Paris Dec 28 to Jan 5, day-by-day.','2026-12-28','2027-01-05','Paris'),
    ('Plan a trip to Lisbon, 2099-03-01 to 2099-03-10.','2099-03-01','2099-03-10','Lisbon'),
])
def test_specific_itinerary_dates_and_destination_are_recovered(text,start,end,destination):
    details=itinerary_details(text,date(2026,11,22))
    assert details and details['start']==start and details['end']==end and details['destination']==destination


@pytest.mark.parametrize('text',[
    'Plan a trip somewhere warm; I do not know when.',
    'Plan a trip to Dubai; dates are flexible.',
    'Plan a trip to Dubai Dec 1 to Dec 10 as one option; compare date windows.',
])
def test_open_exploration_is_not_promoted_to_a_fixed_itinerary(text):
    assert itinerary_details(text,date(2026,11,22)) is None


async def test_fully_specified_itinerary_overrides_explore_and_delegates_before_research(database,users,monkeypatch):
    prompt=('Can you plan a trip to Dubai from Phoenix, 2 travelers, 1 room, Dec 1st to Dec 10th - '
            '20k budget, business class flights, 4 star hotels and great dining venues. '
            'Please show day by day dining and entertainment, with specific dining recommendations.')
    user=users[0]['id'];sid=database.new_session(user);mid=database.message(user,sid,'user',prompt)
    run='specific-dubai-itinerary'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
    travel=[{'domain':'hotels','criteria':{'destination':'Dubai','check_in':'2026-12-01','check_out':'2026-12-10','stars':4}}]
    dining=[{'domain':'dining','criteria':{'location':'Dubai','meal':'dinner'}}]
    model=StrictToolModel(responses=[AIMessage(content='',tool_calls=[
        {'name':'delegate_travel','args':{'queries':travel},'id':'travel','type':'tool_call'},
        {'name':'delegate_dining','args':{'queries':dining},'id':'dining','type':'tool_call'}]),
        call('web_search',{'query':'Dubai sightseeing','official_domains':['visitdubai.com']},'sights'),
        call('compose_trip',{'days':[]},'compose'),call('update_travel_plan',{'update':{}},'draft'),
        AIMessage(content='The requested Dubai itinerary is ready.')])
    # Reproduce the exact classifier mistake observed in production.
    route=AsyncMock(return_value=ResearchDecision(requires_research=True,travel_intent='explore'))
    router=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=route))
    engine=Engine(database,StubBrowsers(),InMemorySaver(),'sk-test',model=model,research_model=router)
    service=Service(database,engine);order=[]
    async def delegate(user,run,queries,role,*args,**kwargs):
        order.append(role)
        result={'results':[{'domain':queries[0]['domain'],'criteria':queries[0]['criteria'],'status':'completed','available':True,'options':[]}]}
        database.event(run,'availability_result',result)
        return result
    async def sightseeing(*args,**kwargs):
        order.append('sightseeing')
        return {'sources':[{'url':'https://visitdubai.com/','title':'Dubai'}],'attractions':[]}
    def compose(*args,**kwargs):
        database.event(run,'trip_plan',{'days':[]})
        return {'status':'completed'}
    engine.availability.delegate=delegate
    monkeypatch.setattr('availability.activities.search',sightseeing)
    monkeypatch.setattr('availability.presentation.compose',compose)
    monkeypatch.setattr('personal_agent.agent.clock_context',lambda *a,**k:{'date':'2026-11-22','timezone':'America/Phoenix'})
    context={'session_id':sid,'current_user_message':{'id':mid,'content':prompt}}
    try:
        graph=engine.graph(user,run,context)
        state=await graph.ainvoke({'messages':[HumanMessage(content=json.dumps(context))]},
            {'configurable':{'thread_id':run},'recursion_limit':30})
        assert set(order[:2])=={'travel','dining'} and order[2]=='sightseeing'
        assert model.offered_history[0]=={'delegate_travel','delegate_dining'}
        assert model.choices[0]=='required'
        event=json.loads(database.one("SELECT payload FROM events WHERE run_id=? AND kind='trip_requested'",(run,))['payload'])
        assert event['criteria']=={'start':'2026-12-01','end':'2026-12-10','location':'Dubai','party_size':2}
        assert json.loads(database.one("SELECT payload FROM events WHERE run_id=? AND kind='travel_intent'",(run,))['payload'])['intent']=='check_availability'
        assert state['messages'][-1].content=='The requested Dubai itinerary is ready.'
    finally:await service.close();await engine.client.close()
