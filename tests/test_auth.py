import re
import json

from personal_agent import config
from personal_agent.app import token_hash
from personal_agent.db import PASSWORDS
from conftest import authenticate


def test_public_origin_is_exact_and_cannot_cross_local_origin(database,credentials):
    from fastapi.testclient import TestClient
    from personal_agent.app import create_app
    from conftest import StubService
    origin='https://example-agent.ngrok-free.app'
    (config.RUNTIME/'public-origin.json').write_text(json.dumps({'origin':origin}))
    with TestClient(create_app(database,StubService(database)),base_url=origin,headers={'origin':origin}) as public:
        assert public.get('/').status_code==200
        assert public.post('/api/login',json={'username':'user01','password':credentials['user01']}).status_code==200
        assert public.post('/api/logout',json={},headers={'origin':config.ORIGIN}).status_code==403
        assert public.post('/api/logout',json={},headers={'origin':'https://evil.example'}).status_code==403
        assert public.get('/',headers={'host':'other.ngrok-free.app'}).status_code==400


def test_ten_accounts_have_unique_argon2_passwords(database, users, credentials):
    assert len(users) == len(credentials) == 10
    assert len(set(credentials.values())) == 10
    for user in users:
        assert user['password'].startswith('$argon2id$')
        assert PASSWORDS.verify(user['password'], credentials[user['username']])
    assert (config.RUNTIME / 'initial-accounts.txt').stat().st_mode & 0o777 == 0o600
    assert database.seed() == 0


def test_all_ten_can_login_and_change_password(client, credentials):
    for username in credentials:
        login = client.post('/api/login', json={'username': username, 'password': credentials[username]})
        assert login.status_code == 200 and login.json()['must_change']
        cookie = login.headers['set-cookie']
        assert all(flag in cookie.lower() for flag in ['secure', 'httponly', 'samesite=strict'])
        assert client.get('/api/sessions').status_code == 403
        change = client.post('/api/password', json={'current_password': credentials[username], 'new_password': f'new-password-{username}'})
        assert change.status_code == 200
        assert client.post('/api/sessions', json={}).status_code == 200
        assert client.post('/api/logout', json={}).status_code == 200
        assert client.get('/api/me').status_code == 401


def test_session_and_memory_isolation(client, database, users, credentials):
    authenticate(client, credentials)
    sid = client.post('/api/sessions', json={}).json()['id']
    user = users[0]['id']
    mid = database.message(user, sid, 'user', 'I prefer tea')
    fid, _ = database.put_fact(user, 'Prefers tea', mid, 'preference.drink', evidence='I prefer tea')
    client.post('/api/logout', json={})
    authenticate(client, credentials, 'user02')
    assert client.get('/api/sessions/' + sid).status_code == 404
    assert client.get('/api/memory').json() == []
    assert client.delete('/api/memory/' + str(fid)).status_code == 404
    assert database.facts(user)[0]['id'] == fid


def test_origin_and_login_rejection(client, credentials):
    assert client.post('/api/login', headers={'origin': 'https://evil.example'}, json={'username': 'user01', 'password': credentials['user01']}).status_code == 403
    assert client.get('/api/me', headers={'host': 'evil.example'}).status_code == 400
    for _ in range(8):
        assert client.post('/api/login', json={'username': 'user01', 'password': 'wrong'}).status_code == 401
    assert client.post('/api/login', json={'username': 'user01', 'password': credentials['user01']}).status_code == 429


def test_only_requested_buttons_and_safe_rendering(client):
    html = client.get('/').text
    assert re.findall(r'<button[^>]*>(.*?)</button>', html) == ['New Chat', 'Login']
    js = client.get('/static/app.js').text
    assert 'innerHTML' not in js
    assert "frame-ancestors 'none'" in client.get('/').headers['content-security-policy']


def test_runs_artifacts_and_approvals_isolated(client, database, users, credentials, tmp_path):
    sid = database.new_session(users[0]['id'])
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('run01',?,?, 'approval',0)", (users[0]['id'], sid))
    artifact = tmp_path / 'private.txt'
    artifact.write_text('private')
    database.execute("INSERT INTO artifacts VALUES('artifact01',?,'run01',?,'private.txt','text/plain')", (users[0]['id'], str(artifact)))
    authenticate(client, credentials, 'user02')
    assert client.get('/api/runs/run01').status_code == 404
    assert client.get('/api/runs/run01/events').status_code == 404
    assert client.get('/api/runs/run01/browser').status_code == 404
    assert client.get('/api/runs/run01/browser/frame').status_code == 404
    assert client.get('/api/artifacts/artifact01').status_code == 404
    assert client.post('/api/runs/run01/approval', json={'approval_id': 'x', 'decision': 'approve'}).status_code == 404
    assert client.post('/api/runs/run01/cancel', json={}).status_code == 404


def test_uploaded_files_are_owned_and_do_not_add_buttons(client,database,users,credentials):
    authenticate(client,credentials)
    sid=client.post('/api/sessions',json={}).json()['id']
    response=client.post('/api/uploads?session_id='+sid,content=b'test document',headers={'content-type':'application/octet-stream','x-filename':'fixture.txt'})
    assert response.status_code==200,response.text
    aid=response.json()['id']
    assert client.get('/api/artifacts/'+aid).content==b'test document'
    assert database.one('SELECT filename FROM artifacts WHERE id=?',(aid,))['filename']=='fixture.txt'
    assert client.post('/api/uploads?session_id='+sid,content=b'').status_code==400
    client.post('/api/logout',json={});authenticate(client,credentials,'user02')
    assert client.get('/api/artifacts/'+aid).status_code==404
    assert client.post('/api/uploads?session_id='+sid,content=b'no').status_code==404
    assert re.findall(r'<button[^>]*>(.*?)</button>',client.get('/').text)==['New Chat','Login']
