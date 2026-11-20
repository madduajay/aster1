import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from personal_agent import config
from personal_agent.app import create_app
from personal_agent.service import Service
from conftest import StubBrowsers,authenticate
from test_background import QUERY
from dining_test_helpers import agent_service,fake_company,submit_client,emit_opening


def test_alerts_and_controls_are_authenticated_and_user_scoped(database,users,credentials):
    service=agent_service(database);fake_company(service)
    with TestClient(create_app(database,service),base_url=config.ORIGIN,headers={'origin':config.ORIGIN}) as client:
        assert client.get('/api/background').status_code==401
        authenticate(client,credentials)
        sid=client.post('/api/sessions',json={}).json()['id']
        reply=submit_client(client,service,sid,QUERY)
        assert 'currently unavailable' in reply
        watch=client.get('/api/background').json()['watches'][0]
        asyncio.run(emit_opening(service))
        notification=client.get('/api/background').json()['notifications'][0]
        assert client.post('/api/background/memory',json={}).status_code==200
        assert database.one("SELECT user_id FROM background_jobs WHERE kind='memory'")['user_id']==users[0]['id']
        assert client.post('/api/background/notifications/'+str(notification['id'])+'/read',json={},headers={'origin':'https://evil.example'}).status_code==403
        authenticate(client,credentials,'user02')
        other=client.get('/api/background').json()
        assert other['watches']==[] and other['notifications']==[] and other['profile'] is None
        assert client.post('/api/background/watches/'+watch['id']+'/cancel',json={}).status_code==404
        assert client.post('/api/background/watches/'+watch['id'],json={'location':'New York','date':'2099-12-01','party_size':2}).status_code==404
        assert client.post('/api/background/notifications/'+str(notification['id'])+'/read',json={}).status_code==404
        assert client.post('/api/login',json={'username':'user01','password':'test-password-for-user01'}).status_code==200
        assert client.post('/api/background/notifications/'+str(notification['id'])+'/read',json={}).status_code==200
        assert client.get('/api/background').json()['notifications']==[]
        assert database.one('SELECT read_at FROM notifications WHERE id=?',(notification['id'],))['read_at'] is not None


def test_form_cannot_create_a_watch_without_a_real_user_request(database,users,credentials):
    service=Service(database,SimpleNamespace(model=None,browsers=StubBrowsers()))
    with TestClient(create_app(database,service),base_url=config.ORIGIN,headers={'origin':config.ORIGIN}) as client:
        authenticate(client,credentials)
        assert client.post('/api/background/watches/forged',json={'location':'New York','date':'2099-12-01','party_size':2}).status_code==404
        assert database.one('SELECT COUNT(*) n FROM background_jobs')['n']==0


def test_clear_all_alerts_persists_across_login_and_preserves_other_users(database,users,credentials):
    service=agent_service(database);fake_company(service)
    with TestClient(create_app(database,service),base_url=config.ORIGIN,headers={'origin':config.ORIGIN}) as client:
        assert client.post('/api/background/notifications/read',json={}).status_code==401
        for username in ('user01','user02'):
            authenticate(client,credentials,username)
            sid=client.post('/api/sessions',json={}).json()['id']
            submit_client(client,service,sid,QUERY)
            asyncio.run(emit_opening(service))
        client.post('/api/login',json={'username':'user01','password':'test-password-for-user01'})
        assert client.post('/api/background/notifications/read',json={},headers={'origin':'https://evil.example'}).status_code==403
        assert client.post('/api/background/notifications/read',json={}).status_code==200
        assert client.get('/api/background').json()['notifications']==[]
        assert client.post('/api/background/notifications/read',json={}).status_code==200
        client.post('/api/logout',json={})
        client.post('/api/login',json={'username':'user01','password':'test-password-for-user01'})
        assert client.get('/api/background').json()['notifications']==[]
        client.post('/api/login',json={'username':'user02','password':'test-password-for-user02'})
        assert len(client.get('/api/background').json()['notifications'])==1
