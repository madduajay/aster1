import json
import time

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from availability.targets import literal_hotels
from availability.contracts import Task
from availability.company import Stock
from availability.security import Security
from availability.subagent import Agent
from test_availability import H,F,service_for,model_for


OYO='hey find availability for oyo time square hotel new york dec 1st to dec 3rd for 2 people, 1 room'


@pytest.mark.parametrize('proposed',[{}, {'hotel':'OYO Times Square'}, {'hotel':'Four Seasons Hotel New York'}])
async def test_explicit_user_target_survives_missing_normalized_or_wrong_model_name(database,users,proposed):
    uid=users[0]['id'];sid=database.new_session(uid);service=service_for(database,[AIMessage(content='Hi')])
    try:
        source=database.message(uid,sid,'user',OYO)
        database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('oyo-binding',?,?,'running',?)",(uid,sid,time.time()))
        c={k:v for k,v in H.items() if k!='hotel'};c.update(proposed)
        task=service.availability.envelope(uid,'oyo-binding',[{'domain':'hotels','criteria':c}],'travel')
        assert task.requested_hotels==['oyo time square hotel new york']
        assert task.queries[0].criteria['hotel']==task.requested_hotels[0]
    finally:await service.close();await service.engine.client.close()


@pytest.mark.parametrize('source,candidates,expected',[
    (OYO,['OYO Times Square'],['oyo time square hotel new york']),
    ('Actually use OYO time square hotel instead',['OYO Times Square'],['OYO time square hotel']),
    ('Find availability for cheap hotels in New York December 1',['cheap hotels'],[]),
    ('Find availability for New York hotels December 1',['New York'],[]),
    ('Find availability for Best Western hotel in New York December 1',[],['Best Western hotel in New York']),
    ('Check Hyatt Grand Central and OYO Times Square',['Hyatt Grand Central','OYO Times Square'],['Hyatt Grand Central','OYO Times Square']),
    ('Recommend New York hotels',['Hyatt Grand Central'],[]),
    ('Find Dubai hotels',['Dubai hotel'],[]),
    ('Plan Dubai with 4-star hotels',['4-star hotels'],[]),
    ('Show alternatives to OYO Times Square in New York',['OYO Times Square'],[]),
    ('No alternatives to OYO Times Square hotel; check it',['OYO Times Square hotel'],['OYO Times Square hotel']),
    ('Check hotel availability in Tokyo from 2099-12-01 to 2099-12-03',[],[])])
def test_targets_must_be_specific_literal_user_text(source,candidates,expected):
    assert literal_hotels(source,candidates)==expected


@pytest.mark.parametrize('source,expected',[
    ('Find availability for Marriot Marquis New York December 1 to 3',['Marriot Marquis New York']),
    ('Find availability for OYO time square New York December 1 to 3',['OYO time square New York']),
    ('Find availability for Dubai hotels December 1 to 3',[]),
    ('Use that one instead',[])])
def test_known_hotel_requests_preserve_names_even_without_the_word_hotel(source,expected):
    assert literal_hotels(source,hotel_domain=True)==expected


@pytest.mark.parametrize('city',['Paris','Tokyo','Sydney','Cape Town','Rio de Janeiro','New York'])
def test_dining_instructions_inside_trip_prompt_are_never_a_hotel_name(city):
    source=f'Plan a trip to {city}, 4-star hotels. Show hotel and flight availability. Use varied dining venues; at most two visits to the same venue. Infer dining dates and party size from the trip.'
    assert literal_hotels(source,hotel_domain=True)==[]


def test_standalone_dining_followup_is_not_a_hotel_name():
    assert literal_hotels('Use varied dining venues',hotel_domain=True)==[]


@pytest.mark.parametrize('hotel',[None,'Four Seasons Hotel New York'])
async def test_subagent_refuses_generic_replacement_and_preserves_independent_flight(tmp_path,monkeypatch,hotel):
    calls=[];stock=Stock(tmp_path/'company.sqlite','flights',Security(tmp_path))
    async def company(security,domain,name,args):
        calls.append(domain);return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    agent=Agent('travel',tmp_path,model_for(1),InMemorySaver())
    task=Task(request_id='bound-property-task',user_id='user1',session_id='session1',source_message_id=1,
        requested_hotels=['OYO time square hotel new york'],queries=[{'domain':'hotels','criteria':{**H,'hotel':hotel}},{'domain':'flights','criteria':{**F,'cabin':'business'}}])
    result=await agent.query(task)
    assert calls==['flights']
    assert result['results'][0]['status']=='error' and result['results'][1]['available']


async def test_old_request_envelopes_without_target_field_remain_compatible(tmp_path,monkeypatch):
    stock=Stock(tmp_path/'company.sqlite','hotels',Security(tmp_path))
    async def company(security,domain,name,args):return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    task=Task(request_id='old-property-task',user_id='user1',session_id='session1',source_message_id=1,queries=[{'domain':'hotels','criteria':H}])
    agent=Agent('travel',tmp_path,model_for(0),InMemorySaver());await agent.query(task)
    old=task.model_dump();old.pop('requested_hotels')
    agent.store.execute('UPDATE tasks SET envelope=? WHERE id=?',(json.dumps(old),task.request_id))
    restored=Agent('travel',tmp_path,model_for(0),InMemorySaver())
    assert (await restored.query(task))['results'][0]['status']=='completed'
