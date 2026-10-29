"""Initial-install browser acceptance. Uses pristine user10, then restores it and removes test data."""
import asyncio
import json
import secrets
import time

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from playwright.async_api import async_playwright, expect

from personal_agent import config
from personal_agent.db import Database


async def wait_until(predicate, timeout=75):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.25)
    raise TimeoutError('Acceptance condition not met')


async def main():
    db = Database()
    original = db.one("SELECT * FROM users WHERE username='user10'")
    uid = original['id']
    if not original['must_change'] or db.one('SELECT id FROM sessions WHERE user_id=?', (uid,)):
        raise RuntimeError('user10 is not pristine; refuse to alter an existing user account')
    credentials = dict(line.split() for line in (config.RUNTIME / 'initial-accounts.txt').read_text().splitlines())
    password = secrets.token_urlsafe(24)
    report = {'passed': False, 'browser_https_verification': True, 'real_model': True}
    problems = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True)
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000}, ignore_https_errors=False)
            page = await context.new_page()
            page.on('pageerror', lambda exc: problems.append(type(exc).__name__))
            report['stage'] = 'login'
            await page.goto(config.ORIGIN, wait_until='networkidle')
            await page.screenshot(path=str(config.RUNTIME / 'login-ui.png'))
            await page.locator('#username').fill('user10')
            await page.locator('#password').fill(credentials['user10'])
            await page.locator('#password').press('Enter')
            await page.locator('#password-form').wait_for(state='visible')
            await page.locator('#new-password').fill(password)
            await page.locator('#new-password').press('Enter')
            await page.locator('#composer').wait_for(state='visible')
            assert await page.get_by_role('button').all_text_contents() == ['New Chat', 'Logout']
            await page.screenshot(path=str(config.RUNTIME / 'chat-ui.png'))
            report['stage'] = 'first_streamed_message'
            await page.locator('#input').fill('My favorite color is teal. Remember that as a stable preference. Reply with one short sentence.')
            await page.locator('#input').press('Enter')
            await wait_until(lambda: db.one("SELECT id FROM runs WHERE user_id=? AND status='completed'", (uid,)))
            await page.locator('.message.assistant').first.wait_for(state='visible')
            await wait_until(lambda: db.one('SELECT summary FROM sessions WHERE user_id=?', (uid,))['summary'])
            assert db.facts(uid, 'teal')
            first_session = db.one('SELECT id FROM sessions WHERE user_id=? ORDER BY created LIMIT 1', (uid,))['id']
            report['stage'] = 'new_chat_memory'
            await page.get_by_role('button', name='New Chat', exact=True).click()
            await wait_until(lambda: len(db.rows('SELECT id FROM sessions WHERE user_id=?', (uid,))) == 2)
            await page.locator('#input').fill('What is my favorite color? Answer from memory without web search.')
            await page.locator('#input').press('Enter')
            await wait_until(lambda: len(db.rows("SELECT id FROM runs WHERE user_id=? AND status='completed'", (uid,))) == 2)
            await wait_until(lambda: db.one("SELECT content FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.user_id=? AND m.role='assistant' ORDER BY m.id DESC LIMIT 1", (uid,)) and
                             'teal' in db.one("SELECT content FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.user_id=? AND m.role='assistant' ORDER BY m.id DESC LIMIT 1", (uid,))['content'].lower())
            await page.locator('.message.assistant').first.wait_for(state='visible')
            assert 'teal' in (await page.locator('#messages').inner_text()).lower()
            report['stage'] = 'reopen'
            await page.locator('#input').fill('/open ' + first_session)
            await page.locator('#input').press('Enter')
            await expect(page.locator('#messages')).to_contain_text('stable preference')
            assert not problems
            assert not await page.locator('#error').inner_text()
            tokens = db.one("SELECT COUNT(*) AS n FROM events e JOIN runs r ON e.run_id=r.id WHERE r.user_id=? AND kind='token'", (uid,))['n']
            assert tokens > 0
            report['stage'] = 'logout'
            await page.get_by_role('button', name='Logout', exact=True).click()
            await page.locator('#login-form').wait_for(state='visible')
            # Use the real browser's system trust; Node's separate request client does not share it.
            assert (await page.goto(config.ORIGIN + '/api/me')).status == 401
            report.update(passed=True, first_login_password_change=True, streamed_chat=True,
                          summary_saved=True, long_term_memory_across_chats=True, session_reopen=True,
                          logout_verified=True, console_errors=problems)
            await browser.close()
    except Exception as exc:
        report['error_class'] = type(exc).__name__
        report['error_detail'] = str(exc).splitlines()[0].replace(password, '[redacted]').replace(credentials['user10'], '[redacted]')
    finally:
        # This script refuses accounts with prior sessions. Only its own bootstrap fixtures are removed.
        runs = db.rows('SELECT * FROM runs WHERE user_id=?', (uid,))
        if any(r['status'] in {'running', 'resuming', 'approval'} for r in runs):
            raise RuntimeError('Test run is active; preserve state for diagnosis rather than deleting it')
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME / 'checkpoints.sqlite')) as saver:
            for run in runs:
                await saver.adelete_thread(f"{uid}:{run['session_id']}:{run['id']}")
        with db.connect() as conn:
            for run in runs:
                conn.execute('DELETE FROM events WHERE run_id=?', (run['id'],))
                conn.execute('DELETE FROM run_usage WHERE run_id=?', (run['id'],))
                conn.execute('DELETE FROM artifacts WHERE run_id=?', (run['id'],))
            conn.execute('DELETE FROM facts WHERE user_id=?', (uid,))
            conn.execute('DELETE FROM runs WHERE user_id=?', (uid,))
            conn.execute('DELETE FROM context_exclusions WHERE message_id IN (SELECT m.id FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.user_id=?)', (uid,))
            conn.execute('DELETE FROM message_facts WHERE message_id IN (SELECT m.id FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.user_id=?)', (uid,))
            conn.execute('DELETE FROM summary_facts WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)', (uid,))
            conn.execute('DELETE FROM summary_snapshots WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)', (uid,))
            conn.execute('DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)', (uid,))
            conn.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
            conn.execute('DELETE FROM logins WHERE user_id=?', (uid,))
            conn.execute('UPDATE users SET password=?,must_change=?,memory_version=? WHERE id=?',
                         (original['password'], original['must_change'], original['memory_version'], uid))
        config.RUNTIME.joinpath('ui-acceptance.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
