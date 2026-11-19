"""One-time local memory upgrade/summary repair. Stop the app before running.

Creates an owner-only SQLite backup through Database's schema migration.
No chat history is removed. At most 24 paid summary calls, no web/tools.
"""
import asyncio
import json
import time

from langchain_openai import ChatOpenAI

from manage import owner
from memory_acceptance import BudgetModel
from personal_agent import config
from personal_agent.db import Database
from personal_agent.memory import Memory


async def main():
    if owner() is not None:
        raise RuntimeError('Stop the application before upgrading memory')
    key = config.keychain_key()
    db = Database()
    model = ChatOpenAI(model=config.MODEL, api_key=key, base_url='https://api.openai.com/v1',
        max_tokens=2048, max_retries=0, timeout=45, temperature=.2, use_responses_api=False, store=False)
    del key
    budget = BudgetModel(model)
    memory = Memory(db, budget)
    report = {'completed': False, 'model': config.MODEL, 'sessions_rebuilt': 0, 'sessions_pending': 0,
              'started': time.time(), 'quarantined_facts': db.one("SELECT COUNT(*) AS n FROM facts WHERE status='quarantined'")['n']}
    for row in db.rows('SELECT id,user_id FROM sessions ORDER BY created'):
        try:
            await memory.summarize(row['user_id'], row['id'])
            report['sessions_rebuilt'] += 1
        except Exception as exc:
            report['sessions_pending'] += 1
            report.setdefault('error_classes', []).append(type(exc).__name__)
    report.update(completed=not report['sessions_pending'], summary_calls=budget.calls,
                  active_facts=db.one("SELECT COUNT(*) AS n FROM facts WHERE status='active'")['n'],
                  finished=time.time())
    (config.RUNTIME / 'memory-upgrade.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    if not report['completed']:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
