"""Bounded real-model HTTPS/browser memory acceptance in isolated storage.

No production accounts/chats are modified. No search or browser-action tools
are permitted. At most 24 main calls and 24 structured-summary calls.
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import time

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import async_playwright, expect
import uvicorn

from personal_agent import config
from personal_agent.agent import Engine
from personal_agent.app import create_app
from personal_agent.db import Database
from personal_agent.memory import Memory
from personal_agent.service import Service


class BudgetModel:
    def __init__(self, model):
        self.model, self.calls = model, 0

    def with_structured_output(self, *args, **kwargs):
        runnable = self.model.with_structured_output(*args, **kwargs)
        parent = self
        class Bounded:
            async def ainvoke(self, messages):
                parent.calls += 1
                if parent.calls > 24:
                    raise RuntimeError('Summary acceptance budget exhausted')
                return await runnable.ainvoke(messages)
        return Bounded()


class AcceptanceEngine(Engine):
    calls = 0
    contexts = None

    def graph(self, user, run, context, max_calls=12, max_searches=3):
        self.contexts.append(json.loads(json.dumps(context)))
        return super().graph(user, run, context, min(max_calls, 2), 0)

    def count(self, run, column, limit):
        if column == 'model_calls':
            self.calls += 1
            if self.calls > 24:
                raise RuntimeError('Chat acceptance budget exhausted')
        return super().count(run, column, limit)


class NoBrowserActions:
    async def close_user(self, user):
        pass

    def set_scope(self, *args):
        raise RuntimeError('No website actions allowed in memory acceptance')


async def wait_until(predicate, timeout=100):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(.1)
    raise TimeoutError('Acceptance condition not met')


async def main():
    original_runtime, original_origin = config.RUNTIME, config.ORIGIN
    certificate = original_runtime / 'tls/server.pem'
    private_key = original_runtime / 'tls/server.key'
    report = {'passed': False, 'real_model': config.MODEL, 'real_browser': True,
              'isolated_storage': True, 'live_searches': 0}
    server = task = engine = None
    try:
        report['stage'] = 'bootstrap'
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(original_runtime / 'playwright')
        key = config.keychain_key()
        config.RUNTIME = Path(tempfile.mkdtemp(prefix='memory-acceptance-', dir=original_runtime))
        config.prepare()
        db = Database(); db.seed()
        uid = db.one("SELECT id FROM users WHERE username='user01'")['id']
        credentials = dict(line.split() for line in (config.RUNTIME / 'initial-accounts.txt').read_text().splitlines())
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen()
        config.ORIGIN = f'https://aster.localhost:{sock.getsockname()[1]}'
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME / 'checkpoints.sqlite')) as saver:
            engine = AcceptanceEngine(db, NoBrowserActions(), saver, key)
            del key
            engine.contexts = []
            summaries = BudgetModel(engine.model)
            service = Service(db, engine); service.memory = Memory(db, summaries)
            server = uvicorn.Server(uvicorn.Config(create_app(db, service), host='127.0.0.1',
                ssl_certfile=str(certificate), ssl_keyfile=str(private_key), access_log=False,
                log_level='error', server_header=False))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            await wait_until(lambda: server.started)
            problems = []
            report['stage'] = 'browser_login'
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True)
                page = await browser.new_page(ignore_https_errors=False)
                page.on('pageerror', lambda error: problems.append(type(error).__name__))
                await page.goto(config.ORIGIN)
                await page.locator('#username').fill('user01')
                await page.locator('#password').fill(credentials['user01'])
                await page.locator('#password').press('Enter')
                await page.locator('#password-form').wait_for(state='visible')
                await page.locator('#new-password').fill(secrets.token_urlsafe(24))
                await page.locator('#new-password').press('Enter')
                await page.locator('#composer').wait_for(state='visible')
                assert await page.get_by_role('button').all_text_contents() == ['New Chat', 'Logout']

                async def send(text):
                    previous = db.one('SELECT COALESCE(MAX(id),0) AS id FROM messages')['id']
                    await page.locator('#input').fill(text)
                    await page.locator('#input').press('Enter')
                    await wait_until(lambda: db.one("SELECT id FROM messages WHERE role='assistant' AND id>?", (previous,))
                                     and not service.active(uid) and not service.tasks)
                    await expect(page.locator('#status')).to_have_text('', timeout=10000)
                    await expect(page.locator('#input')).to_have_value('')
                    last = db.one("SELECT content FROM messages WHERE role='assistant' ORDER BY id DESC LIMIT 1")['content']
                    assert not db.one("SELECT id FROM runs WHERE status='failed'")
                    assert not await page.locator('#error').inner_text()
                    return last

                report['stage'] = 'session_only_task'
                await send('For this session only, I am considering Indian dinner for two tomorrow in Phoenix on Resy. Do not search, book, or save permanent memory; just acknowledge briefly.')
                dining = db.one('SELECT id FROM sessions ORDER BY created LIMIT 1')['id']
                assert not db.facts(uid, inspect=True)
                await page.get_by_role('button', name='New Chat', exact=True).click()
                await expect(page.locator('#messages')).to_have_text('')
                await send('We are reviewing the kinds of things a person likes to shop for and places they might want to visit. No preferences or destinations have been confirmed. Briefly ask what information I want to explore. Do not search.')
                report['stage'] = 'ambiguous_other_states'
                answer = await send('what about other states ?')
                assert 'resy' not in answer.lower() and 'indian' not in answer.lower()
                assert '?' in answer or 'clarif' in answer.lower()
                assert all('Resy' not in str(ctx) for ctx in engine.contexts[1:])
                assert not db.facts(uid, inspect=True)
                report['cross_session_isolation'] = True
                report['ambiguous_followup_clarifies'] = True

                report['stage'] = 'original_profile_followup_replay'
                await page.get_by_role('button', name='New Chat', exact=True).click()
                await expect(page.locator('#messages')).to_have_text('')
                replay = db.one('SELECT id FROM sessions ORDER BY created DESC LIMIT 1')['id']
                db.message(uid, replay, 'user', 'what are the kind of things this user likes to shop and where does he want to go and visit')
                db.message(uid, replay, 'assistant', 'Earlier interests include white shirts, Nike, Amex, electronics, and Indian dinner for two tomorrow on Resy in Phoenix. The user may want to visit Phoenix.')
                answer = await send('what about other states ?')
                assert '?' in answer or 'clarif' in answer.lower()
                assert not db.one('SELECT run_id FROM run_usage WHERE searches>0')
                assert not db.one('SELECT id FROM browser_use_tasks')
                assert not db.facts(uid, inspect=True)
                report['original_followup_replay_clarifies'] = True

                report['stage'] = 'same_session_topic'
                await page.locator('#input').fill('/open ' + dining)
                await page.locator('#input').press('Enter')
                await expect(page.locator('#messages')).to_contain_text('Phoenix on Resy')
                answer = await send('What about other states? Keep the same dining topic. Do not search or invent availability. Briefly restate my dining constraints and ask which state.')
                assert 'indian' in answer.lower() and ('two' in answer.lower() or '2' in answer)
                report['valid_same_session_followup'] = True

                report['stage'] = 'permanent_memory_and_correction'
                await page.get_by_role('button', name='New Chat', exact=True).click()
                await expect(page.locator('#messages')).to_have_text('')
                await send('/remember I prefer vegetarian food')
                fid = db.facts(uid)[0]['id']
                await page.get_by_role('button', name='New Chat', exact=True).click()
                await expect(page.locator('#messages')).to_have_text('')
                assert 'vegetarian' in (await send('What food do I prefer? Answer only from permanent memory without searching.')).lower()
                await send(f'/correct {fid} I prefer vegan food')
                await page.get_by_role('button', name='New Chat', exact=True).click()
                await expect(page.locator('#messages')).to_have_text('')
                answer = await send('What food do I prefer? Answer only from permanent memory without searching.')
                assert 'vegan' in answer.lower() and 'vegetarian' not in answer.lower()
                report['permanent_memory_across_chats'] = True
                report['newer_correction_wins'] = True

                report['stage'] = 'forget_without_stale_recall'
                fid = db.facts(uid)[0]['id']
                await send(f'/forget {fid}')
                answer = await send('What dietary preference do you remember about me? Use only current memory; do not guess or search.')
                assert 'vegan' not in answer.lower() and 'vegetarian' not in answer.lower()
                assert not db.facts(uid, inspect=True)
                report['forget_without_stale_recall'] = True

                report['stage'] = 'natural_explicit_remember'
                await send('Please remember that I prefer quiet hotels for future chats. Acknowledge briefly without searching.')
                assert any('quiet hotels' in f['text'].lower() for f in db.facts(uid, inspect=True))
                report['explicit_natural_memory'] = True
                assert not db.one('SELECT run_id FROM run_usage WHERE searches>0')
                assert not db.one('SELECT id FROM browser_use_tasks')
                assert not problems
                assert db.one("SELECT id FROM events WHERE kind='token'")
                await page.screenshot(path=str(original_runtime / 'memory-acceptance-ui.png'))
                await page.get_by_role('button', name='Logout', exact=True).click()
                await page.locator('#login-form').wait_for(state='visible')
                await browser.close()
            await service.close()
            await engine.client.close()
            report.update(passed=True, main_model_calls=engine.calls, summary_calls=summaries.calls,
                          browser_errors=problems, streamed_answers=True, https_trust_verified=True,
                          buttons=['New Chat', 'Logout'])
    except Exception as exc:
        report.update(error_class=type(exc).__name__)
    finally:
        if server:
            server.should_exit = True
        if task:
            await task
        if engine:
            await engine.client.close()
        config.RUNTIME, config.ORIGIN = original_runtime, original_origin
        (original_runtime / 'memory-acceptance.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    os.environ['LANGSMITH_TRACING'] = 'false'
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    asyncio.run(main())
