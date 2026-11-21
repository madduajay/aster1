import json
from datetime import date,timedelta
from types import SimpleNamespace

from availability.company import Stock
from availability.presentation import compose,render,availability_text
from availability.security import Security


def seed(db,user,tmp_path):
    sid=db.new_session(user);source=db.message(user,sid,'user','Plan a trip, 2 travelers, 1 room, 20k budget.')
    rid='trip-test-run'
    db.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',0)",(rid,user,sid))
    security=Security(tmp_path);results=[]
    for domain,c in [('hotels',{'hotel':'Hotel Alpha','destination':'Dubai','check_in':'2099-12-01','check_out':'2099-12-03','guests':2,'rooms':1,'stars':4}),
        ('flights',{'origin':'Phoenix','destination':'Dubai','departure_date':'2099-12-01','return_date':'2099-12-03','passengers':2,'cabin':'business','carrier':'British Airways','connections':['London Heathrow']}),
        ('dining',{'venue':'Restaurant Alpha','location':'Dubai','date':'2099-12-02','party_size':2})]:
        results.append(Stock(tmp_path/(domain+'.sqlite'),domain,security).search('query-'+domain,c))
    # Presentation cases use a fixed arrival, independent of Company schedules.
    results[1]['options'][0]['arrival']='2099-12-02T17:00:00+04:00'
    db.event(rid,'availability_result',{'results':results,'research':[]})
    db.event(rid,'research_evidence',{'answer':'Dubai Museum and Restaurant Alpha are named here.','sources':[{'url':'https://example.com/dubai','title':'Dubai Museum'}]})
    days=[{'date':'2099-12-01','transit':'outbound','dining':[],'activities':[]},
        {'date':'2099-12-02','transit':'none','dining':['Restaurant Alpha'],'activities':[{'name':'Dubai Museum','source_url':'https://example.com/dubai','period':'evening'}]},
        {'date':'2099-12-03','transit':'return','dining':[],'activities':[]}]
    return rid,days


def test_itinerary_contains_all_days_checked_meals_route_rating_and_budget(database,users,tmp_path):
    user=users[0]['id'];rid,days=seed(database,user,tmp_path)
    result=compose(database,None,user,rid,days)
    assert result['status']=='completed'
    plan=json.loads(database.one("SELECT payload FROM events WHERE kind='trip_plan'")['payload'])
    text=render(plan)
    assert all('Day '+str(i)+' ·' in text for i in range(1,4))
    assert 'via LHR' in text and '4-star' in text
    assert '[Restaurant Alpha]' in text and '[Dubai Museum]' in text
    assert plan['budget']['total']<20000
    assert 'Research recommendations' not in text


def test_missing_dining_or_unsourced_sightseeing_is_repaired_before_rendering(database,users,tmp_path):
    user=users[0]['id'];rid,days=seed(database,user,tmp_path)
    days[1]['dining']=['Unchecked Restaurant']
    result=compose(database,None,user,rid,days)
    assert result['status']=='needs_plan' and result['dining_choices']=={'2099-12-02':['Restaurant Alpha']}
    assert not database.rows("SELECT id FROM events WHERE kind='trip_plan'")
    days[1]['dining']=['Restaurant Alpha'];days[1]['activities'][0]['source_url']='https://invented.example.com'
    assert compose(database,None,user,rid,days)['status']=='needs_research'
    assert not database.rows("SELECT id FROM events WHERE kind='trip_plan'")


def test_itinerary_rejects_exceeding_budget_and_missing_dates(database,users,tmp_path):
    user=users[0]['id'];rid,days=seed(database,user,tmp_path)
    assert compose(database,None,user,rid,days[1:])['status']=='needs_plan'
    result=compose(database,None,user,rid,days,dining_estimate=20000)
    assert result['status']=='needs_plan' and result['total']>result['limit']


def test_category_tables_merge_repeated_venues_and_superseded_errors(tmp_path):
    stock=Stock(tmp_path/'dining.sqlite','dining',Security(tmp_path))
    c={'venue':'Restaurant Alpha','location':'Dubai','date':'2099-12-02','party_size':2}
    first=stock.search('query-first',c)
    duplicate=stock.search('query-retry',c)
    next_day=stock.search('query-next',{**c,'date':'2099-12-03'})
    old={'domain':'dining','criteria':{k:v for k,v in c.items() if k!='venue'},'query_id':'old-query','status':'error'}
    text=availability_text([{'results':[old,first,duplicate,next_day]}])
    assert '**Dining**' not in text and text.count('| Venue | Dates | Available times |')==1 and text.count('Restaurant Alpha')==1
    assert '2099-12-02, 2099-12-03' in text and '19:00, 19:30' in text
    assert 'could not be checked' not in text
    assert '| Venue | Dates | Available times |' in text


def test_internal_route_error_is_not_a_request_for_user_input():
    text=availability_text([{'results':[{'domain':'flights','criteria':{'destination':'Dubai'},'status':'needs_input','missing_fields':['Invalid flight connections']}]}])
    assert 'Please provide' not in text and 'Invalid flight connections' not in text
