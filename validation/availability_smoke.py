"""Exercise real main/subagent models against local Company services only.

Uses a temporary main database and synthetic ownership; no real user's chat,
login, browser, booking, or alerts are modified. All selected options are available.
"""
import asyncio
import json
import secrets
import tempfile
import time
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from availability.security import Security
from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.db import Database
from personal_agent.service import Service


class NoBrowser:
    async def close_user(self,user):pass


async def main():
    runtime=config.RUNTIME;security=Security(runtime);key=config.keychain_key()
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='aster-availability-smoke-') as directory:
        config.RUNTIME=Path(directory)
        database=Database(config.RUNTIME/'main.sqlite')
        user='validation-'+secrets.token_hex(8)
        database.execute('INSERT INTO users(id,username,password,must_change) VALUES(?,?,?,0)',(user,user,'unused'))
        session=database.new_session(user)
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME/'main-checkpoints.sqlite')) as saver:
            engine=Engine(database,NoBrowser(),saver,key);del key
            service=Service(database,engine);service.availability.security=security
            try:
                request=await service.submit(user,session,
                    'Check these three availabilities: Nobu in New York on December 1, 2026 for 2 people; '
                    'The Plaza in New York, check-in December 1, 2026, check-out December 3, 2026, 2 guests, 1 room; '
                    'Emirates business class from JFK to Dubai on December 1, 2026, 1 passenger.')
                await asyncio.wait_for(service.tasks[request['run_id']],180)
                run=database.one('SELECT status,error FROM runs WHERE id=?',(request['run_id'],))
                results=[json.loads(row['payload']) for row in database.rows("SELECT payload FROM events WHERE kind='availability_result'")]
                categories={r['domain']:r for group in results for r in group['results']}
                assert run['status']=='completed',run
                assert set(categories)=={'dining','hotels','flights'},'Not all categories were delegated'
                assert all(r['status']=='completed' and r['available'] for r in categories.values()),'Availability check did not succeed'
                assert not service.availability.store.rows('SELECT * FROM monitors'),'One-off request created a recurring monitor'
                proof={'status':'passed','seconds':round(time.monotonic()-started,2),'categories':sorted(categories),
                    'main_model':config.MODEL,'subagent_model':'gpt-5.4-mini','chat':database.one("SELECT content FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1")['content']}
                (runtime/'availability-smoke.json').write_text(json.dumps(proof,indent=2))
                print(json.dumps(proof,indent=2))
            finally:
                await service.close();await engine.browser_tasks.close();await engine.client.close()
    config.RUNTIME=runtime


if __name__=='__main__':asyncio.run(main())
