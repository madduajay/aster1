"""Scoped trip context and meal coverage derived from the user's itinerary."""
import json
import re
from datetime import date,datetime,timedelta

MEALS={'breakfast':('08:00','08:30'),'lunch':('12:30','13:00'),'dinner':('19:00','19:30')}


def dining_followup(db,store,user,run,text,queries):
    """Resolve a range reference from scoped travel records, not a single date."""
    refers=re.search(r'\b(?:between|during|for|on|over)\s+(?:those|these|the same)\s+(?:days|dates)\b|\b(?:throughout|during|for)\s+(?:my |the |our )?(?:trip|stay)\b|\b(?:every|each)\s+day\b|\b(?:whole|entire)\s+(?:trip|stay)\b',text,re.I)
    refers=refers or re.search(r'\b(?:also|add|include|combine)\b.*\b(?:dining|restaurants?|venues?|breakfast|lunch|dinner)\b',text,re.I)
    if not refers:return {}
    owned=db.one('SELECT session_id FROM runs WHERE id=? AND user_id=?',(run,user))
    if not owned:return {}
    floor=db.owned_session(user,owned['session_id'])['context_floor']
    supplied={q.get('criteria',{}).get('location') for q in queries if q.get('criteria',{}).get('location')}
    from .contracts import canonical,leaves
    from .routes import location
    def matches(city):return not supplied or any(canonical(city)==canonical(c) or location(city) and location(city)==location(c) for c in supplied)
    # A selected working trip is stronger than an arbitrary earlier comparison.
    active=active_scope(db,user,run)
    if active and matches(active.get('location','')):
        explicit_date=re.search(r'\b\d{4}-\d{2}-\d{2}\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b',text,re.I)
        if explicit_date and any(q.get('criteria',{}).get('date') for q in queries):return {}
        meals=[meal for meal in MEALS if re.search(r'\b'+meal+r'\b',text,re.I)]
        return {**active,'meals':meals or list(MEALS)}
    for row in store.rows('SELECT envelope,result FROM tasks WHERE user_id=? AND session_id=? ORDER BY updated DESC LIMIT 30',(user,owned['session_id'])):
        envelope=json.loads(row['envelope'])
        if envelope['source_message_id']<=floor:continue
        records=leaves(json.loads(row['result']).get('results',[]))+envelope.get('queries',[])
        for domain in ('hotels','flights'):
            for record in records:
                if record['domain']!=domain:continue
                c=record['criteria'];city=c.get('destination','')
                if not city or not matches(city):continue
                start=c.get('check_in') if domain=='hotels' else c.get('departure_date')
                end=c.get('check_out') if domain=='hotels' else c.get('return_date')
                try:
                    if not 0<=(date.fromisoformat(end)-date.fromisoformat(start)).days<=30:continue
                except (ValueError,TypeError):continue
                meals=[meal for meal in MEALS if re.search(r'\b'+meal+r'\b',text,re.I)]
                meals=meals or list(dict.fromkeys(q.get('criteria',{}).get('meal') for q in queries if q.get('criteria',{}).get('meal') in MEALS)) or list(MEALS)
                return {'start':start,'end':end,'location':city,'party_size':c.get('guests',c.get('passengers',2)),'meals':meals}
    return {}


def scope(db,user,run):
    if not db.one('SELECT id FROM runs WHERE id=? AND user_id=?',(run,user)):return {}
    row=db.one("SELECT payload FROM events WHERE run_id=? AND kind='trip_requested' ORDER BY id DESC LIMIT 1",(run,))
    if not row:return {}
    result=json.loads(row['payload']).get('criteria',{}).copy()
    runs=[run]
    if db.one("SELECT id FROM events WHERE run_id=? AND kind='availability_continuation' LIMIT 1",(run,)):
        owned=db.one('SELECT session_id FROM runs WHERE id=? AND user_id=?',(run,user))
        floor=db.owned_session(user,owned['session_id'])['context_floor']
        for previous in db.rows("SELECT e.run_id,e.payload FROM events e JOIN runs r ON r.id=e.run_id WHERE r.user_id=? AND r.session_id=? AND e.kind='trip_requested' AND e.run_id!=? ORDER BY e.id DESC LIMIT 5",(user,owned['session_id'],run)):
            if json.loads(previous['payload']).get('source_message',0)<=floor:continue
            runs.append(previous['run_id']);break
    events=[]
    for identifier in runs:events+=db.rows("SELECT payload FROM events WHERE run_id=? AND kind IN ('trip_context','availability_result') ORDER BY id",(identifier,))
    for row in events:
        data=json.loads(row['payload'])
        for query in data.get('queries',data.get('results',[])):
            c=query['criteria']
            if query['domain']=='hotels':fields={'start':c.get('check_in'),'end':c.get('check_out'),'location':c.get('destination'),'party_size':c.get('guests')}
            elif query['domain']=='flights':fields={'start':c.get('departure_date'),'end':c.get('return_date'),'location':c.get('destination'),'party_size':c.get('passengers')}
            else:continue
            for key,value in fields.items():
                if value and not result.get(key):result[key]=value
    if not result.get('party_size'):result['party_size']=2
    try:
        start=date.fromisoformat(result['start']);end=date.fromisoformat(result['end'])
        if not 0<=(end-start).days<=30 or not result.get('location') or not 1<=result['party_size']<=20:return {}
    except (KeyError,ValueError,TypeError):return {}
    return result


def active_scope(db,user,run):
    """Reuse the latest scoped trip across follow-ups, never an older/different trip."""
    current=scope(db,user,run)
    if current:return current
    if db.one("SELECT id FROM events WHERE run_id=? AND kind='trip_requested' LIMIT 1",(run,)):return {}
    owned=db.one('SELECT session_id FROM runs WHERE id=? AND user_id=?',(run,user))
    if not owned:return {}
    floor=db.owned_session(user,owned['session_id'])['context_floor']
    previous=db.one("""SELECT e.run_id,e.payload FROM events e JOIN runs r ON r.id=e.run_id
        WHERE r.user_id=? AND r.session_id=? AND e.kind='trip_requested' ORDER BY e.id DESC LIMIT 1""",
        (user,owned['session_id']))
    if not previous:return {}
    source=json.loads(previous['payload']).get('source_message',0)
    if source<=floor:return {}
    from personal_agent.travel_planning import TravelPlans
    draft=TravelPlans(db).load(user,owned['session_id'])
    if draft and draft['created_message_id']>source:return {}
    if db.one('SELECT message_id FROM context_exclusions WHERE message_id=?',(source,)):return {}
    return scope(db,user,previous['run_id'])


def dining_queries(trip):
    start=date.fromisoformat(trip['start']);end=date.fromisoformat(trip['end'])
    return [{'domain':'dining','research':True,'criteria':{'location':trip['location'],'party_size':trip['party_size'],
        'date':(start+timedelta(days=offset)).isoformat(),'meal':meal}}
        for offset in range((end-start).days+1) for meal in trip.get('meals',MEALS)]


def required_meals(day,arrival='',returning=''):
    """Omit meals before arrival / near the departure airport transfer."""
    if arrival and day<arrival[:10]:return []
    result=[]
    for meal,(slot,_) in MEALS.items():
        if arrival and day==arrival[:10] and datetime.fromisoformat(day+'T'+slot).replace(tzinfo=datetime.fromisoformat(arrival).tzinfo)<datetime.fromisoformat(arrival)+timedelta(minutes=90):continue
        if returning and day==returning[:10] and int(slot[:2])>=int(returning[11:13])-2:continue
        result.append(meal)
    return result
