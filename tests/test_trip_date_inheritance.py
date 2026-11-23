import json
import time
from types import SimpleNamespace

import pytest

from availability.gateway import Gateway
from availability.trips import active_scope
from personal_agent.travel_planning import TravelPlans

START='2099-01-19'
END='2099-01-26'


def setup(database,users,text='Combine hotels, flights and dining for the trip.'):
    user=users[0]['id'];sid=database.new_session(user)
    mid=database.message(user,sid,'user',text)
    run='trip-date-request'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
    gateway=Gateway(SimpleNamespace(db=database))
    return gateway,user,sid,run,mid


def trip(database,run,mid,destination='Aruba'):
    database.event(run,'trip_requested',{'source_message':mid,'criteria':{
        'start':START,'end':END,'location':destination,'party_size':2}})


def hotel(start=START,end=END,destination='Aruba'):
    return {'domain':'hotels','criteria':{'destination':destination,'check_in':start,'check_out':end}}


def flight(**overrides):
    return {'domain':'flights','criteria':{'origin':'Phoenix','destination':'Aruba',**overrides}}


@pytest.mark.parametrize('prompt,expected',[
    ('Find hotels in Aruba.',4),
    ('Find 5 star hotels in Aruba.',5),
    ('Find 3 star hotels in Aruba.',3),
])
def test_hotel_quality_defaults_to_four_stars_unless_explicit(database,users,prompt,expected):
    gateway,user,sid,run,mid=setup(database,users,prompt)
    task=gateway.envelope(user,run,[hotel()],'travel')
    assert task.queries[0].criteria['stars']==expected


def test_named_hotel_has_no_default_star_filter(database,users):
    gateway,user,sid,run,mid=setup(database,users,'Check the Ritz Carlton Aruba hotel.')
    task=gateway.envelope(user,run,[{'domain':'hotels','criteria':{**hotel()['criteria'],'hotel':'Ritz Carlton Aruba'}}],'travel')
    assert 'Ritz Carlton Aruba' in task.queries[0].criteria['hotel']
    assert 'stars' not in task.queries[0].criteria


def test_flight_inherits_selected_trip_start_before_hotel_arrival_date(database,users):
    gateway,user,sid,run,mid=setup(database,users)
    trip(database,run,mid)
    task=gateway.envelope(user,run,[flight(),hotel(start='2099-01-20')],'travel')
    c=task.queries[0].criteria
    assert c['departure_date']==START and c['return_date']==END
    assert task.queries[1].criteria['check_in']=='2099-01-20'


def test_combined_hotel_and_flight_query_can_share_one_unambiguous_range(database,users):
    gateway,user,sid,run,mid=setup(database,users)
    task=gateway.envelope(user,run,[hotel(),flight()],'travel')
    assert task.queries[1].criteria['departure_date']==START
    assert task.queries[1].criteria['return_date']==END


def test_explicit_flight_dates_and_one_way_take_precedence(database,users):
    gateway,user,sid,run,mid=setup(database,users,'Check one-way flights and hotels.')
    trip(database,run,mid)
    task=gateway.envelope(user,run,[flight(departure_date='2099-01-18'),hotel()],'travel')
    assert task.queries[0].criteria['departure_date']=='2099-01-18'
    assert 'return_date' not in task.queries[0].criteria


@pytest.mark.parametrize('mode',['different_destination','multiple_windows'])
def test_dates_do_not_leak_across_destinations_or_ambiguous_windows(database,users,mode):
    gateway,user,sid,run,mid=setup(database,users)
    queries=[hotel(destination='Tokyo'),flight()] if mode=='different_destination' else [hotel(),hotel('2099-05-02','2099-05-09'),flight()]
    task=gateway.envelope(user,run,queries,'travel')
    assert 'departure_date' not in task.queries[-1].criteria


def followup(database,user,sid,old,content):
    database.execute("UPDATE runs SET status='completed' WHERE id=?",(old,))
    mid=database.message(user,sid,'user',content)
    run='trip-date-followup'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(run,user,sid,time.time()))
    return run,mid


def test_flight_followup_reuses_latest_trip_dates_without_reasking(database,users):
    gateway,user,sid,run,mid=setup(database,users)
    trip(database,run,mid)
    next_run,_=followup(database,user,sid,run,'Show the flights too.')
    c=gateway.envelope(user,next_run,[flight()],'travel').queries[0].criteria
    assert c['departure_date']==START and c['return_date']==END


@pytest.mark.parametrize('invalidate',['context_clear','new_trip','other_chat'])
def test_prior_trip_dates_are_scoped_and_invalidated(database,users,invalidate):
    gateway,user,sid,run,mid=setup(database,users)
    trip(database,run,mid)
    if invalidate=='context_clear':database.execute('UPDATE sessions SET context_floor=? WHERE id=?',(mid,sid))
    if invalidate=='other_chat':sid=database.new_session(user)
    next_run,next_mid=followup(database,user,sid,run,'Start another trip; show flights.')
    if invalidate=='new_trip':TravelPlans(database).activate(user,sid,next_mid,reset=True)
    assert not active_scope(database,user,next_run)
    assert 'departure_date' not in gateway.envelope(user,next_run,[flight()],'travel').queries[0].criteria


@pytest.mark.parametrize('same_run',[True,False])
def test_dining_inherits_every_trip_date_and_only_requested_meals(database,users,same_run):
    text='Also include restaurants for lunch and dinner.'
    gateway,user,sid,run,mid=setup(database,users,text)
    trip(database,run,mid)
    if not same_run:run,mid=followup(database,user,sid,run,text)
    task=gateway.envelope(user,run,[{'domain':'dining','criteria':{'location':'Aruba'}}],'dining')
    assert len(task.queries)==16
    assert {q.criteria['date'] for q in task.queries}=={'2099-01-'+str(day) for day in range(19,27)}
    assert {q.criteria['meal'] for q in task.queries}=={'lunch','dinner'}


@pytest.mark.parametrize('same_run',[True,False])
@pytest.mark.parametrize('city',['Phoenix','Aruba'])
def test_explicit_restaurant_day_and_other_location_do_not_inherit_trip_range(database,users,same_run,city):
    text='Also include dinner on 2099-02-02 in '+city+'.'
    gateway,user,sid,run,mid=setup(database,users,text)
    trip(database,run,mid)
    if not same_run:run,_=followup(database,user,sid,run,text)
    task=gateway.envelope(user,run,[{'domain':'dining','criteria':{'location':city,'date':'2099-02-02','meal':'dinner'}}],'dining')
    assert len(task.queries)==1 and task.queries[0].criteria['date']=='2099-02-02'
