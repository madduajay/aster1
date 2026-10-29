import json
import math
import re
import secrets
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

from argon2 import PasswordHasher

from . import config

PASSWORDS = PasswordHasher()
STOP_WORDS = set('a an and are as at be been but by can could do does for from have how i in is it me my of on or other our please that the their them there these they this to us user was we were what when where which who why will with would you your about asked request requested remember memory'.split())


class Database:
    def __init__(self, path=None):
        config.prepare()
        self.path = Path(path or config.RUNTIME / "application.sqlite")
        # Preserve a consistent pre-upgrade copy before touching an existing database.
        if self.path.exists():
            with sqlite3.connect(self.path) as source:
                columns = {r[1] for r in source.execute('PRAGMA table_info(facts)')}
                if columns and 'memory_key' not in columns:
                    backup = self.path.with_name(self.path.stem + '.before-memory-v2.sqlite')
                    if not backup.exists():
                        with sqlite3.connect(backup) as target:
                            source.backup(target)
                        backup.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                must_change INTEGER NOT NULL DEFAULT 1, memory_version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS logins (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (bucket TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS attempts_bucket ON attempts(bucket,at);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', covered INTEGER NOT NULL DEFAULT 0,
                context_floor INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,id);
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id),
                text TEXT NOT NULL, normalized TEXT NOT NULL, source_message INTEGER NOT NULL REFERENCES messages(id),
                importance INTEGER NOT NULL, created REAL NOT NULL, UNIQUE(user_id,normalized));
            CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text,content='facts',content_rowid='id');
            CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
                INSERT INTO facts_fts(rowid,text) VALUES(new.id,new.text); END;
            CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
                INSERT INTO facts_fts(facts_fts,rowid,text) VALUES('delete',old.id,old.text); END;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                session_id TEXT NOT NULL REFERENCES sessions(id), status TEXT NOT NULL,
                approval TEXT, approval_id TEXT, created REAL NOT NULL, error TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(user_id)
                WHERE status IN ('running','approval','resuming','input');
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id),
                path TEXT NOT NULL, filename TEXT NOT NULL, media TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS summary_snapshots (
                session_id TEXT NOT NULL REFERENCES sessions(id), covered INTEGER NOT NULL,
                summary TEXT NOT NULL, PRIMARY KEY(session_id,covered));
            CREATE TABLE IF NOT EXISTS context_exclusions (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id), reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS message_facts (
                message_id INTEGER NOT NULL REFERENCES messages(id),
                fact_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                PRIMARY KEY(message_id,fact_id));
            CREATE TABLE IF NOT EXISTS summary_facts (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                fact_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
                PRIMARY KEY(session_id,fact_id));
            CREATE INDEX IF NOT EXISTS message_facts_fact ON message_facts(fact_id);
            CREATE INDEX IF NOT EXISTS summary_facts_fact ON summary_facts(fact_id);
            """)
            db.execute('BEGIN IMMEDIATE')
            db.execute('DROP INDEX IF EXISTS one_active_run')
            db.execute("CREATE UNIQUE INDEX one_active_run ON runs(user_id) WHERE status IN ('running','approval','resuming','input')")
            if 'memory_opt_in_after' not in {r[1] for r in db.execute('PRAGMA table_info(users)')}:
                db.execute('ALTER TABLE users ADD COLUMN memory_opt_in_after INTEGER NOT NULL DEFAULT 0')
            columns = {r[1] for r in db.execute('PRAGMA table_info(facts)')}
            if 'memory_key' not in columns:
                for column in ["memory_key TEXT NOT NULL DEFAULT ''", "updated REAL NOT NULL DEFAULT 0",
                               "status TEXT NOT NULL DEFAULT 'quarantined'", "origin TEXT NOT NULL DEFAULT 'legacy'",
                               "evidence TEXT NOT NULL DEFAULT ''", "supersedes INTEGER"]:
                    db.execute('ALTER TABLE facts ADD COLUMN ' + column)
                db.execute("UPDATE facts SET memory_key='legacy.'||id,updated=created")
                # Old automatic promotions and compressed assumptions require review.
                db.execute("UPDATE sessions SET summary='',covered=context_floor")
                db.execute('UPDATE users SET memory_version=memory_version+1')
                db.execute('''UPDATE users SET memory_opt_in_after=COALESCE((SELECT MAX(m.id) FROM messages m
                    JOIN sessions s ON s.id=m.session_id WHERE s.user_id=users.id),0)''')
                # An old checkpoint can contain withdrawn facts. Never resume it.
                db.execute("UPDATE runs SET status='interrupted',approval=NULL,approval_id=NULL,error='Memory policy upgraded; inspect before retrying' WHERE status='approval'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_fact_key ON facts(user_id,memory_key) WHERE status='active'")
            db.execute("""CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF text ON facts BEGIN
                INSERT INTO facts_fts(facts_fts,rowid,text) VALUES('delete',old.id,old.text);
                INSERT INTO facts_fts(rowid,text) VALUES(new.id,new.text); END""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).lastrowid

    def seed(self):
        credentials = []
        with self.connect() as db:
            for n in range(1, 11):
                username = f"user{n:02}"
                if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                    continue
                password = secrets.token_urlsafe(18)
                db.execute("INSERT INTO users(id,username,password) VALUES(?,?,?)",
                           (secrets.token_hex(16), username, PASSWORDS.hash(password)))
                credentials.append((username, password))
        if credentials:
            path = config.RUNTIME / "initial-accounts.txt"
            with path.open("a") as output:
                for name, password in credentials:
                    output.write(f"{name}\t{password}\n")
            path.chmod(0o600)
        return len(credentials)

    def owned_session(self, user, session):
        row = self.one("SELECT * FROM sessions WHERE id=? AND user_id=?", (session, user))
        if not row:
            raise LookupError("Conversation not found")
        return row

    def new_session(self, user, title="New chat"):
        sid = secrets.token_hex(16)
        self.execute("INSERT INTO sessions(id,user_id,title,created) VALUES(?,?,?,?)",
                     (sid, user, title[:80], time.time()))
        return sid

    def message(self, user, session, role, content):
        self.owned_session(user, session)
        return self.execute("INSERT INTO messages(session_id,role,content,created) VALUES(?,?,?,?)",
                            (session, role, content, time.time()))

    def event(self, run, kind, value):
        return self.execute("INSERT INTO events(run_id,kind,payload) VALUES(?,?,?)",
                            (run, kind, json.dumps(value)))

    def facts(self, user, query="", *, inspect=False):
        terms = list(dict.fromkeys(t for t in re.findall(r"\w{2,}", query.lower()) if t not in STOP_WORDS))[:20]
        if inspect:
            return self.rows("SELECT * FROM facts WHERE user_id=? AND status='active' ORDER BY updated DESC,id DESC", (user,))
        found = []
        if terms:
            match = " OR ".join('"' + t + '"' for t in terms)
            found = self.rows("""SELECT f.* FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid
                WHERE facts_fts MATCH ? AND f.user_id=? AND f.status='active' ORDER BY rank LIMIT 100""", (match, user))
        # Always consult the permanent store, including a bounded set of defaults.
        defaults = self.rows("SELECT * FROM facts WHERE user_id=? AND status='active' ORDER BY updated DESC,id DESC LIMIT 8", (user,))
        candidates = {r['id']: r for r in found + defaults}
        now = time.time()
        def score(row):
            words = set(re.findall(r'\w{2,}', row['text'].lower()))
            relevance = len(words.intersection(terms)) / max(len(terms), 1)
            freshness = math.exp(-max(0, now - row['updated']) / (90 * 86400))
            return (4 * relevance + freshness + 0.1 * row['importance'], row['updated'], row['id'])
        return sorted(candidates.values(), key=score, reverse=True)[:12]

    def visible_messages(self, user, session):
        row = self.owned_session(user, session)
        return self.rows("""SELECT id,role,content,created FROM messages WHERE session_id=? AND id>?
            AND id NOT IN (SELECT message_id FROM context_exclusions) ORDER BY id""", (session, row['context_floor']))

    def dependencies(self, user, session):
        self.owned_session(user, session)
        return [r['fact_id'] for r in self.rows("""SELECT fact_id FROM summary_facts WHERE session_id=?
            UNION SELECT mf.fact_id FROM message_facts mf JOIN messages m ON m.id=mf.message_id
            WHERE m.session_id=? AND m.id NOT IN (SELECT message_id FROM context_exclusions)""", (session, session))]

    def record_dependencies(self, user, session, message, facts):
        self.owned_session(user, session)
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM messages WHERE id=? AND session_id=?', (message, session)).fetchone():
                raise LookupError('Message not found')
            for fid in set(facts):
                if db.execute("SELECT 1 FROM facts WHERE id=? AND user_id=?", (fid, user)).fetchone():
                    db.execute('INSERT OR IGNORE INTO message_facts VALUES(?,?)', (message, fid))

    def _invalidate(self, db, user, facts):
        """Remove only dependent context; retain unrelated messages and sessions."""
        affected = set()
        for fact in facts:
            ids = [fact['source_message']]
            ids += [r[0] for r in db.execute('SELECT message_id FROM message_facts WHERE fact_id=?', (fact['id'],))]
            for mid in ids:
                message = db.execute('SELECT session_id FROM messages WHERE id=?', (mid,)).fetchone()
                if not message:
                    continue
                sid = message[0]
                affected.add(sid)
                db.execute('INSERT OR IGNORE INTO context_exclusions VALUES(?,?)', (mid, 'Memory withdrawn or superseded'))
                # The response to an explicit remember request may repeat that fact.
                reply = db.execute("SELECT id,role FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT 1", (sid, mid)).fetchone()
                if reply and reply['role'] == 'assistant':
                    db.execute('INSERT OR IGNORE INTO context_exclusions VALUES(?,?)', (reply['id'], 'Memory withdrawn or superseded'))
            affected.update(r[0] for r in db.execute('SELECT session_id FROM summary_facts WHERE fact_id=?', (fact['id'],)))
        for sid in affected:
            db.execute("UPDATE sessions SET summary='',covered=context_floor WHERE id=? AND user_id=?", (sid, user))
            db.execute('DELETE FROM summary_snapshots WHERE session_id=?', (sid,))
            db.execute('DELETE FROM summary_facts WHERE session_id=?', (sid,))
        db.execute('UPDATE users SET memory_version=memory_version+1 WHERE id=?', (user,))
        db.execute("UPDATE runs SET status='interrupted',approval=NULL,approval_id=NULL,error='Memory changed; inspect before retrying' WHERE user_id=? AND status='approval'", (user,))
        return bool(affected)

    def put_fact(self, user, text, source, key, *, importance=5, evidence='', conn=None):
        if conn is None:
            with self.connect() as db:
                return self.put_fact(user, text, source, key, importance=importance, evidence=evidence, conn=db)
        source_row = conn.execute("""SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE m.id=? AND s.user_id=? AND m.role='user'""", (source, user)).fetchone()
        if not source_row:
            raise LookupError('Memory source not found')
        previous = conn.execute("SELECT * FROM facts WHERE user_id=? AND memory_key=? AND status='active'", (user, key)).fetchone()
        normalized = text.strip().casefold()
        if previous and (source_row['created'] < previous['updated'] or previous['source_message'] == source):
            return previous['id'], False
        if previous and previous['normalized'] == normalized:
            conn.execute('UPDATE facts SET updated=?,source_message=?,evidence=? WHERE id=?', (source_row['created'], source, evidence, previous['id']))
            conn.execute('INSERT OR IGNORE INTO message_facts VALUES(?,?)', (source, previous['id']))
            return previous['id'], False
        invalidated = False
        if previous:
            invalidated = self._invalidate(conn, user, [previous])
            conn.execute("UPDATE facts SET status='superseded' WHERE id=?", (previous['id'],))
        existing = conn.execute('SELECT id FROM facts WHERE user_id=? AND normalized=?', (user, normalized)).fetchone()
        if existing:
            fid = existing[0]
            conn.execute("""UPDATE facts SET memory_key=?,source_message=?,importance=?,updated=?,status='active',
                origin='explicit',evidence=?,supersedes=? WHERE id=?""",
                (key, source, importance, source_row['created'], evidence, previous['id'] if previous else None, fid))
        else:
            fid = conn.execute("""INSERT INTO facts(user_id,text,normalized,source_message,importance,created,
                memory_key,updated,status,origin,evidence,supersedes) VALUES(?,?,?,?,?,?,?,?, 'active','explicit',?,?)""",
                (user, text.strip(), normalized, source, importance, time.time(), key, source_row['created'], evidence,
                 previous['id'] if previous else None)).lastrowid
        conn.execute('INSERT OR IGNORE INTO message_facts VALUES(?,?)', (source, fid))
        return fid, invalidated

    def forget(self, user, fact_id):
        with self.connect() as db:
            if fact_id == "all":
                facts = db.execute('SELECT * FROM facts WHERE user_id=?', (user,)).fetchall()
            else:
                row = db.execute("SELECT * FROM facts WHERE user_id=? AND id=?", (user, fact_id)).fetchone()
                if not row:
                    raise LookupError("Memory not found")
                facts = db.execute('SELECT * FROM facts WHERE user_id=? AND memory_key=?', (user, row['memory_key'])).fetchall()
            self._invalidate(db, user, facts)
            for fact in facts:
                db.execute('DELETE FROM facts WHERE id=?', (fact['id'],))
