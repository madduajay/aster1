import asyncio
from types import SimpleNamespace

import pytest

from personal_agent import config
from personal_agent.memory import Memory
from personal_agent.memory_profile import MemoryConsolidator,ProfileDraft,ProfileGrounding,read_profile
from personal_agent.service import Service


QUERY='Can you check Carbone availability in New York on 2099-12-01 for 2?'


def local_service(database):
    # Memory worker tests do not require a model client or dining connection.
    service=Service(database,SimpleNamespace(model=None))
    clock=[100.];service.background.clock=lambda:clock[0]
    return service,clock


def add_fact(database,uid,text='My favorite color is blue'):
    sid=database.new_session(uid);mid=database.message(uid,sid,'user',text)
    fid,_=database.put_fact(uid,'Favorite color is blue',mid,'preference.color',evidence=text)
    return sid,mid,fid


class ReviewModel:
    def __init__(self,draft,supported=True,before_check=None):self.draft,self.supported,self.before_check=draft,supported,before_check;self.calls=0
    def with_structured_output(self,schema,**kwargs):
        async def invoke(messages,**config):
            self.calls+=1
            if schema is ProfileDraft:return self.draft
            if self.before_check:self.before_check()
            return ProfileGrounding(supported=self.supported)
        return SimpleNamespace(ainvoke=invoke)


async def test_memory_summary_is_grounded_scoped_and_not_recomputed_unchanged(database,users):
    uid=users[0]['id'];sid,mid,fid=add_fact(database,uid)
    model=ReviewModel(ProfileDraft(summary='The user prefers blue.',fact_ids=[fid]))
    consolidator=MemoryConsolidator(database,model)
    assert await consolidator.consolidate(uid)=='consolidated'
    assert read_profile(database,uid)['fact_ids']==[fid]
    assert read_profile(database,users[1]['id']) is None
    context=Memory(database,None).context(uid,sid,'')
    assert context['consolidated_profile']['summary']=='The user prefers blue.'
    assert fid in context['memory_dependencies']
    assert await consolidator.consolidate(uid)=='unchanged' and model.calls==2


async def test_forgetting_or_correction_cannot_restore_a_derived_profile(database,users):
    uid=users[0]['id'];sid,mid,fid=add_fact(database,uid)
    model=ReviewModel(ProfileDraft(summary='The user prefers blue.',fact_ids=[fid]))
    await MemoryConsolidator(database,model).consolidate(uid)
    database.forget(uid,str(fid));assert read_profile(database,uid) is None
    assert await MemoryConsolidator(database,model).consolidate(uid)=='empty'
    assert database.one('SELECT COUNT(*) n FROM facts WHERE user_id=?',(uid,))['n']==0


async def test_concurrent_forget_invalidates_inflight_consolidation(database,users):
    uid=users[0]['id'];sid,mid,fid=add_fact(database,uid)
    model=ReviewModel(ProfileDraft(summary='The user prefers blue.',fact_ids=[fid]),before_check=lambda:database.forget(uid,str(fid)))
    assert await MemoryConsolidator(database,model).consolidate(uid)=='stale'
    assert read_profile(database,uid) is None


async def test_correction_rebuilds_profile_only_from_current_evidence(database,users):
    uid=users[0]['id'];sid,mid,fid=add_fact(database,uid)
    await MemoryConsolidator(database,ReviewModel(ProfileDraft(summary='The user prefers blue.',fact_ids=[fid]))).consolidate(uid)
    service,_=local_service(database)
    text=f'/correct {fid} Favorite color is green'
    correction=database.message(uid,sid,'user',text)
    service.command(uid,sid,correction,text)
    assert read_profile(database,uid) is None
    current=database.one("SELECT id FROM facts WHERE user_id=? AND status='active'",(uid,))['id']
    assert await MemoryConsolidator(database,ReviewModel(ProfileDraft(summary='The user prefers green.',fact_ids=[current]))).consolidate(uid)=='consolidated'
    assert 'green' in read_profile(database,uid)['summary']
    database.execute("INSERT INTO context_exclusions VALUES(?,'test source removal')",(correction,))
    assert read_profile(database,uid) is None
    await service.close()


async def test_worker_recovers_from_transient_database_error(database,users):
    import sqlite3
    service,clock=local_service(database);original=service.background.seed_memory;attempts=[]
    def seed():
        attempts.append(True)
        if len(attempts)==1:raise sqlite3.OperationalError('controlled busy')
        original()
    service.background.seed_memory=seed;service.background.start()
    try:
        for _ in range(30):
            if len(attempts)>1:break
            await asyncio.sleep(.1)
        assert len(attempts)>1 and not service.background.runner.done()
    finally:await service.close()


@pytest.mark.parametrize('bad_id,supported',[(True,True),(False,False)])
async def test_unsupported_summaries_are_not_injected(database,users,bad_id,supported):
    uid=users[0]['id'];sid,mid,fid=add_fact(database,uid)
    model=ReviewModel(ProfileDraft(summary='The user prefers red.',fact_ids=[fid+100 if bad_id else fid]),supported=supported)
    assert await MemoryConsolidator(database,model).consolidate(uid) in {'rejected','ungrounded'}
    assert read_profile(database,uid) is None


async def test_foreground_activity_cancels_slow_memory(database,users,monkeypatch):
    service,clock=local_service(database);uid=users[0]['id']
    gate=asyncio.Event();started=asyncio.Event()
    async def slow_review(user):started.set();await gate.wait()
    service.background.consolidator.consolidate=slow_review
    monkeypatch.setattr(config,'MEMORY_IDLE_SECONDS',0)
    service.background.queue_memory(uid);service.background.start()
    try:
        await asyncio.wait_for(started.wait(),2)
        service.background.foreground(uid)
        await asyncio.sleep(.05)
        assert not service.user_lock(uid).locked()
    finally:await service.close()
