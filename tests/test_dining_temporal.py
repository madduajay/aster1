import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from personal_agent.service import Service
from personal_agent.dining_workflow import DiningWorkflow
from dining_test_helpers import start_dining_runtime,agent_service,lookup_messages,submit_agent
from test_background import QUERY


@pytest.mark.skipif(os.environ.get('RUN_DINING_TESTS')!='1',reason='Enable local Temporal/MCP integration tests')
async def test_signal_resumes_workflow_after_worker_restart_without_duplicate_alerts(database,users):
    service=agent_service(database,lookup_messages()+lookup_messages(venue='Nobu',reply='Nobu: 18:30, 19:00, 19:30.')+lookup_messages(reply='Your watch is already active.')+lookup_messages(party_size=3));cleanup=await start_dining_runtime(service,database.path.parent)
    uid=users[0]['id'];sid=database.new_session(uid);started=time.monotonic()
    try:
        initial=await submit_agent(service,uid,sid,QUERY)
        assert 'currently unavailable' in initial['reply']
        watch=service.background.snapshot(uid)['watches'][0]
        handle=cleanup.environment.client.get_workflow_handle('aster-dining-'+watch['id'])
        result=await submit_agent(service,uid, database.new_session(uid),QUERY.replace('Carbone','Nobu'))
        assert '18:30, 19:00, 19:30' in result['reply']
        assert 'already active' in (await submit_agent(service,uid,sid,QUERY))['reply']
        await submit_agent(service,uid,database.new_session(uid),QUERY.replace('for 2','for 3'))
        cancelled=database.one("SELECT id FROM dining_watches WHERE criteria LIKE '%\"party_size\": 3%'")['id']
        service.background.cancel_watch(uid,cancelled)
        cancelled_handle=cleanup.environment.client.get_workflow_handle('aster-dining-'+cancelled)
        await handle.signal('availability_opened',{'available':True,'slots':['01:00'],'location':'Wrong city'})
        await service.dining.close()
        # Company continues running and signals Temporal while Aster's worker is down.
        async with asyncio.timeout(15):
            while True:
                with cleanup.company.state.inventory.connect() as conn:
                    pending=conn.execute('SELECT COUNT(*) FROM requests WHERE due IS NOT NULL AND sent IS NULL').fetchone()[0]
                if not pending:break
                await asyncio.sleep(.1)
        assert time.monotonic()-started>=9.5
        assert not service.background.snapshot(uid)['notifications']
        history=await handle.fetch_history()
        assert any(event.HasField('workflow_execution_signaled_event_attributes') for event in history.events)
        await service.dining.start()
        opening=await asyncio.wait_for(handle.result(),10)
        await asyncio.wait_for(cancelled_handle.result(),10)
        assert opening['available'] and opening['slots']==['19:00']
        assert len(service.background.snapshot(uid)['notifications'])==1
        assert not service.background.snapshot(users[1]['id'])['notifications']
        updates=service.background.snapshot(uid)['chat_updates']
        assert len(updates)==1 and updates[0]['session_id']==sid
        assert 'A table has opened up at Carbone' in updates[0]['content']
        assert '19:00' in updates[0]['content']
        assert not service.background.snapshot(users[1]['id'])['chat_updates']
        assert database.one("SELECT COUNT(*) n FROM background_jobs WHERE kind='carbone_mock'")['n']==0
        # Retried publish activity and a repeated signal cannot duplicate the notification.
        request={'watch_id':watch['id'],'user_id':uid,'company_query':{k:opening[k] for k in ('request_id','venue','location','date','party_size')}}
        await service.dining.publish_opening({'request':request,'event':opening})
        assert database.one('SELECT COUNT(*) n FROM notifications')['n']==1
        assert database.one('SELECT COUNT(*) n FROM availability_chat_messages')['n']==1
        assert database.one('SELECT content FROM messages WHERE id=?',(updates[0]['id'],))['content']==updates[0]['content']
    finally:await service.close();await cleanup()
