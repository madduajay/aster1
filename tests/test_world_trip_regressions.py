import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from availability.activities import extract,search as search_activities
from availability.company import Stock,options
from availability.contracts import Task,leaves
from availability.money import budget,convert
from availability.presentation import compose,render,availability_text
from availability.research import extract as extract_candidates
from availability.routes import connections
from availability.security import Security
from availability.subagent import Agent
from test_availability import model_for,service_for
from langchain_core.messages import AIMessage
from test_candidate_pipeline import candidate
from test_trip_presentation import seed


@pytest.mark.parametrize('text,currency,limit',[
    ('budget of EUR 15,000','EUR',15000),('USD 16,000 budget','USD',16000),
    ('budget is €15,000','EUR',15000),('20k budget','USD',20000),
    ('budget of 15000 EUR','EUR',15000),('budget of $18,000','USD',18000)])
def test_currency_and_limit_are_preserved(text,currency,limit):
    assert budget(text)=={'currency':currency,'limit':limit}


def test_timezone_schedules_and_distinct_airport_aliases():
    c={'origin':'PHX','destination':'New York','departure_date':'2099-12-01','return_date':'2099-12-04','passengers':2,'cabin':'business','carrier':'American Airlines'}
    f=options('flights',c)[0]
    assert f['arrival'][:10]==c['departure_date']
    assert datetime.fromisoformat(f['arrival'])>datetime.fromisoformat(f['departure'])
    assert datetime.fromisoformat(f['arrival']).utcoffset()!=datetime.fromisoformat(f['departure']).utcoffset()
    assert connections(['Frankfurt','Frankfurt (FRA)','FRA'])==['FRA']
    assert connections(['Ponta Delgada','PDL'])==['PDL']
    assert connections(['Washington (DCA)'])==['DCA']
    assert connections(['JFK','EWR'])==['JFK','EWR']
    with pytest.raises(ValueError):options('flights',{**c,'carrier':'FlightRoutes'})


async def test_source_publisher_cannot_become_operating_airline():
    quote='FlightRoutes says American Airlines flies MIA to GIG via Belém.'
    data={'candidates':[{**candidate(name),'connections':['Belém'],'route_verified':True,'evidence_quote':quote} for name in ['FlightRoutes','American Airlines']]}
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(data)))))
    result=await extract_candidates(client,'flights',quote,[{'url':'https://example.com/hotels'}],{})
    assert [r['name'] for r in result]==['American Airlines']


async def test_uncertain_route_snippet_is_not_confirmed_as_an_airline_connection():
    quote='American Airlines serves this route; stopovers include Belém, but the snippet does not identify the operating airlines for these connections.'
    data={'candidates':[{**candidate('American Airlines'),'connections':['Belém'],'route_verified':True,'evidence_quote':quote}]}
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps(data)))))
    assert await extract_candidates(client,'flights',quote,[{'url':'https://example.com/hotels'}],{})==[]


async def test_only_quoted_named_attractions_survive_extraction():
    answer='Sydney Opera House, Royal Botanic Garden and Things to do in Sydney.';url='https://example.com/sydney'
    items=[{'name':name,'evidence_quote':answer,'source_url':url,'period':'morning'} for name in ['Sydney Opera House','Royal Botanic Garden','Things to do in Sydney','Invented Theatre']]
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps({'attractions':items})))))
    result=await extract(client,answer,[{'url':url}])
    assert [r['name'] for r in result]==['Sydney Opera House','Royal Botanic Garden']


async def test_markdown_table_evidence_retains_original_quote():
    url='https://example.com/tokyo';answer='| [Ueno Park](https://example.com/tokyo) | Morning |'
    item={'name':'Ueno Park','evidence_quote':'Ueno Park — Morning','source_url':url,'period':'morning'}
    client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=json.dumps({'attractions':[item]})))))
    result=await extract(client,answer,[{'url':url}])
    assert result[0]['evidence_quote']==answer


async def test_entertainment_discovery_is_broad_but_requires_safe_cited_sources():
    url='https://tourism.example/park';answer='Ueno Park is a morning option.'
    annotations=[SimpleNamespace(type='url_citation',url=url,title='Ueno Park'),SimpleNamespace(type='url_citation',url='http://unsafe.example',title='Unsafe')]
    found=SimpleNamespace(output_text=answer,output=[SimpleNamespace(type='message',content=[SimpleNamespace(annotations=annotations)])])
    extracted=SimpleNamespace(output_text=json.dumps({'attractions':[{'name':'Ueno Park','evidence_quote':answer,'source_url':url,'period':'morning'}]}))
    call=AsyncMock(side_effect=[found,extracted]);client=SimpleNamespace(responses=SimpleNamespace(create=call))
    result=await search_activities(client,'Tokyo entertainment')
    assert result['attractions'][0]['name']=='Ueno Park' and len(result['sources'])==1
    assert 'filters' not in call.call_args_list[0].kwargs['tools'][0]


def test_structured_attractions_repair_bad_model_labels_without_retry_loop(database,users,tmp_path):
    uid=users[0]['id'];rid,days=seed(database,uid,tmp_path);url='https://example.com/dubai'
    database.event(rid,'research_evidence',{'answer':'Dubai Opera','sources':[{'url':url,'title':'Dubai Opera'}],
        'attractions':[{'name':'Dubai Opera','source_url':url,'period':'evening','evidence_quote':'Dubai Opera'}]})
    days[0]['activities']=[{'name':'Things to do','source_url':url,'period':'morning'}]
    days[1]['activities']=[{'name':'Generic entertainment','source_url':url,'period':'morning'}]
    assert compose(database,None,uid,rid,days)['status']=='completed'
    plan=json.loads(database.one("SELECT payload FROM events WHERE kind='trip_plan'")['payload'])
    assert not plan['days'][0]['activities']
    assert plan['days'][1]['activities'][0]['name']=='Dubai Opera'


def test_sparse_museum_research_cannot_fill_every_daily_period(database,users,tmp_path):
    uid=users[0]['id'];rid,days=seed(database,uid,tmp_path);url='https://example.com/dubai'
    database.event(rid,'research_evidence',{'answer':'Dubai Museum','sources':[{'url':url,'title':'Dubai Museum'}],
        'attractions':[{'name':'Dubai Museum','source_url':url,'period':'afternoon','evidence_quote':'Dubai Museum'}]})
    result=compose(database,None,uid,rid,days)
    assert result['status']=='needs_research' and any('evening' in a for a in result['activities'])


def test_euro_budget_converts_costs_instead_of_relabeling_usd(database,users,tmp_path):
    uid=users[0]['id'];rid,days=seed(database,uid,tmp_path)
    sid=database.one('SELECT session_id FROM runs WHERE id=?',(rid,))['session_id']
    database.message(uid,sid,'user','Use a budget of EUR 15,000')
    rates={'rates':{'EUR':1,'USD':1.25},'date':'2099-12-01','source_url':'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'}
    database.event(rid,'exchange_rates',rates)
    assert compose(database,None,uid,rid,days)['status']=='completed'
    plan=json.loads(database.one("SELECT payload FROM events WHERE kind='trip_plan'")['payload'])
    assert plan['budget']['currency']=='EUR' and plan['budget']['limit']==15000
    assert plan['budget']['costs']['flights']==convert(9000,'USD','EUR',rates)==7200
    assert '**Budget (EUR)**' in render(plan) and '€7,200' in render(plan)


async def test_trip_hotel_starts_on_checked_arrival_but_standalone_dates_stay_intact(tmp_path,monkeypatch):
    calls=[];security=Security(tmp_path)
    stocks={d:Stock(tmp_path/(d+'.sqlite'),d,security) for d in ['hotels','flights']}
    async def company(security,domain,name,args):
        calls.append((domain,args['criteria'].copy()));return stocks[domain].search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    h={'hotel':'Hotel Alpha','destination':'Paris','check_in':'2099-12-01','check_out':'2099-12-04','guests':2,'rooms':1}
    f={'origin':'JFK','destination':'Paris','departure_date':'2099-12-01','return_date':'2099-12-04','passengers':2,'cabin':'business','carrier':'Condor','connections':['Frankfurt']}
    task=Task(request_id='aligned-trip-request',user_id='user1',session_id='session1',source_message_id=1,preferences={'arrival_aligned_stay':True},queries=[{'domain':'hotels','criteria':h},{'domain':'flights','criteria':f}])
    result=await Agent('travel',tmp_path,model_for(0,1),InMemorySaver()).query(task)
    assert calls[0][0]=='flights' and calls[1][1]['check_in']=='2099-12-02'
    assert result['results'][0]['criteria']['check_in']=='2099-12-02'
    task=Task(request_id='standalone-hotel-request',user_id='user1',session_id='session1',source_message_id=2,queries=[{'domain':'hotels','criteria':h}])
    await Agent('travel',tmp_path,model_for(0),InMemorySaver()).query(task)
    assert calls[-1][1]['check_in']=='2099-12-01'


async def test_main_accepts_only_authorized_arrival_adjustment(database,users,tmp_path,monkeypatch):
    security=Security(tmp_path);stocks={d:Stock(tmp_path/(d+'.sqlite'),d,security) for d in ['hotels','flights']}
    async def company(security,domain,name,args):return stocks[domain].search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    h={'hotel':'Hotel Alpha','destination':'Paris','check_in':'2099-12-01','check_out':'2099-12-04','guests':2,'rooms':1}
    f={'origin':'JFK','destination':'Paris','departure_date':'2099-12-01','return_date':'2099-12-04','passengers':2,'cabin':'business','carrier':'Condor','connections':['FRA']}
    task=Task(request_id='gateway-aligned-trip',user_id=users[0]['id'],session_id='session1',source_message_id=1,preferences={'arrival_aligned_stay':True},queries=[{'domain':'hotels','criteria':h},{'domain':'flights','criteria':f}])
    service=service_for(database,[AIMessage(content='Hi')])
    async def api(role,path,user,body):return await Agent('travel',tmp_path,model_for(0,1),InMemorySaver()).query(Task.model_validate(body))
    monkeypatch.setattr(service.availability,'api',api)
    try:
        result=await service.availability.check(task)
        assert all(r['status']=='completed' for r in result['results'])
        assert result['results'][0]['criteria']['check_in']=='2099-12-02'
        good={**result,'request_id':task.request_id+'-travel'}
        good['results'][0]['criteria']['guests']=9
        monkeypatch.setattr(service.availability,'api',AsyncMock(return_value=good))
        rejected=await service.availability.check(task)
        assert all(r['status']=='error' for r in rejected['results'])
    finally:await service.close();await service.engine.client.close()


def test_fallback_tables_do_not_offer_meals_before_arrival_or_near_departure(tmp_path):
    security=Security(tmp_path)
    f=Stock(tmp_path/'f.sqlite','flights',security).search('f',{'origin':'PHX','destination':'New York','departure_date':'2099-12-01','return_date':'2099-12-03','passengers':2,'cabin':'business','carrier':'American Airlines'})
    d=Stock(tmp_path/'d.sqlite','dining',security);results=[f]
    for day,meal in [('2099-12-01','breakfast'),('2099-12-02','lunch'),('2099-12-03','dinner')]:
        results.append(d.search(day+meal,{'venue':day+meal,'location':'New York','date':day,'party_size':2,'meal':meal}))
    text=availability_text([{'results':results}],trip=True)
    assert '2099-12-02lunch' in text and '2099-12-01breakfast' not in text and '2099-12-03dinner' not in text
