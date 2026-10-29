"""Secret-free browser diagnostics shared by the app and isolated worker."""
from contextlib import contextmanager
import json
import re
import traceback
from urllib.parse import urlsplit


def clean_message(value,private=()):
    text=str(value)
    for item in sorted((str(s) for s in private if s),key=len,reverse=True):
        text=text.replace(item,'[private]')
    def safe_url(match):
        try:
            p=urlsplit(match[0]);return '[URL '+(p.hostname or '')+p.path+']'
        except ValueError:return '[URL]'
    text=re.sub(r'https?://[^\s<>\"\']+',safe_url,text)
    text=re.sub(r'sk-[\w-]+|Bearer\s+\S+','[private]',text,flags=re.I)
    text=re.sub(r'''(?im)\b(?:cookie|set-cookie|authorization)\b["']?\s*:\s*[^\r\n]+''','[private header]',text)
    text=re.sub(r'''(?i)\b(?:password|passwd|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|session(?:[_-]?(?:id|token))?|authorization|cookie|otp|verification[_ -]?code)\b["']?\s*[:=]\s*[^\r\n,;]+''','[private field]',text)
    text=re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b','[email]',text)
    text=re.sub(r'(?<!\w)\+?(?:\d[ ().-]?){3,}\d(?!\w)','[number]',text)
    return text.splitlines()[0][:400] if text.splitlines() else ''


@contextmanager
def diagnostic_stage(name):
    """Preserve the original exception type and attach a precise observation stage."""
    try:yield
    except Exception as exc:
        if not hasattr(exc,'browser_stage'):
            exc.browser_stage=name
        raise


def safe_target(params,state):
    private=[params.get('text'),(state.get('node') or {}).get('value')]
    target={k:params[k] for k in ('index','coordinate_x','coordinate_y') if k in params}
    for k in ('selector','target_selector'):
        if k in params:
            selector=re.sub(r'(?i)\[value\s*=[^\]]+\]','[value=[private]]',str(params[k]))
            target[k]=clean_message(selector,private)[:200]
    node=state.get('node') or {}
    if node:
        target['tag']=node.get('tag')
        target['label']='[private field]' if node.get('sensitive') else clean_message(node.get('name',''),private)[:160]
        for k in ('visible','disabled'):
            if k in node:target[k]=node[k]
    return target


def exception_details(exc,*,operation,stage,params=None,state=None,dispatched=False):
    params,state=params or {},state or {}
    private=[params.get('text'),(state.get('node') or {}).get('value')]
    frames=traceback.extract_tb(exc.__traceback__)[-7:]
    result={'operation':operation,'stage':stage,'observation_stage':getattr(exc,'browser_stage',None),
            'error_class':type(exc).__name__,'message':clean_message(exc,private),
            'dispatched':dispatched,'target':safe_target(params,state),
            'trace':[{'file':f.filename.replace('\\','/').split('/')[-1],'line':f.lineno,'function':f.name} for f in frames]}
    cause=exc.__cause__ or exc.__context__
    if cause and cause is not exc:
        result['cause']={'error_class':type(cause).__name__,'message':clean_message(cause,private)}
    return result


def diagnostic_text(item):
    stage=item.get('stage','unknown')
    if item.get('observation_stage'):stage+=' / '+item['observation_stage']
    lines=[f"{item.get('operation','browser')} — {item.get('error_class','BrowserError')} at {stage}"]
    if item.get('message'):lines.append(item['message'])
    trace=item.get('trace',[])
    if trace:
        origin=trace[-1];lines.append(f"Source: {origin['file']}:{origin['line']} in {origin['function']}")
    if item.get('cause'):
        cause=item['cause'];lines.append('Caused by: '+cause['error_class']+': '+cause['message'])
    target=item.get('target') or {}
    if target:lines.append('Target: '+', '.join(f'{k}={v}' for k,v in target.items()))
    dispatched=item.get('dispatched')
    lines.append('Action dispatched: '+('unknown; inspect the current page' if dispatched is None else 'yes; inspect its outcome before retrying' if dispatched else 'no'))
    return '\n'.join(lines)


def load_diagnostics(db,user,*,run=None,task=None,session=None,limit=4):
    if not db.one("SELECT name FROM sqlite_master WHERE name='browser_use_diagnostics'"):return []
    where=['t.user_id=?'];args=[user]
    for field,value in [('t.run_id',run),('t.id',task),('r.session_id',session)]:
        if value is not None:where.append(field+'=?');args.append(value)
    rows=db.rows('SELECT d.details FROM browser_use_diagnostics d JOIN browser_use_tasks t ON t.id=d.task_id JOIN runs r ON r.id=t.run_id WHERE '+' AND '.join(where)+' ORDER BY d.id DESC LIMIT ?',(*args,max(1,min(limit,10))))
    return [json.loads(row['details']) for row in reversed(rows)]


def debug_text(items):
    return 'Browser debug details:\n\n'+'\n\n'.join(diagnostic_text(item) for item in items)
