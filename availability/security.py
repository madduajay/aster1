"""Internal service authentication and per-company webhook signatures."""
import hashlib
import hmac
import json
import os
import secrets
import time
import tempfile
from pathlib import Path


class Security:
    def __init__(self,runtime):
        path=Path(runtime)/'availability-secrets.json';path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not path.exists():
            # Publish a fully written file atomically; simultaneous service
            # startups must never read an empty or partial secrets file.
            with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as f:
                temporary=f.name
                json.dump({key:secrets.token_urlsafe(40) for key in ('service','dining','hotels','flights')},f)
                f.flush();os.fsync(f.fileno())
            try:
                try:os.link(temporary,path)
                except FileExistsError:pass
            finally:os.unlink(temporary)
        self.keys=json.loads(path.read_text())

    def headers(self,user=''):return {'Authorization':'Bearer '+self.keys['service'],'X-Aster-User':user}

    def authorize(self,headers,user=None):
        expected='Bearer '+self.keys['service']
        return hmac.compare_digest(headers.get('authorization',''),expected) and (user is None or headers.get('x-aster-user')==user)

    def sign(self,domain,body,now=None):
        timestamp=str(int(time.time() if now is None else now))
        signature=hmac.new(self.keys[domain].encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
        return {'X-Company-Time':timestamp,'X-Company-Signature':signature,'Content-Type':'application/json'}

    def verify(self,domain,body,headers,now=None):
        timestamp=headers.get('x-company-time','')
        try:
            if abs((time.time() if now is None else now)-int(timestamp))>300:return False
        except ValueError:return False
        expected=hmac.new(self.keys[domain].encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
        return hmac.compare_digest(headers.get('x-company-signature',''),expected)
