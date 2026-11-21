import hashlib
import json
import re
import unicodedata
from datetime import date,datetime,time,timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel,Field

DOMAINS=('dining','hotels','flights')
ROLE_DOMAINS={'dining':{'dining'},'travel':{'hotels','flights'}}
COMPANY_PORTS={'dining':55469,'hotels':55470,'flights':55471}
AGENT_PORTS={'dining':55472,'travel':55473}


def canonical(value):return ' '.join(str(value or '').casefold().strip().split())


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


class Query(BaseModel):
    domain:Literal['dining','hotels','flights']
    criteria:dict=Field(default_factory=dict)
    query_id:str=Field(default='',pattern=r'^(av-[a-zA-Z0-9_-]{8,100})?$')
    research:bool=False


class Task(BaseModel):
    request_id:str=Field(pattern=r'^[a-zA-Z0-9_-]{8,160}$')
    user_id:str=Field(min_length=1,max_length=100)
    session_id:str=Field(min_length=1,max_length=100)
    source_message_id:int
    queries:list[Query]=Field(min_length=1,max_length=93)
    preferences:dict=Field(default_factory=dict)
    requested_hotels:list[str]=Field(default_factory=list,max_length=10)
    scope_id:str=''


class WatchRequest(BaseModel):
    request_id:str
    user_id:str
    parent_workflow_id:str=Field(pattern=r'^av-main-[a-zA-Z0-9_-]+$')


class CriteriaError(ValueError):
    """Structured validation; exception text is never a customer question."""
    def __init__(self,field,reason='format'):
        self.fields=[] if field in {'connections','domain','internal'} else [field]
        self.reason=reason
        super().__init__('Invalid availability '+field+' ('+reason+')')


def local_time(value):
    """Preferred dining time is wall-clock time at the venue, not an instant."""
    try:
        if 'T' in value or re.match(r'^\d{4}-\d{2}-\d{2} ',value):return datetime.fromisoformat(value.replace('Z','+00:00')).strftime('%H:%M')
        return time.fromisoformat(value).strftime('%H:%M')
    except ValueError:pass
    compact=re.sub(r'\s+','',value).upper().replace('.','')
    for pattern in ('%I:%M%p','%I%p'):
        try:return datetime.strptime(compact,pattern).strftime('%H:%M')
        except ValueError:pass
    raise CriteriaError('preferred_time')


def validation_result(domain,raw,exc):
    result={'domain':domain,'criteria':raw,'status':'error','error':type(exc).__name__}
    if isinstance(exc,CriteriaError) and exc.fields:
        result.update(status='needs_input',missing_fields=exc.fields,validation_reason=exc.reason)
        result.pop('error')
    return result


FIELD_LABELS={'location':'city','date':'dining date','party_size':'party size','venue':'restaurant',
    'hotel':'hotel','destination':'destination','check_in':'check-in date','check_out':'check-out date',
    'guests':'number of guests','rooms':'number of rooms','stars':'hotel star rating','origin':'departure city or airport',
    'departure_date':'departure date','return_date':'return date','passengers':'number of passengers','cabin':'cabin class',
    'preferred_time':'preferred dining time','meal':'breakfast, lunch or dinner'}


def input_question(result,label):
    fields=result.get('missing_fields',[]);reason=result.get('validation_reason')
    allowed={'dining':{'location','date','party_size','venue','preferred_time','meal'},'hotels':{'hotel','destination','check_in','check_out','guests','rooms','stars'},'flights':{'origin','destination','departure_date','return_date','passengers','cabin'}}
    if any(field not in allowed.get(result['domain'],set()) for field in fields):return label+': availability could not be checked.'
    if not fields or any(field not in FIELD_LABELS for field in fields):return label+': availability could not be checked.'
    if fields==['preferred_time']:return 'What time would you prefer for '+label+'? For example, 7:00 PM.'
    if fields==['origin']:return 'Which city are you flying from? Any airport in that city is fine.'
    if reason=='past':return 'Which current or future '+FIELD_LABELS[fields[0]]+' would you like for '+label+'?'
    if reason=='order' and fields==['check_out']:return 'What check-out date would you like? It needs to be after check-in.'
    if reason=='order' and fields==['return_date']:return 'What return date would you like? It needs to be on or after departure.'
    return 'Please provide '+', '.join(FIELD_LABELS[field] for field in fields)+' for '+label+'.'


def criteria(domain,raw):
    """Normalize criteria with user-authorized occupancy defaults."""
    fields={'dining':['venue','cuisine','location','date','party_size','preferred_time','meal'],
        'hotels':['hotel','destination','check_in','check_out','guests','rooms','stars'],
        'flights':['origin','destination','departure_date','return_date','passengers','cabin','carrier','connections']}
    if domain not in fields:raise CriteriaError('domain','internal')
    c={key:value.strip() if isinstance(value,str) else value for key,value in raw.items() if key in fields[domain] and value not in ('',None)}
    defaults={'dining':{'party_size':2},'hotels':{'guests':2,'rooms':1},'flights':{'passengers':2}}[domain]
    for key,value in defaults.items():c.setdefault(key,value)
    from personal_agent import config
    today=datetime.now(ZoneInfo(config.TIMEZONE)).date()
    if domain=='dining':c.setdefault('date',today.isoformat())
    if domain=='hotels':c.setdefault('check_in',(today+timedelta(days=1)).isoformat())
    required={'dining':['location','date','party_size'],'hotels':([] if c.get('hotel') else ['destination'])+['check_in','check_out','guests','rooms'],
        'flights':['origin','destination','departure_date','passengers','cabin']}[domain]
    for key,value in c.items():
        if key=='meal' and isinstance(value,str):value=value.casefold();c[key]=value
        if key=='meal' and value not in {'breakfast','lunch','dinner'}:raise CriteriaError('meal')
        if key=='connections':
            if not isinstance(value,list) or len(value)>3 or any(not isinstance(v,str) or len(v)>100 for v in value):raise CriteriaError('connections','internal')
            continue
        if key=='stars':
            if type(value) is not int or not 1<=value<=5:raise CriteriaError('stars','range')
            continue
        if key not in {'party_size','guests','rooms','passengers'} and not isinstance(value,str):raise CriteriaError(key)
        if isinstance(value,str) and len(value)>200:raise CriteriaError(key)
        if key in {'party_size','guests','rooms','passengers'}:
            if isinstance(value,str) and value.isdigit():value=int(value);c[key]=value
            if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=20:raise CriteriaError(key,'range')
        if key in {'date','check_in','check_out','departure_date','return_date'}:
            try:parsed=datetime.fromisoformat(value.replace('Z','+00:00')).date() if 'T' in value or ' ' in value else date.fromisoformat(value)
            except ValueError:raise CriteriaError(key) from None
            if parsed<today:raise CriteriaError(key,'past')
            c[key]=parsed.isoformat()
        if key=='preferred_time':c[key]=local_time(value)
    if domain=='hotels' and not c.get('check_out'):c['check_out']=(date.fromisoformat(c['check_in'])+timedelta(days=1)).isoformat()
    if c.get('check_in') and c.get('check_out') and c['check_out']<=c['check_in']:raise CriteriaError('check_out','order')
    if c.get('return_date') and c.get('departure_date') and c['return_date']<c['departure_date']:raise CriteriaError('return_date','order')
    return c,[key for key in required if not c.get(key)]


def expires_at(queries,timezone='America/Phoenix'):
    days=[q['criteria'].get({'dining':'date','hotels':'check_in','flights':'departure_date'}[q['domain']]) for q in queries]
    return min(datetime.combine(date.fromisoformat(day),time.max,ZoneInfo(timezone)).timestamp() for day in days if day)


def role_for(domain):return 'dining' if domain=='dining' else 'travel'


def leaves(results):
    return [child for result in results for child in (result.get('candidate_results') or [result])]


def candidate_id(parent,domain,c):return 'av-'+digest([parent,identity(domain,c)])[:40]


def identity(domain,c):
    """Semantic identity keeps spelling/case aliases on the same inventory watch."""
    def norm(value):return re.sub(r'[^a-z0-9]+',' ',unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower()).strip()
    result={k:norm(v) if isinstance(v,str) else v for k,v in c.items()}
    for field in ('location','destination'):
        if result.get(field) in {'ny','nyc','manhattan','new york','new york city','new york ny'}:result[field]='new york'
        if result.get(field) in {'dxb','dubai dxb','dubai uae','dubai united arab emirates'}:result[field]='dubai'
    if result.get('origin') in {'phx','phoenix az','phoenix arizona'}:result['origin']='phoenix'
    if domain=='dining' and result.get('venue') in {'carbone nyc','carbone ny','carbone new york'}:result['venue']='carbone'
    if domain=='hotels' and result.get('hotel'):
        result.pop('stars',None) # Property identity stays stable when its rating is metadata.
        hotel=re.sub(r'\b(?:in )?(?:new york city|new york|nyc|ny)\b',' ',result['hotel']).removeprefix('the ').replace('5th','fifth')
        hotel=re.sub(r'\bave\b','avenue',' '.join(hotel.split())).removeprefix('the ')
        if hotel in {'fifth avenue','fifth avenue hotel'}:
            result['hotel']='fifth avenue hotel';result.setdefault('destination','new york')
    if domain=='flights':
        if result.get('destination') in {'dxb','dubai dxb','dxb dubai','dubai uae','dubai'}:result['destination']='dubai'
        if result.get('carrier') in {'emirates','ek','emirates airline','emirates airlines'}:result['carrier']='emirates'
        if result.get('cabin') in {'first','first class','first class cabin'}:result['cabin']='first class'
    return result


def describe(result):
    c=result['criteria'];domain=result['domain']
    label=str(c.get('venue') or c.get('hotel') or c.get('carrier') or c.get('destination') or domain.title())
    if result.get('status')=='needs_input':
        return input_question(result,label)
    if result.get('status')=='error':return label+': availability could not be checked.'
    if not result['available']:
        if domain=='dining':details=f" for {c.get('party_size','')} in {c.get('location','')} on {c.get('date','')}"
        elif domain=='hotels':details=f" for {c.get('guests','')} guests, {c.get('check_in','')} to {c.get('check_out','')}"
        else:details=f" {c.get('cabin','')} from {c.get('origin','')} to {c.get('destination','')} on {c.get('departure_date','')}"
        return label+' is currently unavailable'+details+'. I’ll let you know when an option opens up.'
    options=result.get('options',[])
    if domain=='dining':return '; '.join(o['name']+': '+', '.join(o['times']) for o in options)
    if domain=='hotels':return '; '.join(o['name']+' — '+(str(o['stars'])+'-star, ' if o.get('stars') else '')+o['room_type']+', '+o['check_in']+' to '+o['check_out'] for o in options)
    return '; '.join(o['carrier']+' '+o['flight_number']+' — '+o['origin']+' to '+o['destination']+(' via '+' → '.join(o['connections']) if o.get('connections') else '')+', '+o['cabin']+', '+o['departure']+('; return '+o['return_departure'] if o.get('return_departure') else '') for o in options)
