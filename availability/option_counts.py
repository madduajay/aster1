"""Ground requested list lengths in user text; vary unspecified lists."""
import re
import secrets

WORDS={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,'fifteen':15,'twenty':20}
NUMBER=r'(\d+|'+ '|'.join(WORDS)+r')'


def requested_items(text):
    match=re.search(r'\b(?:show|find|recommend|give|suggest|want)(?:\s+me)?\s+'+NUMBER+r'\b',text,re.I)
    if not match:return 1
    token=match[1].casefold();value=int(token) if token.isdigit() else WORDS[token]
    return max(1,value)


def requested(text,domain):
    noun='hotels?' if domain=='hotels' else 'flights?'
    modifiers=r'(?:(?:best|top|available|different|more|business|class|economy|premium|first|direct|nonstop|round[- ]trip|[1-5][- ]star)\s+)*'
    patterns=[r'\b'+NUMBER+r'\s+'+modifiers+noun+r'\b(?!\s+(?:rooms?|guests?|passengers?|seats?|tickets?)\b)',
        r'\b'+NUMBER+r'\s+(?:options?|choices?|recommendations?)\s+(?:for\s+)?'+noun+r'\b',
        r'\b'+noun+r'\s*[:=]\s*'+NUMBER+r'\b']
    matches=[m for p in patterns for m in re.finditer(p,text,re.I)]
    if not matches:
        matches=list(re.finditer(r'\b'+NUMBER+r'\s+(?:of each|each)\b|\b'+NUMBER+r'\s+(?:hotels?\s+and\s+flights?|flights?\s+and\s+hotels?)\b',text,re.I))
        if matches:
            m=max(matches,key=lambda m:m.start());token=next(g for g in m.groups() if g is not None).casefold()
            value=int(token) if token.isdigit() else WORDS[token]
            return value if value>0 else None
        matches=list(re.finditer(r'\b'+NUMBER+r'\s+(?:options?|choices?|recommendations?)\b(?!\s+(?:for\s+)?(?:hotels?|flights?)\b)',text,re.I))
    if not matches:return None
    token=max(matches,key=lambda m:m.start())[1].casefold()
    value=int(token) if token.isdigit() else WORDS[token]
    return value if value>0 else None


def policy(db,run,source,domains):
    previous=db.one("SELECT payload FROM events WHERE run_id=? AND kind='option_counts' ORDER BY id DESC LIMIT 1",(run,))
    import json
    saved=json.loads(previous['payload']) if previous else {}
    for domain in domains:
        if domain not in {'hotels','flights'} or domain in saved:continue
        count=requested(source,domain)
        saved[domain]={'count':count if count is not None else 4+secrets.randbelow(4),'explicit':count is not None}
    if saved:db.event(run,'option_counts',saved)
    return saved
