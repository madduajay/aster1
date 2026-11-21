import json
import time
from datetime import datetime,timezone

import pytest
from langchain_core.messages import AIMessage
from availability.contracts import criteria,CriteriaError,describe
from availability.targets import literal_hotels
from test_availability import service_for


class UserClock(datetime):
    @classmethod
    def now(cls,tz=None):return cls(2026,11,21,6,30,tzinfo=timezone.utc).astimezone(tz)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr('availability.contracts.datetime',UserClock)
    monkeypatch.setattr('personal_agent.config.TIMEZONE','America/Phoenix')


def test_defaults_use_user_calendar_day(clock):
    dining,missing=criteria('dining',{'venue':'Peacock','location':'New York'})
    assert not missing and dining['date']=='2026-11-20' and dining['party_size']==2
    hotel,missing=criteria('hotels',{'hotel':'Hyatt Grand Central'})
    assert not missing and (hotel['check_in'],hotel['check_out'],hotel['rooms'],hotel['guests'])==('2026-11-21','2026-11-22',1,2)
    flight,missing=criteria('flights',{'origin':'LAX','destination':'Paris','departure_date':'2026-12-03','cabin':'business'})
    assert not missing and flight['passengers']==2 and 'rooms' not in flight


def test_explicit_values_and_one_night_checkout_are_preserved(clock):
    hotel,missing=criteria('hotels',{'hotel':'Hyatt','check_in':'2026-12-03','guests':4,'rooms':2})
    assert not missing and hotel['check_out']=='2026-12-04' and hotel['guests']==4 and hotel['rooms']==2
    dining,_=criteria('dining',{'location':'New York','date':'2026-12-03','party_size':6})
    assert dining['date']=='2026-12-03' and dining['party_size']==6
    with pytest.raises(CriteriaError):criteria('hotels',{'hotel':'Hyatt','rooms':0})
    with pytest.raises(CriteriaError):criteria('dining',{'location':'New York','party_size':0})


@pytest.mark.parametrize('candidates',[[],['flights'],['flights from LA to Paris']])
def test_combined_request_never_binds_flights_as_hotel(candidates):
    text='can you check flights from LA to Paris on December 3rd to 21st for 2 people, business class and also check the hotel options'
    assert literal_hotels(text,candidates,hotel_domain=True)==[]


def test_flight_validation_never_asks_for_hotel_room_fields():
    result={'domain':'flights','criteria':{},'status':'needs_input','missing_fields':['rooms']}
    assert 'number of rooms' not in describe(result)


async def test_main_binds_defaults_and_inherits_provided_trip_dates(database,users):
    uid=users[0]['id'];sid=database.new_session(uid)
    database.message(uid,sid,'user','Check business flights from LA to Paris December 3 to 21 and hotel options')
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('defaults-run',?,?,'running',?)",(uid,sid,time.time()))
    service=service_for(database,[AIMessage(content='Hi')])
    try:
        task=service.availability.envelope(uid,'defaults-run',[
            {'domain':'flights','criteria':{'origin':'LAX','destination':'Paris','departure_date':'2099-12-03','return_date':'2099-12-21','cabin':'business','passengers':8}},
            {'domain':'hotels','criteria':{'hotel':'flights','destination':'Paris','rooms':5,'guests':8}}],'travel')
        flight,hotel=task.queries
        assert not task.requested_hotels and 'hotel' not in hotel.criteria
        assert flight.criteria['passengers']==2 and hotel.criteria['guests']==2 and hotel.criteria['rooms']==1
        assert hotel.criteria['check_in']=='2099-12-03' and hotel.criteria['check_out']=='2099-12-21'
    finally:await service.close();await service.engine.client.close()


async def test_default_trip_party_covers_each_day_and_explicit_counts_win(database,users):
    uid=users[0]['id'];sid=database.new_session(uid)
    database.message(uid,sid,'user','Plan New York December 1 to 3, 2099 with dining')
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('trip-defaults',?,?,'running',?)",(uid,sid,time.time()))
    database.event('trip-defaults','trip_requested',{'criteria':{'start':'2099-12-01','end':'2099-12-03','location':'New York','party_size':0}})
    service=service_for(database,[AIMessage(content='Hi')])
    try:
        task=service.availability.envelope(uid,'trip-defaults',[{'domain':'dining','criteria':{}}],'dining')
        assert len(task.queries)==9 and all(q.criteria['party_size']==2 for q in task.queries)
        database.message(uid,sid,'user','Actually for 4 people')
        task=service.availability.envelope(uid,'trip-defaults',[{'domain':'dining','criteria':{}}],'dining')
        assert all(q.criteria['party_size']==4 for q in task.queries)
    finally:await service.close();await service.engine.client.close()
