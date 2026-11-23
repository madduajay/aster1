"""Conservative, website-independent recognition of explicit page-opening requests.

This recognizes the user's requested operation only. The model still resolves
official domains and task details; there are no brand/URL mappings here.
"""
import re
import json


def preview_request(db,user,session,message,resolved_goal):
    """Keep short replies bound to the immediately preceding visible offer."""
    text=message.get('content','')
    result={'current_user_request':text,'resolved_goal':resolved_goal}
    if re.fullmatch(r'\s*(?:yes|yeah|yep|sure|ok(?:ay)?|please do|pls do|go ahead|do that|continue)(?:\s+(?:please|pls|do|go ahead|continue))*\s*[.!]*',text,re.I):
        visible=db.visible_messages(user,session)
        previous=[m for m in visible if m['id']<message['id']]
        if previous and previous[-1]['role']=='assistant':
            result['preceding_assistant_offer']=previous[-1]['content'][-8000:]
    return json.dumps(result,ensure_ascii=False)



_ACTION=re.compile(r"^(?:please\s+)?(?:(?:I\s+(?:want|need)|I['’]d\s+like|let['’]?s|new\s+(?:task|request))\b|(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?P<verb>open|visit|navigate\s+to|go\s+to|load|bring\s+up|take\s+me\s+to|show(?:\s+me)?)\s+(?P<target>.+)",re.I|re.S)
_PAGE=re.compile(r'\b(?:home\s*page|main\s*page|web\s*site|web\s*page|landing\s*page|site|application\s+(?:page|form)|login\s+page)\b|\bapplication\b(?=\s*(?:to\s+fill\s*out|$))',re.I)
_ADDRESS=re.compile(r'https?://\S+|www\.\S+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b',re.I)
_LOGIN_STEP=re.compile(r'(?:[,;]|\b(?:and|then)\b)\s*(?:(?:please|then)\s+)?(?:log\s*in|sign\s*in)\b',re.I)


def requests_page_open(text):
    text=text.strip()
    match=_ACTION.match(text)
    if not match:return False
    target=match['target']
    if re.match(r'(?:how\b|why\b|whether\b|if\b)',target,re.I):return False
    if re.search(r"\b(?:do\s+not|don't|never)\s+(?:open|visit|navigate|load|browse)\b",text,re.I):return False
    if match['verb'].lower().startswith('show') and re.search(r'\b(?:link|url|address|source\s+code|html)\b',target,re.I):return False
    return bool(_PAGE.search(target) or _ADDRESS.search(target) or _LOGIN_STEP.search(target))


def navigation_only_request(text):
    if not requests_page_open(text):return False
    # Opening a site followed by an instruction to act on it is not navigation-only.
    return not re.search(r'(?:\b(?:and|then|also)|[,;])\s+(?:(?:please|you)\s+)?(?:fill\s*out|complete|submit|apply|buy|purchase|order|book|reserve|send|add|delete|enter|type|upload|sign\s*in|log\s*in|summari[sz]e|read|check|tell|explain|compare|extract|list|find)\b',text,re.I)


def original_user_request(db,user,run):
    # Bind intent to the original user message, not model objectives or later Continue replies.
    source=db.one('''SELECT m.content FROM messages m JOIN runs r ON r.session_id=m.session_id
        JOIN sessions s ON s.id=r.session_id WHERE r.id=? AND r.user_id=? AND s.user_id=?
        AND m.role='user' AND m.id>s.context_floor AND m.created<=r.created ORDER BY m.id DESC LIMIT 1''',(run,user,user))
    return source['content'] if source else ''


def navigation_only_run(db,user,run):
    return navigation_only_request(original_user_request(db,user,run))


def related_browser_followup(text,subject):
    """Conservative display continuity; never resumes a task or authorizes actions."""
    if re.match(r'\s*(?:/|hello\b|hi\b|hey\b|thanks\b|thank\s+you\b)',text,re.I):return False
    if re.search(r'\b(?:instead|new\s+(?:topic|task|request)|forget\s+(?:it|that)|move\s+on|stop|cancel)\b',text,re.I):return False
    if re.search(r'\b(?:summari[sz]e|explain|expand|read|list|compare)\s+(?:it|this|that|these|those)\b|\b(?:on|from|about|of)\s+(?:this|that|the same)\s+(?:page|site|website)\b|\b(?:what|which)\s+(?:is|are)\s+(?:it|this|that|these|those)\s*[?.!]*$',text,re.I):return True
    if re.fullmatch(r'\s*(?:(?:please|can you)\s+)?(?:tell me more|go on|continue|explain (?:more|in more detail)|show (?:me )?(?:more|all)|summari[sz]e|expand (?:on )?(?:that|it))\s*[.!?]*',text,re.I):return True
    ignored=set('the a an and or of to for in on at from with me my all any more show tell please can could would you i what which how is are does do have has open page website site browser official read summarize summarise check find get then user task request http https www com org net'.split())
    # Keep topical words (including "benefits") while excluding generic browser
    # instructions. Plural normalization supports e.g. card/cards and fee/fees.
    def terms(value):return {word.rstrip('s') for word in re.findall(r'[a-z]{3,}',value.lower()) if word not in ignored}
    return bool(terms(text)&terms(subject))


def browser_preview_run(db,user,session):
    if not db.one("SELECT name FROM sqlite_master WHERE name='browser_use_tasks'"):return None
    source=db.one('''SELECT r.id,r.created,r.rowid AS position,r.status,t.status AS task_status,t.objective
        FROM browser_use_tasks t JOIN runs r ON r.id=t.run_id
        WHERE t.user_id=? AND r.user_id=? AND r.session_id=?
        ORDER BY r.created DESC,r.rowid DESC,t.created DESC,t.rowid DESC LIMIT 1''',(user,user,session))
    if not source:return None
    if db.one("SELECT id FROM events WHERE run_id=? AND kind='browser_context_retired' LIMIT 1",(source['id'],)):return None
    newer=db.rows('''SELECT id FROM runs WHERE user_id=? AND session_id=?
        AND (created>? OR created=? AND rowid>?) ORDER BY created,rowid LIMIT 21''',
        (user,session,source['created'],source['created'],source['position']))
    if not newer:return source['id']
    if len(newer)>20 or source['status']!='completed' or source['task_status']!='completed':return None
    subject=original_user_request(db,user,source['id'])+' '+source['objective']
    for row in newer:
        link=db.one("SELECT payload FROM events WHERE run_id=? AND kind='browser_context' ORDER BY id DESC LIMIT 1",(row['id'],))
        if link and json.loads(link['payload']).get('source_run')==source['id']:continue
        text=original_user_request(db,user,row['id'])
        if not related_browser_followup(text,subject):return None
        subject+=' '+text
    return source['id']


def native_entry_url(db,user,run,task,domains):
    """Choose a scoped public entry page before Visible tooling attaches."""
    from urllib.parse import urlsplit
    from .sources import urls,allowed_url
    from .browser_policy import URL_MUTATION
    request=original_user_request(db,user,run)
    candidates=list(urls(request))
    if re.search(r'\b(?:log\s*in|sign\s*in|balances?|statements?|transactions?|account)\b',request,re.I):
        rows=db.rows("""SELECT a.before_state FROM browser_use_actions a JOIN browser_use_tasks t ON t.id=a.task_id
            WHERE t.user_id=? AND a.operation='request_user' ORDER BY a.rowid DESC LIMIT 20""",(user,))
        for row in rows:
            try:
                state=json.loads(row['before_state'])
                if not isinstance(state,dict) or not any(isinstance(f,dict) and f.get('type')=='password' for f in (state.get('fields') or [])):continue
                p=urlsplit(state.get('url') or '')
            except (ValueError,TypeError):continue
            # Reuse only an observed, ordinary login entry path. Session/query
            # parameters must never become Chrome process arguments.
            if re.search(r'/(?:log[-_]?in|log[-_]?on|sign[-_]?in)/?$',p.path,re.I):
                candidates.append(p._replace(query='',fragment='').geturl())
    candidates.extend(urls(task))
    for candidate in candidates:
        try:p=urlsplit(candidate)
        except ValueError:continue
        if (p.scheme in {'http','https'} and not p.username and not p.password and not p.query and not p.fragment
                and allowed_url(candidate,domains) and not URL_MUTATION.search(p.path)):
            return candidate
    return None


# Short replies to an explicit information question stay bound to that question.
# Instructions/questions in the composer start a fresh run; no website names here.
_NEW_REQUEST = re.compile(r"^(?:/|(?:please\s+)?(?:(?:I\s+(?:want|need)|I['’]d\s+like|let['’]?s|new\s+(?:task|request))\b|(?:can|could|would|will)\s+you\b|(?:what|why|how|where|when|who|which)\b|(?:open|visit|navigate|go|load|show|find|search|look|book|reserve|buy|add|remove|delete|cancel|stop|start|switch|tell|help|compare|check|explain|summarize|write|do|don't|instead|actually|forget)\b))", re.I)


def starts_new_request(text, kind='information'):
    text = text.strip()
    if re.match(r'^(?:hello|hi|hey|thanks|thank\s+you|good\s+(?:morning|afternoon|evening))(?=[\s!,.?]|$)',text,re.I):return True
    if _NEW_REQUEST.search(text):
        return True
    # Treat other text as a bound answer. In a manual handoff it is rejected,
    # so a private code/password is never logged as a fresh model request.
    return False
