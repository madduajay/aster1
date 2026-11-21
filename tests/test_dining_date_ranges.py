import json
import time
from collections import Counter

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from personal_agent import config
from availability.contracts import Task
from availability.company import Stock
from availability.presentation import availability_text
from availability.security import Security
from availability.subagent import Agent
from test_availability import service_for
from test_agent import ScriptedModel


def trip_context(db,service,uid,sid):
    source=db.message(uid,sid,'user','Check Paris hotels December 3 to 21, 2099 for four people.')
    c={'destination':'Paris','check_in':'2099-12-03','check_out':'2099-12-21','guests':4,'rooms':1}
    task=Task(request_id='previous-paris-trip',user_id=uid,session_id=sid,source_message_id=source,queries=[{'domain':'hotels','criteria':c}])
    result={'results':[{'domain':'hotels','criteria':c,'status':'completed','available':True,'options':[{'name':'Hotel Alpha'}]}]}
    service.availability.store.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task.request_id,uid,sid,'travel',task.model_dump_json(),json.dumps(result),time.time()))
    return source


@pytest.mark.parametrize('word,meals,count',[('lunch and dinner',{'lunch','dinner'},38),('lunch',{'lunch'},19)])
async def test_followup_covers_whole_stay_and_only_requested_meals(database,users,word,meals,count):
    uid=users[0]['id'];sid=database.new_session(uid);service=service_for(database,[AIMessage(content='Hi')])
    try:
        source=trip_context(database,service,uid,sid)
        database.message(uid,sid,'user','show me some dining venues in paris between those days - for '+word)
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('range-followup',?,?,'running',?)",(uid,sid,time.time()))
        task=service.availability.envelope(uid,'range-followup',[{'domain':'dining','criteria':{'location':'Paris','date':'2099-12-03','meal':'lunch'}}],'dining')
        assert len(task.queries)==count
        assert {q.criteria['date'] for q in task.queries}=={'2099-12-'+str(i).zfill(2) for i in range(3,22)}
        assert {q.criteria['meal'] for q in task.queries}==meals
        assert all(q.criteria['party_size']==4 for q in task.queries)
        assert not database.one("SELECT id FROM events WHERE run_id='range-followup' AND kind='trip_requested'")
        # Clearing context prevents old trips from being reused.
        database.execute('UPDATE sessions SET context_floor=? WHERE id=?',(source,sid))
        task=service.availability.envelope(uid,'range-followup',[{'domain':'dining','criteria':{'location':'Paris','date':'2099-12-03','meal':'lunch'}}],'dining')
        assert len(task.queries)==1
    finally:await service.close();await service.engine.client.close()


async def test_single_day_request_is_not_expanded_into_previous_trip(database,users):
    uid=users[0]['id'];sid=database.new_session(uid);service=service_for(database,[AIMessage(content='Hi')])
    try:
        trip_context(database,service,uid,sid)
        database.message(uid,sid,'user','Dinner in Paris on December 5, 2099 only')
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('one-day',?,?,'running',?)",(uid,sid,time.time()))
        task=service.availability.envelope(uid,'one-day',[{'domain':'dining','criteria':{'location':'Paris','date':'2099-12-05','meal':'dinner'}}],'dining')
        assert len(task.queries)==1 and task.queries[0].criteria['date']=='2099-12-05'
    finally:await service.close();await service.engine.client.close()


async def test_company_checks_and_rendering_cover_all_38_meals(database,users,tmp_path,monkeypatch):
    uid=users[0]['id'];sid=database.new_session(uid);service=service_for(database,[AIMessage(content='Hi')]);calls=[]
    try:
        trip_context(database,service,uid,sid)
        database.message(uid,sid,'user','show me some dining venues in paris between those days - for lunch and dinner')
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('all-meals',?,?,'running',?)",(uid,sid,time.time()))
        task=service.availability.envelope(uid,'all-meals',[{'domain':'dining','criteria':{'location':'Paris','date':'2099-12-03','meal':'lunch'}}],'dining')
        async def research(client,domain,c,*args):
            names=[c['meal']+' Venue '+str(i) for i in range(10)]
            return {'status':'completed','answer':' '.join(names),'sources':[{'url':'https://example.com/paris','title':'Paris meals'}],
                'candidates':[{'name':name,'source_urls':['https://example.com/paris'],'evidence_quote':name} for name in names]}
        stock=Stock(tmp_path/'company.sqlite','dining',Security(tmp_path))
        async def company(security,domain,name,args):
            calls.append(args['criteria']);return stock.search(args['query_id'],args['criteria'])
        monkeypatch.setattr('availability.research.search',research);monkeypatch.setattr('availability.subagent.call_company',company)
        model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'web_search','args':{'query_index':0},'id':'web','type':'tool_call'}]),AIMessage(content='',tool_calls=[{'name':'search_dining','args':{'query_index':0},'id':'check','type':'tool_call'}]),AIMessage(content='Done')])
        result=await Agent('dining',tmp_path,model,InMemorySaver(),research_client=object()).query(task)
        assert len(calls)==38 and all(r['status']=='completed' for r in result['results'])
        assert max(Counter(c['venue'] for c in calls).values())<=2
        text=availability_text([result])
        assert all('2099-12-'+str(i).zfill(2) in text for i in range(3,22))
        assert 'Breakfast' not in text
    finally:await service.close();await service.engine.client.close()
