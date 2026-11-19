import pytest

from conftest import SummaryModel
from personal_agent.db import Database
from personal_agent.memory import Digest, Fact, Memory


def stable_fact(mid, supporting_words, text, key, **changes):
    values = dict(text=text, source_message=mid, importance=4, memory_key=key,
                  evidence=supporting_words, retention='stable_user_fact', subject='user',
                  sensitivity='ordinary', confidence=0.99,
                  durability_reason='The user directly states a lasting personal preference, independent of any task.')
    values.update(changes)
    return Fact(**values)


async def test_general_stable_self_disclosures_survive_new_session_without_task_leakage(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    entries = [
        ('my favorite color is white', 'Favorite color is white', 'preference.color'),
        ('my favority pet is dog', 'Favorite pet is a dog', 'preference.pet'),
        ('I am a software engineer', 'Works as a software engineer', 'profile.occupation'),
        ('My usual reading language is Spanish', 'Usually reads in Spanish', 'preference.reading_language'),
    ]
    facts = []
    for evidence, text, key in entries:
        mid = database.message(uid, sid, 'user', evidence)
        facts.append(stable_fact(mid, evidence, text, key))
        database.message(uid, sid, 'assistant', 'Thanks for sharing.')
    database.message(uid, sid, 'user', 'Find white shoes in Phoenix for tomorrow under $100')
    database.message(uid, sid, 'assistant', 'Here are shopping options.')
    await Memory(database, SummaryModel(Digest(summary='Profile discussion then a one-off shopping task.', facts=facts))).summarize(uid, sid)
    reloaded = Database(database.path)
    context = Memory(reloaded, None).context(uid, reloaded.new_session(uid), 'what is my favorite color and pet')
    assert {f['text'] for f in context['long_term_facts']} == {f.text for f in facts}
    assert all(f['origin'] == 'self_declared' for f in context['long_term_facts'])
    assert not context['summary'] and not context['recent_messages'] and not context['unsummarized_messages']
    assert 'Phoenix' not in str(context) and 'tomorrow' not in str(context)
    other = users[1]['id']
    assert not Memory(reloaded, None).context(other, reloaded.new_session(other), 'color pet')['long_term_facts']


@pytest.mark.parametrize('source,changes', [
    ('What is my favorite color?', {}),
    ('My favorite color is blue today', {}),
    ('If my favorite color is blue, find shoes', {}),
    ('Pretend my favorite color is blue', {}),
    ('My favorite color is blue. Do not remember this', {}),
    ('My favorite color is blue', {'confidence': 0.8}),
    ('My favorite color is blue', {'subject': 'other'}),
    ('My favorite color is blue', {'retention': 'session_only'}),
    ('My favorite color is blue', {'sensitivity': 'sensitive'}),
    ('My favorite color is blue', {'sensitivity': 'unknown'}),
    ('My favorite color is blue', {'durability_reason': ''}),
    ('My favorite color is blue', {'evidence': 'I like green'}),
    ('My password is abc123', {}),
])
async def test_unsafe_or_unsupported_candidates_do_not_become_permanent(database, users, source, changes):
    uid = users[0]['id']; sid = database.new_session(uid)
    mid = database.message(uid, sid, 'user', source)
    fact = stable_fact(mid, source, 'Favorite color blue', 'preference.color', **changes)
    await Memory(database, SummaryModel(Digest(summary='Conversation retained in this chat.', facts=[fact]))).summarize(uid, sid)
    assert not database.facts(uid)
    assert database.visible_messages(uid, sid)[0]['content'] == source


async def test_requests_choices_and_assistant_inference_are_session_only(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    request = database.message(uid, sid, 'user', 'Find white shoes')
    choice = database.message(uid, sid, 'user', 'Indian please')
    website = database.message(uid, sid, 'assistant', 'Your favorite pet is a dog')
    facts = [
        stable_fact(request, 'Find white shoes', 'Favorite color white', 'preference.color'),
        stable_fact(choice, 'Indian please', 'Prefers Indian food', 'preference.diet'),
        stable_fact(website, 'Your favorite pet is a dog', 'Favorite pet dog', 'preference.pet'),
    ]
    await Memory(database, SummaryModel(Digest(summary='A shopping request and restaurant choice.', facts=facts))).summarize(uid, sid)
    assert not database.facts(uid)


async def test_semantic_correction_and_forgetting_do_not_restore_old_preferences(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    first = database.message(uid, sid, 'user', 'My favorite color is white')
    fact = stable_fact(first, 'My favorite color is white', 'Favorite color white', 'preference.color')
    await Memory(database, SummaryModel(Digest(summary='Favorite white', facts=[fact]))).summarize(uid, sid)
    old = database.facts(uid)[0]['id']
    next_sid = database.new_session(uid)
    latest = database.message(uid, next_sid, 'user', 'My favorite color is green now')
    replacement = stable_fact(latest, 'My favorite color is green now', 'Favorite color green', 'preference.color')
    memory = Memory(database, SummaryModel(Digest(summary='Favorite green', facts=[replacement])))
    await memory.summarize(uid, next_sid)
    assert database.one('SELECT status FROM facts WHERE id=?', (old,))['status'] == 'superseded'
    fid = database.facts(uid)[0]['id']
    assert database.facts(uid)[0]['text'] == 'Favorite color green'
    database.forget(uid, fid)
    await memory.summarize(uid, next_sid)
    assert not database.facts(uid)
    assert 'white' not in str(memory.context(uid, sid, 'color')).lower()
    assert 'green' not in str(memory.context(uid, next_sid, 'color')).lower()


async def test_failed_extraction_retries_across_sessions_and_restart_without_copying_old_chat(database, users):
    uid, other = users[0]['id'], users[1]['id']
    sid = database.new_session(uid)
    mid = database.message(uid, sid, 'user', 'My favorite color is white')
    database.message(uid, sid, 'assistant', 'Thanks for sharing.')
    memory = Memory(database, SummaryModel(fail=True))
    memory.queue_summary(uid, sid)
    with pytest.raises(RuntimeError):
        await memory.summarize(uid, sid)
    assert database.one('SELECT session_id FROM pending_memory_summaries')['session_id'] == sid
    reloaded = Database(database.path)
    fact = stable_fact(mid, 'My favorite color is white', 'Favorite color white', 'preference.color')
    memory = Memory(reloaded, SummaryModel(Digest(summary='User preference.', facts=[fact])))
    other_sid = reloaded.new_session(other)
    await memory.retry_pending(other, other_sid)
    assert not reloaded.facts(other) and reloaded.one('SELECT session_id FROM pending_memory_summaries')
    fresh = reloaded.new_session(uid)
    # A new uncompleted request in the old chat is outside the queued exchange.
    later = reloaded.message(uid, sid, 'user', 'Book a trip to Phoenix tomorrow')
    await memory.retry_pending(uid, fresh)
    assert not reloaded.one('SELECT session_id FROM pending_memory_summaries')
    assert reloaded.owned_session(uid, sid)['covered'] < later
    context = memory.context(uid, fresh, 'color')
    assert context['long_term_facts'][0]['text'] == 'Favorite color white'
    assert not context['summary'] and not context['recent_messages'] and not context['unsummarized_messages']


async def test_service_recovers_queued_preference_before_first_answer_in_new_chat(database, users, tmp_path):
    import json
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from personal_agent.agent import Engine
    from personal_agent.service import Service
    from test_agent import ScriptedModel, ScriptedBrowser, wait_service

    uid = users[0]['id']; sid = database.new_session(uid)
    path = str(tmp_path / 'retention-graph.sqlite')
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        first_model = ScriptedModel(responses=[AIMessage(content='Thanks for sharing.')])
        engine = Engine(database, ScriptedBrowser(), saver, 'sk-controlled', model=first_model)
        service = Service(database, engine); service.memory = Memory(database, SummaryModel(fail=True))
        first_run = (await service.submit(uid, sid, 'My favorite color is white'))['run_id']
        await wait_service(service, first_run)
        source = database.one("SELECT id FROM messages WHERE session_id=? AND role='user'", (sid,))['id']
        assert not database.facts(uid) and database.one('SELECT * FROM pending_memory_summaries')
        await engine.client.close()
    reloaded = Database(database.path)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        model = ScriptedModel(responses=[AIMessage(content='Your favorite color is white.')])
        engine = Engine(reloaded, ScriptedBrowser(), saver, 'sk-controlled', model=model)
        service = Service(reloaded, engine)
        fact = stable_fact(source, 'My favorite color is white', 'Favorite color white', 'preference.color')
        service.memory = Memory(reloaded, SummaryModel(Digest(summary='Personal preference.', facts=[fact])))
        fresh = await service.new_session(uid)
        run = (await service.submit(uid, fresh, 'What is my favorite color?'))['run_id']
        await wait_service(service, run)
        supplied = json.loads(model.seen[0][-1].content.split('\n', 1)[1])
        assert supplied['long_term_facts'][0]['text'] == 'Favorite color white'
        assert not supplied['summary'] and not supplied['recent_messages'] and not supplied['unsummarized_messages']
        assert not reloaded.one('SELECT * FROM pending_memory_summaries')
        await engine.client.close()
