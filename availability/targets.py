"""Bind named hotel intent to literal user text, independent of discovery."""
import re
from .contracts import canonical,identity


def generic_target(name):
    value=re.sub(r'[^a-z0-9]+',' ',canonical(name)).strip()
    if re.search(r'\bflights\b',value) and not re.search(r'\bhotel\b',value):return True
    return bool(re.match(r'^(?:(?:business|economy|premium|first|class|round|trip|return|nonstop|direct|cheap|available)\s+)*(?:flights?|airfares?|airlines?|availability|options|dining|restaurants?)(?:$|\s+(?:from|to|and|options|availability|on)\b)',value))


def literal_hotels(text,candidates=(),hotel_domain=False):
    source=canonical(text);found=[]
    alternatives=re.search(r'\b(?:alternatives?\s+(?:to|for)|other hotels\s+(?:than|instead of))\b',source)
    negated=re.search(r"\b(?:no|not|without|don['’]t|do not)\s+(?:show\s+)?alternatives?\b",source)
    if alternatives and not negated:return []
    def specific(name):
        value=canonical(name).removeprefix('the ')
        if generic_target(value):return False
        if re.search(r'\bhotels$',value):return False
        if re.match(r'^(?:that|this|it|same|another)\b',value) or re.search(r'\b(?:one|ones|option)$',value):return False
        if re.match(r'^\d\s*[- ]?\s*stars?\s+hotels?\b',value):return False
        return bool(hotel_key(name)) and not re.match(r'^(?:(?:a|an|any|some|other|alternatives|available)\b|(?:best|nice|good|cheap|affordable|luxury|budget)\s+hotels?\b|hotels\b|hotel\s+(?:in|near|at|availability|options|recommendations)\b)',value)
    for name in candidates:
        name=name.strip(' \"\'')
        if 2<=len(name)<=160 and re.search(r'(?<!\w)'+re.escape(canonical(name))+r'(?!\w)',source) and specific(name) and not re.search(re.escape(canonical(name))+r'\s+hotels\b',source):
            if canonical(name) not in {canonical(n) for n in found}:found.append(name)
    if found:return found[:10]
    # Backup for explicit availability requests, not open-ended recommendations.
    match=re.search(r'\bavailability\s+(?:for|at)\s+(.+)',text,re.I)
    if not match:match=re.match(r'^(?:(?:actually|please|hey|can you|could you|would you)\s+)*(?:use|check|try|what about|how about|switch to|change to)\s+(.+)',text,re.I)
    if not match:return []
    name=re.split(r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|from|on|for|instead|check[- ]?in)\b|\b\d{4}-\d{2}-\d{2}\b',match[1],maxsplit=1,flags=re.I)[0].strip(' ,.-?!;:')
    if re.search(r'\b(?:dining|meals|venues|restaurants|itinerary|entertainment)\b',name,re.I):return []
    if not 2<=len(name)<=160 or not (hotel_domain or candidates or re.search(r'\bhotel\b',name,re.I)):return []
    if not specific(name) or re.match(r'^(?:\d|(?:the )?hotels?\b)',name,re.I):return []
    return [name]


def hotel_key(name):
    if not isinstance(name,str):return ''
    value=identity('hotels',{'hotel':name}).get('hotel','')
    value=re.sub(r'\b(?:in )?(?:new york city|new york|nyc|ny)\b',' ',value)
    value=re.sub(r'\b(?:the|hotel|hotels)\b',' ',value)
    value=re.sub(r'\btimes(?= square\b)','time',value)
    return ' '.join(value.split())


def requested_match(name,targets):
    key=hotel_key(name)
    if not key:return None
    matches=[target for target in targets if key==hotel_key(target)]
    return matches[0] if len(matches)==1 else None
