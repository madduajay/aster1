"""User-requested monitoring cadence, with a one-minute minimum."""
import re

DEFAULT=300
MINIMUM=60
WORDS={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10,'fifteen':15,'twenty':20,'thirty':30,'sixty':60,'a':1,'an':1}


def requested(text):
    value=text.casefold()
    match=re.search(r'\bevery\s+(\d+(?:\.\d+)?|'+ '|'.join(WORDS)+r')\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b',value)
    if match:
        amount=WORDS.get(match[1])
        if amount is None:amount=float(match[1])
        unit=match[2];scale=1 if unit.startswith(('sec',)) else 60 if unit.startswith('min') else 3600 if unit.startswith(('hour','hr')) else 86400 if unit.startswith('day') else 604800
        return max(MINIMUM,round(amount*scale))
    if re.search(r'\bevery\s+(?:half\s+(?:an?\s+)?hour|half-hour)\b',value):return 1800
    if re.search(r'\bhourly\b',value):return 3600
    return None


def seconds(preferences):
    value=preferences.get('monitor_interval_seconds',DEFAULT)
    return max(MINIMUM,value) if type(value) is int else DEFAULT


def label(value):
    value=max(MINIMUM,int(value))
    if value%3600==0:count=value//3600;unit='hour'
    elif value%60==0:count=value//60;unit='minute'
    else:count=value;unit='second'
    return str(count)+' '+unit+('s' if count!=1 else '')
