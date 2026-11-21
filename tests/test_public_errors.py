import json
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from availability.contracts import criteria,describe,validation_result,Task
from availability.company import Stock
from availability.security import Security
from availability.subagent import Agent
from personal_agent.approvals import display_message
from personal_agent.public_errors import REQUEST_FAILURE
from conftest import authenticate
from test_availability import D,model_for


@pytest.mark.parametrize('value',['7:00 PM','7 PM','7pm','7:00 p.m.','19:00','19:00:00','2026-11-20T19:00:00-07:00','2026-11-20T19:00:00Z'])
def test_common_dining_time_formats_are_normalized(value):
    c,missing=criteria('dining',{**D,'preferred_time':value})
    assert not missing and c['preferred_time']=='19:00'


@pytest.mark.parametrize('value',['7:00 PM','2026-11-20T19:00:00-07:00'])
async def test_original_carbone_inputs_reach_company_instead_of_prompting_user(tmp_path,monkeypatch,value):
    calls=[];stock=Stock(tmp_path/'company.sqlite','dining',Security(tmp_path))
    async def company(security,domain,name,args):
        calls.append(args);return stock.search(args['query_id'],args['criteria'])
    monkeypatch.setattr('availability.subagent.call_company',company)
    agent=Agent('dining',tmp_path,model_for(0,domains={0:'dining'}),InMemorySaver())
    result=await agent.query(Task(request_id='carbone-time-request',user_id='user1',session_id='session1',source_message_id=1,
        queries=[{'domain':'dining','criteria':{**D,'preferred_time':value}}]))
    assert calls[0]['criteria']['preferred_time']=='19:00'
    assert result['results'][0]['status']=='completed' and result['results'][0]['available'] is False
    assert 'Please provide' not in describe(result['results'][0])


@pytest.mark.parametrize('field,value,expected',[
    ('preferred_time','25:99','What time would you prefer'),('date','not a date','dining date'),
    ('party_size',40,'party size')])
def test_invalid_customer_values_get_friendly_field_questions(field,value,expected):
    raw={**D,field:value}
    try:criteria('dining',raw)
    except ValueError as exc:result=validation_result('dining',raw,exc)
    assert result['status']=='needs_input' and result['missing_fields']==[field]
    text=describe(result)
    assert expected in text and 'isoformat' not in text and 'ValueError' not in text and str(value) not in text


def test_unknown_internal_errors_do_not_become_missing_fields():
    result=validation_result('dining',D,ValueError("Invalid isoformat string: 'private-input'"))
    assert result['status']=='error' and 'missing_fields' not in result
    assert 'private-input' not in describe(result)


def test_date_timestamp_is_normalized_and_date_order_gets_a_human_question():
    c,missing=criteria('dining',{**D,'date':'2099-12-01T19:00:00-05:00'})
    assert not missing and c['date']=='2099-12-01'
    raw={'hotel':'Hotel Alpha','destination':'Dubai','check_in':'2099-12-03','check_out':'2099-12-01','guests':2,'rooms':1}
    try:criteria('hotels',raw)
    except ValueError as exc:result=validation_result('hotels',raw,exc)
    assert describe(result)=='What check-out date would you like? It needs to be after check-in.'


def test_history_projection_hides_old_generated_errors_but_keeps_code_explanations():
    raw="- Please provide Invalid isoformat string: '7:00 PM' for Carbone."
    assert 'isoformat' not in display_message('assistant',raw)
    assert display_message('user',raw)==raw
    explanation='A ValueError can occur when parsing dates. Here is how fromisoformat works.'
    assert display_message('assistant',explanation)==explanation
    old_browser='Browser task unverified: KeyError\n\nBrowser debug details:\nSource: worker.py:12'
    assert 'KeyError' not in display_message('assistant',old_browser)
    explicit_debug='Browser debug details:\nKeyError at worker.py:12'
    assert display_message('assistant',explicit_debug)==explicit_debug


def test_api_errors_and_saved_history_are_customer_safe(client,database,users,credentials):
    authenticate(client,credentials)
    user=users[0]['id'];sid=database.new_session(user)
    raw="Please provide Invalid isoformat string: '7:00 PM' for Carbone."
    mid=database.message(user,sid,'assistant',raw)
    assert 'isoformat' not in client.get('/api/sessions/'+sid).json()['messages'][0]['content']
    assert database.one('SELECT content FROM messages WHERE id=?',(mid,))['content']==raw
    client.app.state.service.new_session=AsyncMock(side_effect=ValueError('Internal parser detail: private-input'))
    response=client.post('/api/sessions')
    assert response.status_code==409 and response.text==REQUEST_FAILURE
