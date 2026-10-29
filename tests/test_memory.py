import sqlite3
import time
from types import SimpleNamespace

import pytest

from personal_agent.db import Database
from personal_agent.memory import Digest, Fact, Memory
from personal_agent.service import Service
from conftest import SummaryModel


async def test_summary_coverage_followup_and_restart(database, users):
    uid = users[0]['id']
    sid = database.new_session(uid)
    first = database.message(uid, sid, 'user', 'Remember that I prefer quiet hotels. Find one in Phoenix.')
    last = database.message(uid, sid, 'assistant', 'Here are quiet hotel choices.')
    model = SummaryModel(Digest(summary='User wants a quiet Phoenix hotel.', facts=[
        Fact(text='Prefers quiet hotels', source_message=first, importance=5, memory_key='preference.hotels',
             evidence='I prefer quiet hotels', remember_requested=True)]))
    memory = Memory(database, model)
    await memory.summarize(uid, sid)
    assert database.owned_session(uid, sid)['covered'] == last
    database.message(uid, sid, 'user', 'Which one is downtown?')
    reloaded = Memory(Database(database.path), model).context(uid, sid, 'hotels')
    assert reloaded['summary'] == ''  # Entire short session is kept exact, without duplicate summary.
    assert reloaded['recent_messages'][0]['id'] == first
    assert database.owned_session(uid, sid)['summary'].endswith('User wants a quiet Phoenix hotel.')
    assert [m['content'] for m in reloaded['unsummarized_messages']] == ['Which one is downtown?']
    assert reloaded['long_term_facts'][0]['text'] == 'Prefers quiet hotels'


async def test_failed_summary_preserves_original_context(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    database.message(uid, sid, 'user', 'Keep this exact unresolved request.')
    memory = Memory(database, SummaryModel(fail=True))
    with pytest.raises(RuntimeError):
        await memory.summarize(uid, sid)
    assert memory.context(uid, sid, '')['unsummarized_messages'][0]['content'] == 'Keep this exact unresolved request.'
    assert database.owned_session(uid, sid)['covered'] == 0


async def test_forget_removes_fts_and_stale_summary(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    source = database.message(uid, sid, 'user', 'Remember my favorite color is violet.')
    model = SummaryModel(Digest(summary='Favorite color violet', facts=[Fact(text='Favorite color violet', source_message=source,
        importance=5, memory_key='preference.color', evidence='my favorite color is violet', remember_requested=True)]))
    memory = Memory(database, model)
    await memory.summarize(uid, sid)
    fid = database.facts(uid, 'violet')[0]['id']
    database.forget(uid, fid)
    assert database.facts(uid, 'violet') == []
    assert memory.context(uid, sid, 'violet')['summary'] == ''
    assert memory.context(uid, sid, 'violet')['unsummarized_messages'] == []
    assert memory.context(uid, sid, 'violet')['recent_messages'] == []
    assert database.rows('SELECT * FROM messages WHERE session_id=?', (sid,))
    database.message(uid, sid, 'user', 'What do you remember?')
    assert len(memory.context(uid, sid, '')['unsummarized_messages']) == 1


async def test_facts_require_user_provenance_and_exclude_secrets(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    a = database.message(uid, sid, 'assistant', 'User earns a million dollars.')
    u = database.message(uid, sid, 'user', 'My password is abc123.')
    model = SummaryModel(Digest(summary='Private details omitted', facts=[
        Fact(text='Earns a million dollars', source_message=a, importance=5),
        Fact(text='Password abc123', source_message=u, importance=5),
        Fact(text='Likes green', source_message=999999, importance=5)]))
    await Memory(database, model).summarize(uid, sid)
    assert database.facts(uid) == []


async def test_long_summary_backlog_has_no_gaps(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    ids = [database.message(uid, sid, 'user', str(n) + 'x' * 15000) for n in range(7)]
    model = SummaryModel()
    await Memory(database, model).summarize(uid, sid)
    covered_ids = [m['id'] for batch in model.inputs for m in batch['messages']]
    assert covered_ids == ids
    assert database.owned_session(uid, sid)['covered'] == ids[-1]
    assert len(model.inputs) > 1


async def test_memory_never_leaks_between_users(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    mid = database.message(uid, sid, 'user', 'Remember I like astronomy.')
    await Memory(database, SummaryModel(Digest(summary='Likes astronomy', facts=[Fact(text='Likes astronomy',source_message=mid,
        importance=5, memory_key='preference.astronomy', evidence='I like astronomy', remember_requested=True)]))).summarize(uid, sid)
    assert database.facts(uid)
    other = users[1]['id']; other_session = database.new_session(other)
    assert Memory(database, None).context(other, other_session, 'astronomy')['long_term_facts'] == []
    with pytest.raises(LookupError):
        Memory(database, None).context(other, sid, '')


async def test_ordinary_tasks_and_yes_cannot_be_promoted_even_with_high_importance(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    for content in ['Dinner tomorrow for 2 on Resy in Phoenix', 'yes indian please', 'what about other states ?',
                    'what about utah', 'Remember what I asked earlier?', 'Find the best electronic deals']:
        mid = database.message(uid, sid, 'user', content)
        result = Digest(summary='Session-only task.', facts=[Fact(text='Prefers Indian restaurants', source_message=mid,
            importance=5, memory_key='preference.diet', evidence=content, remember_requested=True)])
        await Memory(database, SummaryModel(result)).summarize(uid, sid)
    assert database.facts(uid, inspect=True) == []


async def test_same_user_new_session_has_no_old_task_memory(database, users):
    uid = users[0]['id']; old = database.new_session(uid)
    database.message(uid, old, 'user', 'Dinner tomorrow for two on Resy in Phoenix. Indian please.')
    database.message(uid, old, 'assistant', 'Here are Indian restaurants on Resy.')
    await Memory(database, SummaryModel(Digest(summary='Resy dinner tomorrow for two in Phoenix.'))).summarize(uid, old)
    new = database.new_session(uid)
    database.message(uid, new, 'user', 'Which states should I visit?')
    context = Memory(database, None).context(uid, new, 'other states')
    assert context['long_term_facts'] == []
    assert 'Resy' not in str(context) and 'Phoenix' not in str(context)


async def test_promotion_requires_grounded_evidence_and_explicit_intent(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    mid = database.message(uid, sid, 'user', 'Please remember I prefer vegetarian food.')
    result = Digest(summary='User explicitly saved a dietary preference.', facts=[
        Fact(text='Prefers vegetarian food', source_message=mid, importance=5, memory_key='preference.diet',
             evidence='I prefer vegetarian food', remember_requested=True),
        Fact(text='Lives in Utah', source_message=mid, importance=5, memory_key='profile.home',
             evidence='I live in Utah', remember_requested=True)])
    await Memory(database, SummaryModel(result)).summarize(uid, sid)
    new = database.new_session(uid)
    assert [f['text'] for f in Memory(database, None).context(uid, new, '')['long_term_facts']] == ['Prefers vegetarian food']


async def test_recent_window_and_older_snapshot_cover_history_exactly_once(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    model = SummaryModel(); memory = Memory(database, model)
    ids = []
    for n in range(9):
        ids.append(database.message(uid, sid, 'user', f'Topic {n}. Latest correction {n}'))
        ids.append(database.message(uid, sid, 'assistant', f'Reply {n}'))
        await memory.summarize(uid, sid)
    current = database.message(uid, sid, 'user', 'Use the latest correction')
    context = Memory(Database(database.path), model).context(uid, sid, '')
    assert context['covered_through_message'] == ids[5]
    assert context['summary']
    assert [m['id'] for m in context['recent_messages']] == ids[6:]
    assert [m['id'] for m in context['unsummarized_messages']] == [current]
    assert all(m['id'] > context['covered_through_message'] for m in context['recent_messages'])
    assert context['recent_messages'][-2]['content'] == 'Topic 8. Latest correction 8'


async def test_summary_failure_retains_recent_exact_and_unsummarized_messages(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    first = database.message(uid, sid, 'user', 'Original subject')
    database.message(uid, sid, 'assistant', 'Original answer')
    await Memory(database, SummaryModel()).summarize(uid, sid)
    last = database.message(uid, sid, 'user', 'New correction overrides the original')
    with pytest.raises(RuntimeError):
        await Memory(database, SummaryModel(fail=True)).summarize(uid, sid)
    context = Memory(Database(database.path), None).context(uid, sid, '')
    assert context['recent_messages'][0]['id'] == first
    assert context['unsummarized_messages'][0]['id'] == last


def test_permanent_corrections_supersede_old_value_and_preserve_other_sessions(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    other = database.new_session(uid)
    mid = database.message(uid, sid, 'user', '/remember I prefer tea')
    old, _ = database.put_fact(uid, 'Prefers tea', mid, 'preference.drink')
    untouched = database.message(uid, other, 'user', 'Plan a hiking route')
    correction = database.message(uid, sid, 'user', '/correct 1 Prefers coffee')
    new, _ = database.put_fact(uid, 'Prefers coffee', correction, 'preference.drink')
    assert database.one('SELECT status FROM facts WHERE id=?', (old,))['status'] == 'superseded'
    assert database.one('SELECT supersedes FROM facts WHERE id=?', (new,))['supersedes'] == old
    assert [f['text'] for f in database.facts(uid)] == ['Prefers coffee']
    context = Memory(Database(database.path), None).context(uid, sid, '')
    assert 'Prefers tea' not in str(context) and 'prefer tea' not in str(context)
    assert Memory(database, None).context(uid, other, '')['unsummarized_messages'][0]['id'] == untouched


def test_fts_recency_relevance_and_stopwords(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    now = time.time()
    saved = []
    for n, text in enumerate(['Prefers quiet hotels', 'Prefers modern hotels', 'Enjoys astronomy']):
        mid = database.message(uid, sid, 'user', '/remember ' + text)
        fid, _ = database.put_fact(uid, text, mid, f'preference.item{n}')
        database.execute('UPDATE facts SET updated=? WHERE id=?', (now - (100 if n == 0 else 0) * 86400, fid))
        saved.append(fid)
    assert database.facts(uid, 'hotels')[0]['id'] == saved[1]
    assert database.facts(uid, 'quiet hotels')[0]['id'] == saved[0]  # Relevance still matters.
    assert database.facts(uid, 'what about other states')[0]['id'] == saved[2]  # No generic 'what' match.


async def test_forget_removes_derived_recall_but_retains_unrelated_communication(database, users):
    uid = users[0]['id']; source_session = database.new_session(uid); derived = database.new_session(uid)
    mid = database.message(uid, source_session, 'user', '/remember Favorite color violet')
    fid, _ = database.put_fact(uid, 'Favorite color violet', mid, 'preference.color')
    keep = database.message(uid, source_session, 'user', 'Discuss Python packaging')
    output = database.message(uid, derived, 'assistant', 'Your favorite color is violet')
    database.record_dependencies(uid, derived, output, [fid])
    database.message(uid, derived, 'user', 'Also discuss local hiking')
    await Memory(database, SummaryModel()).summarize(uid, derived)
    database.forget(uid, fid)
    for sid in [source_session, derived]:
        context = Memory(database, None).context(uid, sid, '')
        assert 'violet' not in str(context).lower()
    assert any(m['id'] == keep for m in Memory(database, None).context(uid, source_session, '')['unsummarized_messages'])
    assert database.rows('SELECT * FROM messages WHERE id=?', (output,))  # Visible history is preserved.


async def test_explicit_chat_commands_and_quarantine_review(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    service = Service(database, SimpleNamespace(model=SummaryModel()))
    result = await service.submit(uid, sid, '/remember I prefer quiet hotels')
    assert 'Saved permanent memory' in result['reply']
    fid = database.facts(uid)[0]['id']
    await service.submit(uid, sid, f'/correct {fid} I prefer modern hotels')
    facts = database.facts(uid)
    assert len(facts) == 1 and facts[0]['text'] == 'I prefer modern hotels'
    assert 'I prefer quiet hotels' not in str(Memory(database, None).context(uid, sid, ''))
    await service.submit(uid, sid, '/forget all')
    assert database.facts(uid) == []
    assert 'modern hotels' not in str(Memory(database, None).context(uid, sid, ''))
    assert 'No permanent memory' in (await service.submit(uid, sid, '/memory'))['reply']


def test_schema_upgrade_backs_up_quarantines_and_invalidates_legacy_summary(tmp_path, monkeypatch):
    from personal_agent import config
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    path = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT,password TEXT,must_change INTEGER DEFAULT 0,memory_version INTEGER DEFAULT 0);
            CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id TEXT,title TEXT,summary TEXT DEFAULT '',covered INTEGER DEFAULT 0,context_floor INTEGER DEFAULT 0,created REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,created REAL);
            CREATE TABLE facts(id INTEGER PRIMARY KEY,user_id TEXT,text TEXT,normalized TEXT,source_message INTEGER,importance INTEGER,created REAL,UNIQUE(user_id,normalized));
            INSERT INTO users VALUES('u','user01','hash',0,0);
            INSERT INTO sessions VALUES('s','u','Old chat','Inferred Resy preference',2,0,0);
            INSERT INTO messages VALUES(1,'s','user','yes indian please',0);
            INSERT INTO messages VALUES(2,'s','assistant','Resy tomorrow for 2',0);
            INSERT INTO facts VALUES(1,'u','Prefers Resy tomorrow for 2','prefers resy tomorrow for 2',1,5,0);
        ''')
    upgraded = Database(path)
    assert upgraded.facts('u') == []
    assert upgraded.one('SELECT status FROM facts')['status'] == 'quarantined'
    assert upgraded.owned_session('u', 's')['summary'] == ''
    assert len(upgraded.visible_messages('u', 's')) == 2
    assert path.with_name('legacy.before-memory-v2.sqlite').exists()
    # Idempotent re-open does not quarantine explicitly re-saved facts.
    mid = upgraded.message('u', 's', 'user', '/remember Prefers vegetarian food')
    upgraded.put_fact('u', 'Prefers vegetarian food', mid, 'preference.diet')
    assert Database(path).facts('u')[0]['text'] == 'Prefers vegetarian food'


async def test_old_explicit_requests_are_not_repromoted_during_rebuild(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    mid = database.message(uid, sid, 'user', 'Remember I prefer tea')
    database.execute('UPDATE users SET memory_opt_in_after=? WHERE id=?', (mid, uid))
    fact = Fact(text='Prefers tea', source_message=mid, importance=5, memory_key='preference.drink',
                evidence='I prefer tea', remember_requested=True)
    await Memory(database, SummaryModel(Digest(summary='User discussed tea', facts=[fact]))).summarize(uid, sid)
    assert database.facts(uid, inspect=True) == []


async def test_natural_correction_rebuilds_summary_without_old_value(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    first = database.message(uid, sid, 'user', 'Remember I prefer tea')
    database.put_fact(uid, 'Prefers tea', first, 'preference.drink')
    await Memory(database, SummaryModel(Digest(summary='Prefers tea'))).summarize(uid, sid)
    latest = database.message(uid, sid, 'user', 'Remember I prefer coffee instead')
    fact = Fact(text='Prefers coffee', source_message=latest, importance=5, memory_key='preference.drink',
                evidence='I prefer coffee instead', remember_requested=True)
    model = SummaryModel(Digest(summary='User corrected preference to coffee', facts=[fact]))
    await Memory(database, model).summarize(uid, sid)
    assert len(model.inputs) == 2  # First invalidates, second rebuilds from retained communication.
    assert all(m['id'] != first for m in model.inputs[-1]['messages'])
    assert model.inputs[-1]['previous_summary'] == ''
    assert [f['text'] for f in database.facts(uid)] == ['Prefers coffee']
    assert 'prefer tea' not in str(Memory(database, None).context(uid, sid, '')).lower()


async def test_concurrent_forget_cannot_be_undone_by_inflight_digest(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    mid = database.message(uid, sid, 'user', 'Remember I prefer tea')
    fid, _ = database.put_fact(uid, 'Prefers tea', mid, 'preference.drink')
    fact = Fact(text='Prefers tea', source_message=mid, importance=5, memory_key='preference.drink',
                evidence='I prefer tea', remember_requested=True)
    class ForgettingModel(SummaryModel):
        async def ainvoke(self, messages):
            database.forget(uid, fid)
            return Digest(summary='Stale tea preference', facts=[fact])
    await Memory(database, ForgettingModel()).summarize(uid, sid)
    assert database.facts(uid) == []
    assert 'tea' not in str(Memory(database, None).context(uid, sid, '')).lower()


async def test_quarantined_fact_can_be_explicitly_corrected_without_deleting_replacement(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    mid = database.message(uid, sid, 'user', 'yes indian please')
    fid = database.execute("INSERT INTO facts(user_id,text,normalized,source_message,importance,created,memory_key) VALUES(?,?,?,?,?,0,'legacy.test')",
                           (uid, 'Prefers dinner tomorrow for two', 'prefers dinner tomorrow for two', mid, 5))
    service = Service(database, SimpleNamespace(model=SummaryModel()))
    assert 'quarantined' in (await service.submit(uid, sid, '/memory review'))['reply']
    assert not database.facts(uid)
    await service.submit(uid, sid, f'/correct {fid} I prefer vegetarian food')
    assert [f['text'] for f in database.facts(uid)] == ['I prefer vegetarian food']
    await service.submit(uid, sid, '/forget all')
    assert not database.facts(uid)
    assert 'dinner tomorrow' not in str(Memory(database, None).context(uid, sid, '')).lower()


async def test_summary_keeps_user_intent_separate_from_assistant_inference(database, users):
    uid = users[0]['id']; sid = database.new_session(uid)
    database.message(uid, sid, 'user', 'What shopping and travel interests are known?')
    database.message(uid, sid, 'assistant', 'I infer you want Indian dinner on Resy tomorrow for two.')
    database.message(uid, sid, 'user', 'What about other states?')
    model = SummaryModel(Digest(summary='User likes Indian dinner tomorrow for two.'))
    await Memory(database, model).summarize(uid, sid)
    stored = database.owned_session(uid, sid)['summary']
    originals, synopsis = stored.split('Conversation synopsis', 1)
    assert 'What shopping and travel interests are known?' in originals
    assert 'What about other states?' in originals
    assert 'Indian' not in originals and 'tomorrow' not in originals
    assert 'may contain assistant assumptions, not user decisions' in synopsis
    assert not database.facts(uid)
