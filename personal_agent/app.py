import asyncio
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from argon2.exceptions import VerificationError
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config
from .agent import Engine
from .browser_host import BrowserHost
from .db import Database, PASSWORDS
from .service import Service
from .approvals import approval_text, display_message
from .browser_recovery import input_text
from .browser_diagnostics import load_diagnostics


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class Turn(BaseModel):
    pending_input_id: str | None = Field(default=None, max_length=64)
    session_id: str
    text: str = Field(min_length=1, max_length=16000)


class Approval(BaseModel):
    approval_id: str
    decision: str = Field(pattern='^(approve|reject)$')


class InputReply(BaseModel):
    input_id: str
    text: str = Field(min_length=1,max_length=2000)
    completed_in_browser: bool = False


class ManualAction(BaseModel):
    input_id: str
    operation: str = Field(pattern='^(open|click|type|key|scroll)$')
    x: float | None = None
    y: float | None = None
    text: str | None = Field(default=None,max_length=2000)
    key: str | None = Field(default=None,max_length=32)


class MemoryCorrection(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    session_id: str


def create_app(database=None, service=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            app.state.db, app.state.service = database, service
            yield
            return
        db = Database()
        db.seed()
        browsers = BrowserHost(db)
        # Avoid provider keys in the environment, arguments, frontend, or logs.
        key = config.keychain_key()
        async with AsyncSqliteSaver.from_conn_string(str(config.RUNTIME / 'checkpoints.sqlite')) as saver:
            engine = Engine(db, browsers, saver, key)
            engine.browser_tasks.recover()
            del key
            app.state.db, app.state.service = db, Service(db, engine)
            app.state.service.recover()
            try:
                yield
            finally:
                await app.state.service.close()
                await engine.browser_tasks.close()
                await browsers.close()
                await engine.client.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    origins = config.allowed_origins()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', *{urlsplit(o).hostname for o in origins}])
    if database is not None:
        app.state.db, app.state.service = database, service

    @app.middleware('http')
    async def security(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if origin not in origins or urlsplit(origin).netloc != request.headers.get('host'):
                return Response('Origin rejected', status_code=403)
            length = request.headers.get('content-length', '0')
            maximum = 50_000_000 if request.url.path == '/api/uploads' else 70000
            if not length.isdigit() or int(length) > maximum:
                return Response('Request too large or missing length', status_code=413)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        sockets=' '.join(origin.replace('https://','wss://') for origin in origins)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self' "+sockets+"; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        return response

    def identity(request: Request):
        raw = request.cookies.get('pa_session', '')
        row = request.app.state.db.one("""SELECT u.*,l.token FROM logins l JOIN users u ON u.id=l.user_id
            WHERE l.token=? AND l.expires>?""", (token_hash(raw), time.time()))
        if not row:
            raise HTTPException(401, 'Please log in')
        return row

    def user(row=Depends(identity)):
        if row['must_change']:
            raise HTTPException(403, 'Change your temporary password first')
        return row['id']

    def db(request):
        return request.app.state.db

    @app.exception_handler(RequestValidationError)
    async def bad_request(request,exc):
        # Do not echo submitted passwords or private takeover values in validation errors.
        return Response('Please check the form fields.',status_code=422)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return Response('Not found', status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return Response(str(exc), status_code=409)

    @app.get('/api/health')
    def health():
        return {'ok': True, 'application': 'aster1-personal-agent'}

    @app.post('/api/login')
    async def login(body: Login, request: Request, response: Response):
        database = db(request)
        username = body.username.strip().lower()
        bucket = token_hash(username)
        now = time.time()
        with database.connect() as conn:
            conn.execute("DELETE FROM attempts WHERE at<?", (now - 900,))
            total = conn.execute("SELECT COUNT(*) FROM attempts WHERE at>?", (now - 60,)).fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM attempts WHERE bucket=? AND at>?", (bucket, now - 300)).fetchone()[0]
            if total >= 30 or count >= 8:
                raise HTTPException(429, 'Too many attempts. Try again later.')
            conn.execute("INSERT INTO attempts VALUES(?,?)", (bucket, now))
        row = database.one("SELECT * FROM users WHERE username=?", (username,))
        try:
            encoded = row['password'] if row else PASSWORDS.hash(secrets.token_urlsafe(20))
            valid = await asyncio.to_thread(PASSWORDS.verify, encoded, body.password)
        except VerificationError:
            valid = False
        if not valid or not row:
            raise HTTPException(401, 'Username or password is incorrect')
        raw = secrets.token_urlsafe(32)
        database.execute("INSERT INTO logins VALUES(?,?,?)", (token_hash(raw), row['id'], now + 12 * 3600))
        response.set_cookie('pa_session', raw, secure=True, httponly=True, samesite='strict', max_age=12 * 3600, path='/')
        return {'username': username, 'must_change': bool(row['must_change'])}

    @app.get('/api/me')
    def me(row=Depends(identity)):
        return {'username': row['username'], 'must_change': bool(row['must_change'])}

    @app.post('/api/password')
    async def password(body: PasswordChange, request: Request, response: Response, row=Depends(identity)):
        try:
            await asyncio.to_thread(PASSWORDS.verify, row['password'], body.current_password)
        except VerificationError:
            raise HTTPException(401, 'Current password is incorrect') from None
        if body.current_password == body.new_password:
            raise HTTPException(400, 'Choose a different password')
        encoded = await asyncio.to_thread(PASSWORDS.hash, body.new_password)
        raw = secrets.token_urlsafe(32)
        with db(request).connect() as conn:
            conn.execute("UPDATE users SET password=?,must_change=0 WHERE id=?", (encoded, row['id']))
            conn.execute("DELETE FROM logins WHERE user_id=?", (row['id'],))
            conn.execute("INSERT INTO logins VALUES(?,?,?)", (token_hash(raw), row['id'], time.time() + 12 * 3600))
        response.set_cookie('pa_session', raw, secure=True, httponly=True, samesite='strict', max_age=12 * 3600, path='/')
        return {'ok': True}

    @app.post('/api/logout')
    async def logout(request: Request, response: Response, row=Depends(identity)):
        active = request.app.state.service.active(row['id'])
        if active:
            await request.app.state.service.cancel(row['id'], active['id'])
        await request.app.state.service.engine.browsers.close_user(row['id'])
        db(request).execute("DELETE FROM logins WHERE token=?", (row['token'],))
        response.delete_cookie('pa_session', secure=True, httponly=True, samesite='strict', path='/')
        return {'ok': True}

    @app.get('/api/sessions')
    def sessions(request: Request, uid=Depends(user)):
        return db(request).rows("SELECT id,title,created FROM sessions WHERE user_id=? ORDER BY created DESC LIMIT 100", (uid,))

    @app.post('/api/sessions')
    async def new_session(request: Request, uid=Depends(user)):
        return {'id': await request.app.state.service.new_session(uid)}

    @app.get('/api/sessions/{sid}')
    def session(sid: str, request: Request, uid=Depends(user)):
        row = db(request).owned_session(uid, sid)
        messages=db(request).rows("SELECT id,role,content FROM messages WHERE session_id=? ORDER BY id", (sid,))
        active = db(request).one("SELECT id,status,approval,approval_id FROM runs WHERE user_id=? AND session_id=? AND status IN ('running','resuming','approval','input')", (uid, sid))
        if active:
            active['after'] = db(request).one("SELECT COALESCE(MAX(id),0) AS cursor FROM events WHERE run_id=? AND kind IN ('approval','input')", (active['id'],))['cursor']
            if active['status']=='input' and active['approval']:
                pending=json.loads(active['approval'])
                active['input']=pending['action']
                active['input_text']=input_text(pending['action'])
                event=db(request).one("SELECT payload FROM events WHERE run_id=? AND kind='input' ORDER BY id DESC LIMIT 1",(active['id'],))
                active['input_message_id']=json.loads(event['payload']).get('message_id') if event else None
            if active['status']=='approval' and active['approval']:
                active['approval_text']=approval_text(json.loads(active['approval']))
                event=db(request).one("SELECT payload FROM events WHERE run_id=? AND kind IN ('approval','input') ORDER BY id DESC LIMIT 1",(active['id'],))
                active['approval_message_id']=json.loads(event['payload']).get('message_id') if event else None
                if not active['approval_message_id']:
                    previous=next((m for m in reversed(messages) if m['role']=='assistant' and m['content'].startswith(('Approval required:\n','Approval needed\n'))),None)
                    if previous:active['approval_message_id']=previous['id']
        for item in messages:item['content']=display_message(item['role'],item['content'])
        browser_run=None
        if db(request).one("SELECT name FROM sqlite_master WHERE name='browser_use_tasks'"):
            browser_run=db(request).one('SELECT t.run_id FROM browser_use_tasks t JOIN runs r ON r.id=t.run_id WHERE t.user_id=? AND r.session_id=? ORDER BY t.created DESC LIMIT 1',(uid,sid))
        return {'session': row, 'messages': messages,
                'active_run': active,'browser_run':browser_run['run_id'] if browser_run else None}

    @app.post('/api/runs')
    async def run(body: Turn, request: Request, uid=Depends(user)):
        return await request.app.state.service.submit(uid, body.session_id, body.text.strip(), pending_input_id=body.pending_input_id)

    @app.get('/api/runs/{rid}')
    def run_status(rid: str, request: Request, uid=Depends(user)):
        row = db(request).one("SELECT id,session_id,status,approval,approval_id,error FROM runs WHERE id=? AND user_id=?", (rid, uid))
        if not row:
            raise HTTPException(404)
        return row

    @app.post('/api/runs/{rid}/cancel')
    async def cancel(rid: str, request: Request, uid=Depends(user)):
        await request.app.state.service.cancel(uid, rid)
        return {'ok': True}

    @app.get('/api/runs/{rid}/diagnostics')
    def browser_diagnostics(rid: str,request: Request,uid=Depends(user)):
        if not db(request).one('SELECT id FROM runs WHERE id=? AND user_id=?',(rid,uid)):raise HTTPException(404)
        return {'diagnostics':load_diagnostics(db(request),uid,run=rid,limit=10)}

    @app.get('/api/runs/{rid}/browser')
    def browser_view(rid: str,request: Request,uid=Depends(user)):
        if not db(request).one('SELECT id FROM runs WHERE id=? AND user_id=?',(rid,uid)):raise HTTPException(404)
        host=request.app.state.service.engine.browsers
        return host.view_info(uid,rid) if hasattr(host,'view_info') else {'available':False}

    @app.get('/api/runs/{rid}/browser/frame')
    async def browser_frame(rid: str,request: Request,uid=Depends(user)):
        if not db(request).one('SELECT id FROM runs WHERE id=? AND user_id=?',(rid,uid)):raise HTTPException(404)
        host=request.app.state.service.engine.browsers
        frame=await host.preview(uid,rid) if hasattr(host,'preview') else None
        if not frame:return Response(status_code=204)
        return Response(frame,media_type='image/jpeg')

    @app.websocket('/api/runs/{rid}/browser/control')
    async def browser_control(socket: WebSocket,rid: str,input_id: str='',width: int=1280,height: int=850):
        origin=socket.headers.get('origin','')
        if origin not in origins or urlsplit(origin).netloc!=socket.headers.get('host'):
            await socket.close(code=4403);return
        try:
            row=identity(socket)
            if row['must_change']:raise HTTPException(403)
            uid=row['id']
            if not db(socket).one('SELECT id FROM runs WHERE id=? AND user_id=?',(rid,uid)):raise LookupError()
            if not input_id or len(input_id)>128:raise LookupError()
            host=socket.app.state.service.engine.browsers
            host.pending_manual(uid,rid,input_id)
            from .browser_control import ViewportSize
            viewport=ViewportSize(width=width,height=height).model_dump()
        except (HTTPException,LookupError,ValueError):
            await socket.close(code=4403);return
        def authorized():
            try:
                current=identity(socket)
                return current['id']==uid and not current['must_change']
            except HTTPException:return False
        await socket.accept()
        from .browser_control import serve_control
        await serve_control(host,socket,uid,rid,input_id,authorized,viewport)

    @app.post('/api/runs/{rid}/input')
    async def input_reply(rid: str,body: InputReply,request: Request,uid=Depends(user)):
        if not db(request).one('SELECT id FROM runs WHERE id=? AND user_id=?',(rid,uid)):raise HTTPException(404)
        from .browser_dom import InspectionUnavailable
        try:return await request.app.state.service.respond_input(uid,rid,body.input_id,body.text,completed_in_browser=body.completed_in_browser)
        except InspectionUnavailable as exc:raise ValueError(str(exc)) from None

    @app.post('/api/runs/{rid}/takeover')
    async def manual_action(rid: str,body: ManualAction,request: Request,uid=Depends(user)):
        if not db(request).one('SELECT id FROM runs WHERE id=? AND user_id=?',(rid,uid)):raise HTTPException(404)
        host=request.app.state.service.engine.browsers
        if body.operation=='open':return await host.open_takeover(uid,rid,body.input_id)
        from playwright.async_api import Error as BrowserError
        try:return await host.manual_action(uid,rid,body.input_id,body.operation,body.x,body.y,body.text,body.key)
        except BrowserError:raise ValueError('The website changed or disconnected. Inspect the browser before continuing.') from None

    @app.post('/api/runs/{rid}/approval')
    async def approve(rid: str, body: Approval, request: Request, uid=Depends(user)):
        row = db(request).one("SELECT session_id FROM runs WHERE id=? AND user_id=?", (rid, uid))
        if not row:
            raise HTTPException(404)
        return await request.app.state.service.resume(uid,rid,body.decision,body.approval_id)

    @app.get('/api/runs/{rid}/events')
    async def events(rid: str, request: Request, after: int = 0, uid=Depends(user)):
        row = db(request).one("SELECT * FROM runs WHERE id=? AND user_id=?", (rid, uid))
        if not row:
            raise HTTPException(404)
        cursor = max(after, int(request.headers.get('last-event-id', '0')))

        async def stream():
            nonlocal cursor
            while True:
                if await request.is_disconnected():
                    return
                login = db(request).one("SELECT token FROM logins WHERE token=? AND expires>?",
                                        (token_hash(request.cookies.get('pa_session', '')), time.time()))
                if not login:
                    return
                batch = db(request).rows("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 100", (rid, cursor))
                for event in batch:
                    cursor = event['id']
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {event['payload']}\n\n"
                state = db(request).one("SELECT status FROM runs WHERE id=?", (rid,))['status']
                if state not in {'running', 'resuming'} and len(batch) < 100:
                    return
                if not batch:
                    yield ': keepalive\n\n'
                await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.get('/api/memory')
    def memory(request: Request, uid=Depends(user)):
        return db(request).facts(uid, inspect=True)

    def memory_unlocked(request, uid):
        if request.app.state.service.active(uid):
            raise HTTPException(409, 'Stop the active run before changing memory')

    @app.delete('/api/memory/{fid}')
    def forget(fid: str, request: Request, uid=Depends(user)):
        memory_unlocked(request, uid)
        db(request).forget(uid, fid)
        return {'ok': True}

    @app.patch('/api/memory/{fid}')
    def correct(fid: str, body: MemoryCorrection, request: Request, uid=Depends(user)):
        memory_unlocked(request, uid)
        database = db(request)
        database.owned_session(uid, body.session_id)
        mid = database.message(uid, body.session_id, 'user', f'/correct {fid} {body.text}')
        return {'reply': request.app.state.service.command(uid, body.session_id, mid, f'/correct {fid} {body.text}')}

    @app.get('/api/artifacts/{aid}')
    def artifact(aid: str, request: Request, uid=Depends(user)):
        row = db(request).one("SELECT * FROM artifacts WHERE id=? AND user_id=?", (aid, uid))
        if not row or not Path(row['path']).is_file():
            raise HTTPException(404)
        return FileResponse(row['path'], media_type=row['media'], filename=row['filename'],
                            content_disposition_type='inline' if row['media'] == 'image/png' else 'attachment')

    @app.post('/api/uploads')
    async def upload(request: Request, session_id: str, uid=Depends(user)):
        database = db(request)
        database.owned_session(uid, session_id)
        if request.app.state.service.active(uid):
            raise HTTPException(409, 'Stop the active run before uploading a file')
        raw = request.headers.get('x-filename','Document')
        from urllib.parse import unquote
        filename = Path(unquote(raw)).name.replace('\\','_')[:180]
        filename = ''.join(c for c in filename if c.isprintable()) or 'Document'
        aid, run_id = secrets.token_hex(16), secrets.token_hex(16)
        path = config.RUNTIME / 'artifacts' / uid / aid
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        total = 0
        try:
            with path.open('xb') as output:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > 50_000_000:
                        raise HTTPException(413,'File exceeds the 50 MB limit')
                    output.write(chunk)
            if not total:
                raise HTTPException(400,'File is empty')
            with database.connect() as conn:
                conn.execute("INSERT INTO runs(id,user_id,session_id,status,created) VALUES(?,?,?,'completed',?)",(run_id,uid,session_id,time.time()))
                conn.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?)',(aid,uid,run_id,str(path),filename,'application/octet-stream'))
            text=f'Uploaded file: {filename} (artifact ID {aid}). This file is available for this user; sharing it with a website requires approval.'
            database.message(uid,session_id,'user',text)
            return {'id':aid,'filename':filename,'text':text}
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    app.mount('/static', StaticFiles(directory=config.ROOT / 'static'), name='static')

    @app.get('/')
    def index():
        return FileResponse(config.ROOT / 'static/index.html')

    return app


app = create_app()
