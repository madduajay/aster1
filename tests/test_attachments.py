import base64

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from personal_agent.agent import Engine
from personal_agent.attachments import image_blocks
from personal_agent.memory import Memory
from personal_agent.service import Service
from conftest import SummaryModel, authenticate
from test_agent import ScriptedModel, ScriptedBrowser, wait_service

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOuoAAAAASUVORK5CYII=')


def test_image_upload_preview_persists_and_is_owned(client, database, users, credentials):
    authenticate(client, credentials)
    sid = client.post('/api/sessions', json={}).json()['id']
    uploaded = client.post('/api/uploads?session_id='+sid, content=PNG,
                           headers={'x-filename': 'sample.png'}).json()
    assert uploaded['image'] and uploaded['media'] == 'image/png'
    response = client.get(uploaded['url'])
    assert response.content == PNG
    assert response.headers['content-type'] == 'image/png'
    assert response.headers['content-disposition'].startswith('inline')
    messages = client.get('/api/sessions/'+sid).json()['messages']
    assert messages[-1]['attachments'][0]['id'] == uploaded['id']
    assert len(image_blocks(database, users[0]['id'], sid)) == 2
    other = database.new_session(users[0]['id'])
    assert image_blocks(database, users[0]['id'], other) == []
    with pytest.raises(LookupError):
        image_blocks(database, users[1]['id'], sid)
    # A misleading extension must not become an inline image.
    fake = client.post('/api/uploads?session_id='+sid, content=b'<script>bad</script>',
                       headers={'x-filename': 'fake.png'}).json()
    assert not fake['image'] and fake['media'] == 'application/octet-stream'
    database.execute('INSERT INTO context_exclusions VALUES(?,?)', (uploaded['message_id'], 'withdrawn'))
    assert image_blocks(database, users[0]['id'], sid) == []


def test_draft_upload_does_not_enter_conversation(client, database, users, credentials):
    authenticate(client, credentials)
    sid=client.post('/api/sessions',json={}).json()['id']
    uploaded=client.post('/api/uploads?draft=true&session_id='+sid,content=PNG,headers={'x-filename':'draft.png'}).json()
    assert uploaded['message_id'] is None
    assert client.get('/api/sessions/'+sid).json()['messages']==[]
    assert image_blocks(database,users[0]['id'],sid)==[]


async def test_image_reaches_model_without_entering_text_memory(database, users, tmp_path):
    uid = users[0]['id']; sid = database.new_session(uid)
    path = tmp_path/'sample.png'; path.write_bytes(PNG)
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES('upload',?,?,'completed',0)", (uid, sid))
    database.execute("INSERT INTO artifacts VALUES('image',?,'upload',?,'sample.png','image/png')", (uid, str(path)))
    assert image_blocks(database,uid,sid)==[]
    model = ScriptedModel(responses=[AIMessage(content='This is a small image.')])
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'image-checkpoints.sqlite')) as saver:
        engine = Engine(database, ScriptedBrowser(), saver, 'sk-test', model=model)
        service = Service(database, engine)
        digest = SummaryModel(); service.memory = Memory(database, digest)
        other=database.new_session(uid)
        with pytest.raises(ValueError):
            await service.submit(uid,other,'Wrong chat',attachment_ids=['image'])
        with pytest.raises(ValueError):
            await service.submit(users[1]['id'],database.new_session(users[1]['id']),'Wrong user',attachment_ids=['image'])
        run = (await service.submit(uid, sid, 'What is in this image?',attachment_ids=['image']))['run_id']
        await wait_service(service, run)
        assert database.one('SELECT status FROM runs WHERE id=?', (run,))['status'] == 'completed'
        blocks = [b for m in model.seen[0] if isinstance(m.content, list) for b in m.content]
        image = next(b for b in blocks if b['type'] == 'image_url')
        assert base64.b64decode(image['image_url']['url'].split(',',1)[1]) == PNG
        assert 'base64' not in str(digest.inputs)
        with pytest.raises(ValueError):
            await service.submit(uid,sid,'Send twice',attachment_ids=['image'])
        state = await saver.aget_tuple({'configurable': {'thread_id': f'{uid}:{sid}:{run}'}})
        assert 'data:image' not in str(state.checkpoint)
        await engine.client.close()
