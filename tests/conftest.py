import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from personal_agent import config
from personal_agent.app import create_app
from personal_agent.db import Database


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    database = Database(tmp_path / 'app.sqlite')
    database.seed()
    return database


@pytest.fixture
def users(database):
    return database.rows('SELECT * FROM users ORDER BY username')


@pytest.fixture
def credentials(database):
    return dict(line.split() for line in (config.RUNTIME / 'initial-accounts.txt').read_text().splitlines())


class StubBrowsers:
    async def close_user(self, user):
        pass


class StubService:
    def __init__(self, db):
        self.db = db
        self.engine = SimpleNamespace(browsers=StubBrowsers())

    async def new_session(self, user):
        return self.db.new_session(user)

    def active(self, user):
        return None

    async def cancel(self, user, run):
        if not self.db.one('SELECT id FROM runs WHERE id=? AND user_id=?', (run, user)):
            raise LookupError('Run not found')


@pytest.fixture
def client(database):
    with TestClient(create_app(database, StubService(database)), base_url=config.ORIGIN,
                    headers={'origin': config.ORIGIN}) as client:
        yield client


def authenticate(client, credentials, username='user01'):
    response = client.post('/api/login', json={'username': username, 'password': credentials[username]})
    assert response.status_code == 200
    response = client.post('/api/password', json={'current_password': credentials[username],
                                                 'new_password': 'test-password-for-' + username})
    assert response.status_code == 200


class SummaryModel:
    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.inputs = result, fail, []

    def with_structured_output(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        from personal_agent.memory import Digest
        self.inputs.append(json.loads(messages[-1].content))
        if self.fail:
            raise RuntimeError('Controlled failure')
        if self.result:
            return self.result
        return Digest(summary='Remembered session summary: ' + self.inputs[-1]['messages'][-1]['content'][:500])
