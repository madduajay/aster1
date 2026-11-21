import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.touch(mode=0o600,exist_ok=True);self.path.chmod(0o600)
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,role TEXT,envelope TEXT,result TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS queries(id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,domain TEXT,criteria TEXT,result TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,user_id TEXT,request_id TEXT,domain TEXT,parent TEXT,expires REAL,state TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,query_id TEXT,payload TEXT,state TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS monitors(id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,envelope TEXT,expires REAL,state TEXT,last_check REAL,last_hash TEXT,last_result TEXT,schedule_id TEXT);
            CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY,user_id TEXT,session_id TEXT,query_id TEXT,payload TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS delivery(id TEXT PRIMARY KEY,state TEXT,payload TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS venue_metadata(id TEXT PRIMARY KEY,value TEXT,updated REAL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns={row[1] for row in db.execute('PRAGMA table_info(monitors)')}
            for column in ('anchor','next_check'):
                if column not in columns:db.execute('ALTER TABLE monitors ADD COLUMN '+column+' REAL')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def rows(self,sql,args=()):
        with self.connect() as db:return [dict(r) for r in db.execute(sql,args)]
    def one(self,sql,args=()):
        rows=self.rows(sql,args);return rows[0] if rows else None
    def execute(self,sql,args=()):
        with self.connect() as db:return db.execute(sql,args)

    def save_query(self,qid,task,domain,c,result):
        from .contracts import identity
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT user_id,session_id,criteria,domain,result FROM queries WHERE id=?',(qid,)).fetchone()
            if old and (old['user_id']!=task.user_id or old['session_id']!=task.session_id or old['domain']!=domain or identity(domain,json.loads(old['criteria']))!=identity(domain,c)):raise ValueError('Query correlation mismatch')
            if old and json.loads(old['result']).get('revision',0)>result.get('revision',0):return
            db.execute('INSERT INTO queries VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result,updated=excluded.updated',
                (qid,task.user_id,task.session_id,domain,json.dumps(c),json.dumps(result),time.time()))

    def accept_event(self,event):
        """Persist before acknowledgement; ignore duplicate or cancelled events."""
        qid=event.get('query_id');sub=self.one('SELECT * FROM subscriptions WHERE id=?',(qid,))
        query=self.one('SELECT * FROM queries WHERE id=?',(qid,))
        if not sub or not query:raise ValueError('Unknown subscription')
        from .contracts import digest,identity
        expected=identity(query['domain'],json.loads(query['criteria']))
        if event.get('domain')!=query['domain'] or event.get('criteria_hash')!=digest(expected) or identity(query['domain'],event.get('criteria',{}))!=expected:raise ValueError('Event criteria mismatch')
        if event.get('event_id')!='opening:'+qid:raise ValueError('Event ID mismatch')
        if event.get('available') is not True or not event.get('options') or event.get('revision')!=1:raise ValueError('Invalid opening event')
        if sub['state']=='cancelled' or sub['expires']<time.time():return False
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?,\'pending\',?)',(event['event_id'],qid,json.dumps(event),time.time()))
        return True
