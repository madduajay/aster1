import json

from availability.presentation import compose,render
from availability.streaming import publish
from test_trip_presentation import seed


def setup(db,users,tmp_path):
    user=users[0]['id'];run,days=seed(db,user,tmp_path)
    source=db.one("SELECT id FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1")['id']
    db.event(run,'request_message',{'message_id':source})
    db.event(run,'trip_requested',{'source_message':source,'criteria':{
        'start':'2099-12-01','end':'2099-12-03','location':'Dubai','party_size':2}})
    return user,run,days


def sections(db,run):
    return [json.loads(r['payload']) for r in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='trip_section' ORDER BY id",(run,))]


def test_hotels_wait_for_flight_outcome_and_replays_do_not_duplicate(database,users,tmp_path):
    user,run,days=setup(database,users,tmp_path)
    group=json.loads(database.one("SELECT payload FROM events WHERE kind='availability_result'")['payload'])
    database.execute("DELETE FROM events WHERE kind='availability_result'")
    hotels=[r for r in group['results'] if r['domain']=='hotels']
    flights=[r for r in group['results'] if r['domain']=='flights']
    database.event(run,'availability_result',{'results':hotels})
    publish(database,user,run)
    assert [s['key'] for s in sections(database,run)]==['summary']
    database.event(run,'availability_result',{'results':flights})
    publish(database,user,run)
    assert [s['key'] for s in sections(database,run)]==['summary','flights','hotels']
    publish(database,user,run)
    assert len(sections(database,run))==3
    database.event(run,'response_reset',{})
    publish(database,user,run)
    assert [s['key'] for s in sections(database,run)][-3:]==['summary','flights','hotels']


def test_each_validated_day_streams_before_final_plan_without_duplicates(database,users,tmp_path):
    user,run,days=setup(database,users,tmp_path)
    first=compose(database,None,user,run,days[:1],complete=False)
    assert first['status']=='in_progress' and first['next_dates']==['2099-12-02','2099-12-03']
    assert not database.one("SELECT id FROM events WHERE kind='trip_plan'")
    assert [s['key'] for s in sections(database,run)]==['summary','flights','hotels','day:2099-12-01']
    assert compose(database,None,user,run,days[:2],complete=False)['status']=='in_progress'
    assert [s['key'] for s in sections(database,run)].count('day:2099-12-01')==1
    assert sections(database,run)[-1]['key']=='day:2099-12-02'
    assert compose(database,None,user,run,days)['status']=='completed'
    assert [s['key'] for s in sections(database,run)][-2:]==['day:2099-12-03','budget']
    plan=json.loads(database.one("SELECT payload FROM events WHERE kind='trip_plan'")['payload'])
    text=render(plan)
    assert '**Flights**' not in text and '**Hotels**' not in text
    assert text.index('| Flight |')<text.index('| Hotel |')<text.index('Day 1')<text.index('Day 2')<text.index('Day 3')<text.index('**Budget')


def test_invalid_or_out_of_order_days_never_publish(database,users,tmp_path):
    user,run,days=setup(database,users,tmp_path)
    assert compose(database,None,user,run,days[1:2],complete=False)['status']=='needs_plan'
    assert not sections(database,run)
    assert compose(database,None,user,run,days[:1],complete=False)['status']=='in_progress'
    days[1]['activities'][0]['name']='Invented attraction'
    assert compose(database,None,user,run,days[:2],complete=False)['status']=='needs_research'
    assert not any(s['key']=='day:2099-12-02' for s in sections(database,run))
    plan=json.loads(database.one("SELECT payload FROM events WHERE kind='trip_plan_progress'")['payload'])
    assert plan['status']=='in_progress' and '**Budget' not in render(plan)


def test_streaming_is_scoped_to_owned_itinerary(database,users,tmp_path):
    user,run,days=setup(database,users,tmp_path)
    publish(database,users[1]['id'],run)
    assert not sections(database,run)
    database.execute("DELETE FROM events WHERE kind='trip_requested'")
    publish(database,user,run)
    assert not sections(database,run)


def test_sse_delivers_sections_and_resets_with_owned_replay(client,database,users,credentials):
    from conftest import authenticate
    authenticate(client,credentials)
    user=database.one("SELECT id FROM users WHERE username='user01'")['id']
    sid=database.new_session(user);run='stream-replay'
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'completed',0)",(run,user,sid))
    reset=database.event(run,'response_reset',{})
    database.event(run,'trip_section',{'key':'flights','order':10,'text':'Checked flights'})
    database.event(run,'done',{'status':'completed'})
    response=client.get('/api/runs/'+run+'/events')
    assert response.status_code==200
    assert 'event: response_reset' in response.text and 'event: trip_section' in response.text
    assert 'Checked flights' in response.text
    cursor=database.one("SELECT id FROM events WHERE run_id=? AND kind='response_reset'",(run,))['id']
    replay=client.get('/api/runs/'+run+'/events?after='+str(cursor))
    assert 'event: response_reset' not in replay.text and 'Checked flights' in replay.text
    other=users[1]['id'] if users[1]['id']!=user else users[0]['id']
    database.execute('UPDATE runs SET user_id=? WHERE id=?',(other,run))
    assert client.get('/api/runs/'+run+'/events').status_code==404
