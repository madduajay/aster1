import json
import time
from collections import Counter
import pytest

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from availability.company import Stock
from availability.contracts import Task
from availability.presentation import compose,render
from availability.security import Security
from availability.subagent import Agent
from availability.trips import required_meals,dining_queries
from test_agent import ScriptedModel
from test_availability import service_for
from test_trip_presentation import seed


async def test_dining_inherits_trip_and_covers_all_meals_without_separate_dates(database,users):
    uid=users[0]['id'];sid=database.new_session(uid)
    database.message(uid,sid,'user','Dubai trip for 2 travelers, 1 room, December 1 to 10, 2099, with dining.')
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('meals-trip',?,?,'running',?)",(uid,sid,time.time()))
    database.event('meals-trip','trip_requested',{'criteria':{'start':'2099-12-01','end':'2099-12-10','location':'Dubai','party_size':2}})
    service=service_for(database,[AIMessage(content='Hi')])
    try:
        task=service.availability.envelope(uid,'meals-trip',[{'domain':'dining','criteria':{}}],'dining')
        assert len(task.queries)==30
        assert {q.criteria['date'] for q in task.queries}=={'2099-12-'+str(i).zfill(2) for i in range(1,11)}
        assert all(q.criteria['location']=='Dubai' and q.criteria['party_size']==2 for q in task.queries)
        assert {q.criteria['meal'] for q in task.queries}=={'breakfast','lunch','dinner'}
        database.event('meals-trip','availability_result',{'results':[{'domain':'dining','criteria':q.criteria,'status':'completed'} for q in task.queries if q.criteria['meal']!='breakfast']})
        retry=service.availability.envelope(uid,'meals-trip',[{'domain':'dining','criteria':{}}],'dining')
        assert len(retry.queries)==10 and all(q.criteria['meal']=='breakfast' for q in retry.queries)
    finally:await service.close();await service.engine.client.close()


@pytest.mark.parametrize('varied',[False,True])
async def test_meal_discovery_is_shared_by_date_and_company_checks_each_meal(tmp_path,monkeypatch,varied):
    research_calls=[];company_calls=[]
    async def research(client,domain,c,*a):
        research_calls.append(c['meal']);name=c['meal'].title()+' Venue'
        offset={'breakfast':0,'lunch':5,'dinner':10}[c['meal']]
        names=['Venue '+str(i) for i in range(offset,offset+10)] if varied else [name]
        return {'status':'completed','answer':name,'sources':[{'url':'https://example.com/meals','title':name}],
            'candidates':[{'name':n,'source_urls':['https://example.com/meals'],'evidence_quote':n} for n in names]}
    stock=Stock(tmp_path/'company.sqlite','dining',Security(tmp_path))
    async def company(security,domain,name,args):
        company_calls.append(args['criteria']);return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.research.search',research);monkeypatch.setattr('availability.subagent.call_company',company)
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query_index':0},'id':'web','type':'tool_call'}]),
        AIMessage(content='',tool_calls=[{'name':'search_dining','args':{'query_index':0},'id':'check','type':'tool_call'}]),AIMessage(content='Done')])
    agent=Agent('dining',tmp_path,model,InMemorySaver(),research_client=object())
    task=Task(request_id='trip-meals-request',user_id='user1',session_id='session1',source_message_id=1,
        queries=dining_queries({'start':'2099-12-01','end':'2099-12-10','location':'Dubai','party_size':2}))
    result=await agent.query(task)
    # Discovery is shared by meal, with one bounded supplement when its
    # source supplies only one venue, rather than searching for every date.
    assert Counter(research_calls)=={meal:1 if varied else 2 for meal in ('breakfast','lunch','dinner')} and len(company_calls)==30
    assert all(r['status']=='completed' for r in result['results'])
    slots={r['criteria']['meal']:r['options'][0]['times'] for r in result['results']}
    assert slots=={'breakfast':['08:00','08:30'],'lunch':['12:30','13:00'],'dinner':['19:00','19:30']}
    if varied:
        counts=Counter(c['venue'] for c in company_calls)
        assert len(counts)==20 and max(counts.values())<=2


def test_meals_follow_flight_boundaries_and_render_entertainment(database,users,tmp_path):
    assert required_meals('2099-12-03','2099-12-02T17:00:00','2099-12-04T14:00:00')==['breakfast','lunch','dinner']
    assert required_meals('2099-12-01','2099-12-02T17:00:00')==[]
    user=users[0]['id'];rid,days=seed(database,user,tmp_path)
    stock=Stock(tmp_path/'meals.sqlite','dining',Security(tmp_path));results=[]
    for day,meal in [('2099-12-02','dinner'),('2099-12-03','breakfast')]:
        results.append(stock.search('meal-'+meal,{'venue':meal.title()+' Venue','location':'Dubai','date':day,'party_size':2,'meal':meal}))
    database.event(rid,'availability_result',{'results':results})
    for day in days:day['dining']=[]
    assert compose(database,None,user,rid,days)['status']=='completed'
    plan=json.loads(database.one("SELECT payload FROM events WHERE kind='trip_plan'")['payload'])
    assert [r['criteria']['meal'] for r in plan['days'][1]['dining']]==['dinner']
    assert [r['criteria']['meal'] for r in plan['days'][2]['dining']]==['breakfast']
    text=render(plan)
    assert '**Breakfast:**' in text and '**Dinner:**' in text and '**Entertainment:**' in text and 'Dubai Museum' in text
