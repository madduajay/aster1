"""Scoped pending criteria and narrow, explicit answers to missing fields."""
import re
from .contracts import role_for


def room_counts(text):
    numbers={'a':1,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10}
    found=re.findall(r'\b(\d{1,2}|a|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:(?:hotel|single|double|twin|family|deluxe|standard|connecting|adjoining)\s+){0,2}(?:rooms?|suites?|apartments?)\b',text.casefold())
    found+=re.findall(r'\brooms?\s*[:=]\s*(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b',text.casefold())
    values={int(v) if v.isdigit() else numbers[v] for v in found}
    if re.search(r'\b(?:a )?couple (?:of )?rooms?\b',text.casefold()):values.add(2)
    return values


def star_ratings(text):return {int(v) for v in re.findall(r'\b([1-5])[- ]star\b',text.casefold())}


def party_counts(text):
    numbers={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10}
    number=r'(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)'
    pairs=re.findall(r'\b'+number+r'\s+(adults?|child(?:ren)?|kids?|guests?|people|persons?|travelers?|travellers?|passengers?|diners?)\b',text.casefold())
    values={int(n) if n.isdigit() else numbers[n] for n,kind in pairs if not kind.startswith(('adult','child','kid'))}
    family=[int(n) if n.isdigit() else numbers[n] for n,kind in pairs if kind.startswith(('adult','child','kid'))]
    if family:values.add(sum(family))
    for n in re.findall(r'\bfor\s+'+number+r'\b(?!\s*(?:nights?|days?|rooms?|stars?|hours?|minutes?|adults?|child(?:ren)?|kids?|guests?|people|travelers?|passengers?|hotels?|flights?|options?|choices?|recommendations?|restaurants?|venues?)\b)',text.casefold()):values.add(int(n) if n.isdigit() else numbers[n])
    if re.search(r'\b(?:for me|just me|solo)\b',text.casefold()):values.add(1)
    for n in re.findall(r'\b'+number+r'\s+of us\b',text.casefold()):values.add(int(n) if n.isdigit() else numbers[n])
    if re.search(r'\b(?:my (?:wife|husband|partner|spouse) and (?:me|i)|me and my (?:wife|husband|partner|spouse))\b',text.casefold()):values.add(2)
    return values


def cabins(text):
    text=text.casefold();found=set()
    if 'premium economy' in text:found.add('premium economy')
    elif re.search(r'\b(?:economy|coach)\b',text):found.add('economy')
    if re.search(r'\bbusiness\b(?!\s+trip)',text):found.add('business')
    if re.search(r'\bfirst[- ]class\b',text) or text.strip(' .!')=='first':found.add('first class')
    return found


def cabin_value(value):
    value=' '.join(str(value).casefold().replace('-',' ').split())
    return {'first':'first class','first class cabin':'first class','business class':'business','coach':'economy'}.get(value,value)


def explicit_reply(text,domain):
    text=text.strip().rstrip('.!')
    fields={r'from\s+(.{2,80})':'origin',r'to\s+(.{2,80})':'destination'} if domain=='flights' else ({r'in\s+(.{2,80})':'location'} if domain=='dining' else {r'in\s+(.{2,80})':'destination'})
    for pattern,field in fields.items():
        match=re.fullmatch(pattern,text,re.I)
        if match:return {field:match[1]}
    if domain=='hotels':
        match=re.fullmatch(r'(?:actually\s+)?(?:change\s+)?(check[- ]?out|check[- ]?in)\s+(?:to\s+)?(\d{4}-\d{2}-\d{2})',text,re.I)
        if match:return {'check_out' if 'out' in match[1].casefold() else 'check_in':match[2]}
    return {}


def continuation_roles(text,pending,recent=()):
    value=' '.join(text.casefold().strip(' .!').split())
    if value in {'continue','please continue','go on'} and pending:return {role_for(q['domain']) for q in pending}
    fields=set()
    count=r'(?:\d{1,2}|one|two|three|four|five|six)'
    if re.fullmatch(r'(?:just )?'+count+r' rooms?',value):fields={'rooms'}
    elif re.fullmatch(count+r' (?:adults?|guests?|people|persons?|travelers?|travellers?|passengers?|diners?)',value):fields={'guests','passengers','party_size'}
    elif re.fullmatch(r'(?:make that |change to |actually )?(?:economy|premium economy|business(?: class)?|first(?: class)?)',value):fields={'cabin'}
    elif re.fullmatch(r'from [\w ,.-]{2,60}',value):fields={'origin'}
    elif re.fullmatch(r'to [\w ,.-]{2,60}',value):fields={'destination'}
    elif re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):fields={'date','check_in','check_out','departure_date','return_date'}
    elif re.fullmatch(r'(?:actually )?(?:change )?check[- ]?out (?:to )?\d{4}-\d{2}-\d{2}',value):fields={'check_out'}
    roles={role_for(q['domain']) for q in pending if fields.intersection(q.get('missing_fields',[]))}
    allowed={'hotels':{'rooms','guests','destination','check_in','check_out'},'flights':{'origin','destination','passengers','cabin','departure_date','return_date'},'dining':{'party_size','date','location'}}
    roles.update(role_for(q['domain']) for q in recent if fields.intersection(allowed[q['domain']]))
    return roles
