"""Normalize route identities and configure Company flight clocks."""
import math
import re
import unicodedata
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo


def key(value):
    return re.sub(r'[^a-z0-9]+',' ',unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode().lower()).strip()


# Company schedule configuration: aliases, timezone, latitude and longitude.
PLACES=[
    ('phoenix phx','America/Phoenix',33.43,-112.01),
    ('new york nyc jfk ewr lga','America/New_York',40.64,-73.78),
    ('san francisco sfo','America/Los_Angeles',37.62,-122.38),
    ('los angeles lax','America/Los_Angeles',33.94,-118.41),
    ('miami mia','America/New_York',25.79,-80.29),
    ('paris cdg ory','Europe/Paris',49.01,2.55),
    ('london lhr heathrow','Europe/London',51.47,-.45),
    ('tokyo hnd nrt','Asia/Tokyo',35.55,139.78),
    ('sydney syd','Australia/Sydney',-33.94,151.18),
    ('cape town cpt','Africa/Johannesburg',-33.97,18.6),
    ('rio de janeiro gig sdu','America/Sao_Paulo',-22.81,-43.25),
    ('dubai dxb','Asia/Dubai',25.25,55.36),
    ('frankfurt fra','Europe/Berlin',50.04,8.56),
    ('munich muc','Europe/Berlin',48.35,11.79),
    ('istanbul ist','Europe/Istanbul',41.28,28.75),
    ('doha doh','Asia/Qatar',25.27,51.61),
    ('auckland akl','Pacific/Auckland',-37.01,174.79),
    ('detroit dtw','America/Detroit',42.21,-83.35),
    ('chicago ord','America/Chicago',41.98,-87.9),
    ('washington dca iad','America/New_York',38.85,-77.04),
    ('ponta delgada pdl','Atlantic/Azores',37.74,-25.7),
    ('oslo osl','Europe/Oslo',60.2,11.1),
    ('belem bel','America/Belem',-1.38,-48.48),
    ('brasilia bsb','America/Sao_Paulo',-15.87,-47.92),
    ('singapore sin','Asia/Singapore',1.36,103.99),
    ('bangkok bkk','Asia/Bangkok',13.69,100.75),
    ('rome fco','Europe/Rome',41.8,12.24),
    ('barcelona bcn','Europe/Madrid',41.3,2.08),
]


CITY_NAMES=['phoenix','new york','san francisco','los angeles','miami','paris','london','tokyo','sydney','cape town','rio de janeiro','dubai','frankfurt','munich','istanbul','doha','auckland','detroit','chicago','washington','ponta delgada','oslo','belem','brasilia','singapore','bangkok','rome','barcelona']


def location(value):
    value=key(value)
    for city,entry in zip(CITY_NAMES,PLACES):
        names=entry[0];aliases=names[len(city):].split()
        if re.search(r'\b'+re.escape(city)+r'\b',value) or any(re.search(r'\b'+code+r'\b',value) for code in aliases):return entry


def place(value):
    entry=location(value)
    return entry[1:] if entry else ('UTC',None,None)


def connections(values):
    """City + IATA aliases describe one stop, not two sequential legs."""
    result=[];seen=set()
    for value in values:
        normalized=key(value)
        code=re.search(r'\b([A-Z]{3})\b',value)
        entry=location(value);matched=entry[0] if entry else None
        city=CITY_NAMES[PLACES.index(entry)] if entry else ''
        codes=[v.upper() for v in matched[len(city):].split() if len(v)==3] if matched else []
        airport=code[1] if code and code[1] in codes else (codes[0] if codes else None)
        identity=airport or normalized
        if identity in seen:continue
        seen.add(identity)
        result.append(airport or value.strip())
    return result


def airline(name):
    publishers={'flightroutes','flight routes','flightconnections','flight connections','google','google flights','kayak','skyscanner','expedia','tripadvisor','booking com'}
    return bool(name.strip()) and all(key(part) not in publishers for part in re.split(r'\s+[+&]\s+',name))


def schedule(c):
    points=[place(v) for v in [c['origin'],*c.get('connections',[]),c['destination']]]
    hours=0
    for (_,lat,lon),(_,lat2,lon2) in zip(points,points[1:]):
        if lat is None or lat2 is None:hours+=10;continue
        a,b=map(math.radians,(lat,lat2));dl=math.radians(lon2-lon)
        distance=6371*2*math.asin(min(1,math.sqrt(math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(dl/2)**2)))
        hours+=distance/850+.75
    hours+=2*len(c.get('connections',[]))
    minutes=math.ceil(hours*60/15)*15
    departure=datetime.fromisoformat(c['departure_date']+'T12:00:00').replace(tzinfo=ZoneInfo(points[0][0]))
    arrival=(departure.astimezone(ZoneInfo('UTC'))+timedelta(minutes=minutes)).astimezone(ZoneInfo(points[-1][0]))
    result={'departure':departure.isoformat(),'arrival':arrival.isoformat(),'duration_minutes':minutes}
    if c.get('return_date'):
        returning=datetime.fromisoformat(c['return_date']+'T14:00:00').replace(tzinfo=ZoneInfo(points[-1][0]))
        result['return_departure']=returning.isoformat()
        result['return_arrival']=(returning.astimezone(ZoneInfo('UTC'))+timedelta(minutes=minutes)).astimezone(ZoneInfo(points[0][0])).isoformat()
    return result
