import json

from conftest import authenticate
from personal_agent.approvals import approval_text,display_message


def test_approval_uses_readable_task_options_and_price():
    text=approval_text({'action':{'summary':'Add to Cart','operation':'click','url':'https://shop.example/product',
        'task':'Add blue shoes in size 10, quantity 1','selected_options':[{'name':'size','value':'10'}],
        'displayed_prices':['$80.00'],'verification':'internal schema'}})
    assert all(value in text for value in ['Add to Cart','shop.example','size: 10','$80.00','type approve or cancel'])
    assert '"operation"' not in text and 'internal schema' not in text and '{' not in text


def test_existing_approval_history_hides_json_and_codes(client,database,users,credentials):
    authenticate(client,credentials)
    user=users[0]['id'];sid=database.new_session(user)
    action={'operation':'click','control':'Add to Cart','url':'https://shop.example/product','task':'Add blue shoes size 10','displayed_prices':['$80.00']}
    body='Approval required:\nClick: Add to Cart\n'+json.dumps(action,indent=2)+'\nReply approve deadbeef or reject deadbeef.'
    mid=database.message(user,sid,'assistant',body)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created,approval,approval_id) VALUES('pending',?,?,'approval',0,?,'deadbeef')",(user,sid,json.dumps({'type':'browser_approval','action':action})))
    result=client.get('/api/sessions/'+sid).json()
    assert result['active_run']['approval_message_id']==mid
    assert result['active_run']['approval_text']==result['messages'][0]['content']
    assert 'Add to Cart' in result['messages'][0]['content'] and '$80.00' in result['messages'][0]['content']
    assert 'deadbeef' not in result['messages'][0]['content'] and '"operation"' not in result['messages'][0]['content']
    assert database.one('SELECT content FROM messages WHERE id=?',(mid,))['content']==body
    assert display_message('user','approve deadbeef')=='Approve'
    assert display_message('user','reject deadbeef')=='Cancel'
    assert display_message('assistant','A normal message {with braces}')=='A normal message {with braces}'
