import asyncio
import base64
import json
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from personal_agent import config
from personal_agent.agent import ResearchDecision
from personal_agent.app import create_app
from personal_agent.illustrations import Illustrations,ImageReview
from conftest import authenticate
from test_attachments import PNG
from test_availability import service_for
from test_agent import wait_service

SCENE='Pip, a small white fox in a blue scarf, helps one red bird beside a moonlit lake.'


@pytest.mark.parametrize('prompt,expected',[
    ('Write a story about my trip above',True),
    ('Tell me a bedtime story',True),
    ('A story about Dubai',True),
    ('Rewrite this story with more detail',True),
    ('How do I write a story?',False),
    ('What is the story behind this building?',False),
    ('Write a story, text only please',False),
    ('Tell me a story without pictures',False),
    ('Do not write a story',False),
])
def test_story_illustration_defaults_respect_request_type_and_opt_out(prompt,expected):
    from personal_agent.illustrations import story_illustration_default
    assert story_illustration_default(prompt) is expected


async def test_explicit_text_only_story_does_not_force_generation(database,users):
    user=users[0]['id'];sid=database.new_session(user)
    service=service_for(database,[AIMessage(content='A short trip story.')])
    # Even a mistaken image classifier must respect the explicit opt-out.
    router=AsyncMock(return_value=ResearchDecision(requires_research=False,image_generation=True))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router))
    try:
        rid=(await service.submit(user,sid,'Write a story, text only.'))['run_id'];await wait_service(service,rid)
        assert service.engine.model.choices[0] is None
        assert not database.one('SELECT id FROM illustrations WHERE run_id=?',(rid,))
        assert database.one('SELECT status FROM runs WHERE id=?',(rid,))['status']=='completed'
    finally:await service.close();await service.engine.client.close()


def setup(database,user,reviews):
    sid=database.new_session(user);database.message(user,sid,'user','Illustrate this story')
    rid='image-'+str(time.time_ns())
    database.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'running',?)",(rid,user,sid,time.time()))
    response=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])
    client=SimpleNamespace(images=SimpleNamespace(generate=AsyncMock(return_value=response),edit=AsyncMock(return_value=response)))
    inspect=AsyncMock(side_effect=reviews)
    verifier=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=inspect))
    return sid,rid,Illustrations(database,client,verifier),inspect


async def test_verified_images_are_private_idempotent_and_keep_exact_text(database,users,credentials):
    uid=users[0]['id'];sid,rid,images,inspect=setup(database,uid,[ImageReview(aligned=True,issues=[])])
    result=await images.generate(uid,rid,SCENE,'Watercolor')
    assert result['status']=='verified'
    assert await images.generate(uid,rid,SCENE,'Watercolor')==result
    assert images.client.images.generate.await_count==1
    row=database.one('SELECT * FROM artifacts WHERE id=?',(result['image_id'],))
    assert row['user_id']==uid and open(row['path'],'rb').read()==PNG
    mid=database.message(uid,sid,'assistant',images.render(rid));images.attach(rid,mid)
    assert images.recent(uid,sid)[0]['scene_text']==SCENE
    assert images.recent(uid,database.new_session(uid))==[]
    with TestClient(create_app(database,service_for(database,[AIMessage(content='Hi')])),base_url=config.ORIGIN,headers={'Origin':config.ORIGIN}) as client:
        assert client.get(result['image_url']).status_code==401
        authenticate(client,credentials)
        assert client.get(result['image_url']).content==PNG
        client.post('/api/logout')
        authenticate(client,credentials,users[1]['username'])
        assert client.get(result['image_url']).status_code==404


async def test_visual_mismatch_is_corrected_once_using_the_candidate(database,users):
    sid,rid,images,inspect=setup(database,users[0]['id'],[ImageReview(aligned=False,issues=['Scarf is red; must be blue.']),ImageReview(aligned=True,issues=[])])
    result=await images.generate(users[0]['id'],rid,SCENE)
    assert result['status']=='verified'
    edit=images.client.images.edit.await_args.kwargs
    assert 'Scarf is red' in edit['prompt'] and edit['image'][0][1]==PNG
    assert inspect.await_count==2 and images.client.images.generate.await_count==1


@pytest.mark.parametrize('reviews',[
    [ImageReview(aligned=False,issues=['Bird missing'])]*2,
    [RuntimeError('Verification unavailable')]])
async def test_failed_or_unavailable_verification_never_publishes_an_image(database,users,reviews):
    sid,rid,images,inspect=setup(database,users[0]['id'],reviews)
    result=await images.generate(users[0]['id'],rid,SCENE)
    assert result['status']=='error'
    assert not database.rows('SELECT * FROM artifacts')
    assert '/api/artifacts/' not in images.render(rid)
    assert inspect.await_count<=2


async def test_edit_reuses_reference_and_checks_character_continuity(database,users):
    uid=users[0]['id'];sid,rid,images,inspect=setup(database,uid,[ImageReview(aligned=True,issues=[])]*2)
    first=await images.generate(uid,rid,SCENE)
    mid=database.message(uid,sid,'assistant',images.render(rid));images.attach(rid,mid)
    result=await images.generate(uid,rid,SCENE,'Richer watercolor detail','latest')
    assert result['image_id']!=first['image_id']
    assert images.client.images.edit.await_args.kwargs['image'][0][1]==PNG
    assert len(inspect.await_args.args[0][1].content)==4
    other=database.new_session(uid)
    with pytest.raises(ValueError):images.reference(uid,other,first['image_id'])
    with pytest.raises(ValueError):images.reference(users[1]['id'],database.new_session(users[1]['id']),first['image_id'])
    database.execute('INSERT INTO context_exclusions VALUES(?,?)',(mid,'forgotten'))
    assert images.recent(uid,sid)==[]
    with pytest.raises(ValueError):images.reference(uid,sid,first['image_id'])
    database.execute("UPDATE runs SET status='completed' WHERE id=?",(rid,))
    with pytest.raises(ValueError):images.reference(uid,sid,first['image_id'])


async def test_scene_limit_and_cancellation_do_not_publish_unverified_artifacts(database,users):
    uid=users[0]['id'];sid,rid,images,inspect=setup(database,uid,[ImageReview(aligned=True,issues=[])]*3)
    for n in range(3):assert (await images.generate(uid,rid,SCENE+' Scene '+str(n)))['status']=='verified'
    assert (await images.generate(uid,rid,'Fourth scene'))['status']=='error'
    assert images.client.images.generate.await_count==3
    database.execute("UPDATE runs SET status='completed' WHERE id=?",(rid,))
    sid,rid,images,inspect=setup(database,uid,[])
    images.client.images.generate.side_effect=asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):await images.generate(uid,rid,SCENE)
    assert database.one('SELECT status FROM illustrations WHERE run_id=?',(rid,))['status']=='cancelled'
    assert not database.one('SELECT id FROM artifacts WHERE run_id=?',(rid,))


@pytest.mark.parametrize('prompt,classified_image',[
    ('Write a short story with a picture.',True),
    ('write a story about my trip above',False),
    ('Tell me a bedtime story.',False),
])
async def test_main_harness_publishes_verified_scene_instead_of_model_drift(database,users,prompt,classified_image):
    uid=users[0]['id'];sid=database.new_session(uid)
    service=service_for(database,[AIMessage(content='',tool_calls=[{'name':'generate_illustration','args':{'scene_text':SCENE,'instructions':'Watercolor'},'id':'draw','type':'tool_call'}]),AIMessage(content='A black dog drives a car. Here is your picture!')])
    response=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])
    images=service.engine.illustrations
    images.client=SimpleNamespace(images=SimpleNamespace(generate=AsyncMock(return_value=response)))
    images.verifier=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=AsyncMock(return_value=ImageReview(aligned=True,issues=[]))))
    router=AsyncMock(return_value=ResearchDecision(requires_research=False,image_generation=classified_image))
    service.engine.research_model=SimpleNamespace(with_structured_output=lambda *a,**k:SimpleNamespace(ainvoke=router))
    try:
        rid=(await service.submit(uid,sid,prompt))['run_id'];await wait_service(service,rid)
        assert service.engine.model.choices[0]=={'type':'function','function':{'name':'generate_illustration'}}
        answer=database.rows("SELECT * FROM messages WHERE session_id=? AND role='assistant'",(sid,))[-1]
        assert SCENE in answer['content'] and 'black dog' not in answer['content']
        assert '![Illustration](/api/artifacts/' in answer['content']
        assert database.one('SELECT artifact_id FROM message_attachments WHERE message_id=?',(answer['id'],))
        assert 'data:image' not in str(database.rows('SELECT payload FROM events WHERE run_id=?',(rid,)))
    finally:await service.close();await service.engine.client.close()
