"""Session-only travel drafts; research assumptions never become user facts."""
import json
import re
import time
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict, StrictInt, StrictStr

TravelIntent = Literal['none', 'explore', 'compare', 'check_availability', 'prepare', 'execute']
DetailKey = Literal['origin', 'destination', 'start_date', 'end_date', 'duration_days', 'travelers',
                    'rooms', 'cabin', 'hotel_quality', 'budget', 'interests', 'season', 'horizon']


class ConfirmedDetail(BaseModel):
    field: DetailKey
    value: StrictStr | StrictInt
    source_message_id: int
    evidence: str = Field(min_length=1, max_length=1000)


class Destination(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    reason: str = Field(min_length=1, max_length=500)
    tradeoff: str = Field(default='', max_length=500)
    source_urls: list[str] = Field(min_length=1, max_length=8)


class TravelWindow(BaseModel):
    start_date: date
    end_date: date
    label: Literal['best_overall_fit', 'potential_value', 'seasonal_fit']
    reason: str = Field(min_length=1, max_length=500)
    source_urls: list[str] = Field(min_length=1, max_length=8)


class PreparationItem(BaseModel):
    category: Literal['gear', 'transfers', 'connectivity', 'documents', 'forms', 'appointments']
    title: str = Field(min_length=1, max_length=300)
    status: Literal['suggested', 'requested', 'completed'] = 'suggested'


class Selection(BaseModel):
    destination: str = Field(default='', max_length=150)
    window_label: Literal['', 'best_overall_fit', 'potential_value', 'seasonal_fit'] = ''
    source_message_id: int
    evidence: str = Field(min_length=1, max_length=1000)


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: list[ConfirmedDetail] = Field(default_factory=list, max_length=16)
    destinations: list[Destination] | None = Field(default=None, max_length=3)
    windows: list[TravelWindow] | None = Field(default=None, max_length=3)
    selected: Selection | None = None
    preparation: list[PreparationItem] | None = Field(default=None, max_length=30)


DEFAULTS = {'duration_days': 7, 'cabin': 'economy', 'hotel_quality': 'mid luxury (4-star)', 'travelers': 2, 'rooms': 1}


def planning_disclosure(draft, assumptions, trip=None, text=''):
    """Show the working dates and provisional inputs before the itinerary."""
    from availability.followup import cabins, party_counts, room_counts, star_ratings
    values=dict((draft or {}).get('assumptions',{}))
    values.update(assumptions)
    confirmed=(draft or {}).get('confirmed',{})
    for key in confirmed:values.pop(key,None)
    for key,parse in [('cabin',cabins),('travelers',party_counts),('rooms',room_counts),('hotel_quality',star_ratings)]:
        if parse(text):values.pop(key,None)
    if trip_duration(text,default=None) is not None:values.pop('duration_days',None)
    if trip and trip.get('start') and trip.get('end'):values.pop('duration_days',None)
    labels={'duration_days':'days','cabin':'cabin','hotel_quality':'hotels','travelers':'travelers','rooms':'room' if values.get('rooms')==1 else 'rooms'}
    defaults=[f'{values[key]} {label}' for key,label in labels.items() if key in values]
    lines=[]
    if defaults:lines.append('**Planning defaults (assumed):** '+', '.join(defaults)+'.')
    def display(value):
        try:return date.fromisoformat(value).strftime('%b %d, %Y')
        except (ValueError,TypeError):return str(value)
    start=(trip or {}).get('start') or assumptions.get('departure_date')
    end=(trip or {}).get('end') or assumptions.get('return_date')
    if start or end:
        chosen=bool(assumptions.get('departure_date') or assumptions.get('return_date'))
        lines.append('**Travel dates'+(' (proposed; assumed dates)' if chosen else '')+':** '
                     +display(start or 'not selected')+' → '+display(end or 'not selected')+'.')
    elif draft:
        windows=draft.get('windows',[])
        if windows:
            lines.append('**Proposed date options (not confirmed):** '+'; '.join(
                display(w['start_date'])+' → '+display(w['end_date']) for w in windows)+'.')
        else:lines.append('**Travel dates:** Not selected yet.')
    return '\n\n'.join(lines)

MONTHS = ('january','february','march','april','may','june','july','august','september','october','november','december')
MONTH_PATTERN = '|'.join(name+'|'+name[:3] for name in MONTHS)


def named_destination(text):
    match=re.search(r'\b(?:trip|vacation|holiday|itinerary)\s+(?:to|in|for)\s+(.+?)(?=\s+from\b|\s+(?:on|starting|leaving|departing|for)\b|\s+(?:'+MONTH_PATTERN+r')\b|\s+\d{4}-\d{2}-\d{2}|[,;\n]|$)',text,re.I)
    match=match or re.search(r'\b(?:pick|choose|go with|visit)\s+(.+?)(?=\s+from\b|[,.;\n]|$)',text,re.I)
    value=match[1].strip() if match else ''
    return '' if re.search(r'\b(?:somewhere|anywhere|undecided|something)\b|^(?:a |an )?(?:place|destination|beach destination|warm place)\b',value,re.I) else value


def trip_duration(text, default=7):
    numbers={'a':1,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10,'fourteen':14}
    values={ (int(n) if n.isdigit() else numbers[n])*(7 if unit.startswith('week') else 1)
        for n,unit in re.findall(r'\b(\d{1,3}|a|one|two|three|four|five|six|seven|eight|nine|ten|fourteen)[ -]*(days?|weeks?)\b',text.lower())}
    return next(iter(values)) if len(values)==1 and 1<=next(iter(values))<=31 else default


def itinerary_details(text, today, *, destination='', start='', end='', allow_defaults=False, duration_days=7):
    """Specific itinerary requests outrank a contradictory exploration label."""
    daily=bool(re.search(r'\bday[ -]by[ -]day\b|\bitinerary\b',text,re.I))
    planning=bool(re.search(r'\b(?:plan|build|create|organize|arrange)\b.*\btrip\b',text,re.I))
    destination=named_destination(text) or destination
    if not destination or re.search(r'\b(?:somewhere|anywhere|undecided)\b|^(?:a |an )?(?:place|destination|beach destination|warm place)\b',destination,re.I):return None
    if not (daily or planning or allow_defaults):return None
    if re.search(r"\b(?:dates? (?:are )?flexible|flexible dates?|not sure when|don['’]?t know (?:exactly )?when|undecided|compare (?:date )?windows|best (?:travel )?(?:month|season|time))\b",text,re.I):return None
    assumptions={}
    iso=re.findall(r'\b\d{4}-\d{2}-\d{2}\b',text)
    if len(iso)==2:start,end=iso
    else:
        pattern=(r'\b(?P<m1>'+MONTH_PATTERN+r')\.?\s+(?P<d1>\d{1,2})(?:st|nd|rd|th)?'
                 r'(?:[, ]+(?P<y1>20\d{2}))?\s*(?:to|through|until|[–—-])\s*'
                 r'(?:(?P<m2>'+MONTH_PATTERN+r')\.?\s+)?(?P<d2>\d{1,2})(?:st|nd|rd|th)?'
                 r'(?:[, ]+(?P<y2>20\d{2}))?\b')
        match=re.search(pattern,text,re.I)
        if match:
            def month(value):return next(i for i,name in enumerate(MONTHS,1) if name.startswith(value.lower()))
            m1=month(match['m1']);m2=month(match['m2']) if match['m2'] else m1
            d1=int(match['d1']);d2=int(match['d2']);wrap=(m2,d2)<(m1,d1)
            year=int(match['y1'] or (int(match['y2'])-int(wrap) if match['y2'] else today.year))
            try:
                first=date(year,m1,d1)
                if first<today and not match['y1'] and not match['y2']:year+=1;first=date(year,m1,d1)
                last=date(int(match['y2'] or year+int(wrap)),m2,d2)
            except ValueError:return None
            start,end=first.isoformat(),last.isoformat()
    if allow_defaults:
        if not start:
            start=(today+timedelta(days=7)).isoformat();assumptions['departure_date']=start
        if not end:
            try:end=(date.fromisoformat(start)+timedelta(days=duration_days-1)).isoformat()
            except ValueError:return None
            assumptions['return_date']=end
    try:
        if date.fromisoformat(start)<today or date.fromisoformat(end)<date.fromisoformat(start):return None
    except (ValueError,TypeError):return None
    return {'start':start,'end':end,'destination':destination,'daily':daily,'planning':planning,'assumptions':assumptions,
            'dining':bool(re.search(r'\b(?:dining|restaurants?|venues?|meals?|breakfast|lunch|dinner)\b',text,re.I))}


def grounded_detail(item):
    """Never turn comparison defaults into confirmed inputs using a generic quote."""
    from availability.followup import cabins, cabin_value, party_counts, room_counts
    value, quote = item.value, item.evidence.casefold()
    if item.field == 'cabin':
        return cabin_value(value) in cabins(quote)
    if item.field in {'travelers', 'rooms', 'duration_days'}:
        numbers = {word: index for index, word in enumerate(
            ['zero','one','two','three','four','five','six','seven','eight','nine','ten','eleven','twelve','thirteen','fourteen'], 0)}
        bare = quote.strip(' .!')
        explicit = {int(bare)} if bare.isdigit() else ({numbers[bare]} if bare in numbers else set())
        if item.field == 'travelers':return value in party_counts(quote) | explicit
        if item.field == 'rooms':return value in room_counts(quote) | explicit
        durations = set()
        for amount, unit in re.findall(r'\b(\d{1,3}|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen)\s*(days?|weeks?)\b', quote):
            n = int(amount) if amount.isdigit() else (1 if amount == 'a' else numbers[amount])
            durations.add(n * (7 if unit.startswith('week') else 1))
        return value in durations | explicit
    if item.field in {'start_date','end_date'}:
        date.fromisoformat(str(value))
        return bool(re.search(r'\d|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday', quote))
    return isinstance(value, str) and value.casefold().strip() in quote

ROUTING = (
    ' Destination exploration is ONLY for genuinely open requests with no known destination. '
    'For any travel request extract trip_destination, not just itineraries. A known destination means '
    'proceed with targeted planning/checks; missing dates, cabin, guests or rooms are not reasons to '
    'enter exploration. Unless explicitly supplied for this trip, default economy, upscale 4-star '
    'mid-luxury hotels, 2 guests and 1 room. For an undated trip assume 7 days starting one week from '
    'the runtime date and disclose the dates. Explicit duration or known trip dates take precedence. '
    ' PRIORITY: A requested itinerary with a known destination and date range is check_availability, '
    'not explore/compare, even when the user asks for recommendations or says plan a trip. Set '
    'trip_itinerary=true, travel_availability=true, and dining_availability=true for requested meals/venues. '
    'Travel and Dining perform their own targeted research and inventory checks. Do not ask for a '
    'trip-brief confirmation. General sightseeing research remains main-agent work. '
    ' Classify travel_intent as none for unrelated conversation; explore for loose trip guidelines, destination '
    'inspiration, or just choose when continuing travel exploration; compare for flexible dates, seasons, value '
    'or comparing proposed destinations/windows; check_availability for concrete inventory checks; prepare '
    'for trip gear, logistics, documents or appointment research; execute for a user-requested booking, purchase, '
    'form fill or appointment action related to travel. These are intents, not mandatory stages. '
    'For explore/compare, missing destination, dates, cabin or origin must NOT trigger travel_availability, '
    'dining_availability or trip_itinerary: research useful options first. Set requires_research=true for '
    'explore/compare and preparation recommendations. Short selections/corrections resolve against travel_draft '
    'only when the current message concerns that trip; greetings/unrelated requests must remain none. '
    'Set new_travel_plan=true only when the user explicitly starts a different trip. A request to choose an '
    'option authorizes a provisional planning choice, never a booking. For execute use browser_navigation=true '
    'and do not set availability flags. Keep shopping flags for gear product recommendations. '
)

INSTRUCTIONS = (
    'Travel planning: give useful options first and at most ONE optional follow-up alongside them. '
    'Explore: research three destinations (or fewer with an explicit evidence gap), explain fit/tradeoff, '
    'and recommend one; "just choose" means make a leading recommendation now. Compare: research three '
    'candidate windows for the selected destination: best overall fit, potential value, seasonal fit. '
    'Use requested duration, else 7 days, and next 12 months from the runtime clock if no horizon is given. '
    'Respect confirmed constraints; if none fit, explain the conflict and label alternatives. '
    'Use explicit trip choices before defaults: economy, upscale 4-star mid-luxury hotels, 2 travelers, 1 room. '
    'Do not silently use a saved cabin preference instead of economy for a new trip. '
    'Before displaying any trip itinerary, show a brief planning summary with the exact departure and return '
    'dates (including year), duration, travelers, rooms, cabin and hotel level. Clearly label which were '
    'chosen as defaults versus supplied by the user. Proposed date windows are not confirmed dates. '
    'For a day-by-day itinerary, stream verified sections in this order: summary, flights, hotels, then '
    'chronological days. After the required checks and activity research, call compose_trip with the '
    'first day and complete=false; extend the cumulative days list as each next day is ready. '
    'Use complete=true only with every trip day. For longer trips, use small cumulative batches within '
    'the existing model-call budget. Never describe an incomplete prefix as the finished itinerary. '
    'Disclose actual assumptions together, not as confirmed user facts. Unknown origin '
    'does not block destinations or seasons; ask departure airport alongside results, and omit flight-price '
    'comparisons until known. Never invent personal details, citizenship, eligibility or prices. '
    'Use sourced estimates separately from Company-checked inventory; Company inventory is not a market-wide '
    'price comparison. Every price needs currency, relevant dates, source and check time. Compare equal trip '
    'length, occupancy, cabin/baggage/connections, hotel quality/location and cancellation terms. Say lowest '
    'checked cost only for actual comparable checked prices, never claim exhaustive cheapest. '
    'Proposed destinations/windows are candidates, not finalized itineraries. Save grounded confirmed inputs, '
    'sourced candidates and selections with update_travel_plan before answering. Keep assumptions out of '
    'confirmed; cite exact original user quotes and IDs, not assistant suggestions. A short selection can '
    'select a saved candidate; arbitrary new selections need explicit user wording. Do not save provisional '
    'travel choices as permanent memory. Research first within existing budgets; never fill gaps with invention. '
    'Preparation: offer relevant gear, transfers, connectivity, documents, forms and appointments. Ask what '
    'they already own only when useful and alongside an initial checklist. Use existing shopping/browser '
    'tools; research entry rules from official sources. Execute forms or appointment tasks only when '
    'requested, preserving inline final approvals and manual secret entry. Travel discussion alone never '
    'authorizes purchases, bookings, submissions, watches, or reminders. Never report a preparation action '
    'completed without verified browser evidence. '
)


class TravelPlans:
    def __init__(self, db):
        self.db = db

    def load(self, user, session):
        owned = self.db.owned_session(user, session)
        row = self.db.one('SELECT * FROM travel_plans WHERE user_id=? AND session_id=?', (user, session))
        if not row or row['source_message_id'] <= owned['context_floor']:
            return None
        draft = json.loads(row['draft'])
        ids = draft.get('source_message_ids', [row['source_message_id']])
        if ids:
            excluded = self.db.rows('SELECT message_id FROM context_exclusions WHERE message_id IN ('+
                                    ','.join('?' for _ in ids)+')', ids)
            if excluded:
                return None
        # Defaults evolve without converting a provisional choice into user memory.
        for key in ('cabin','hotel_quality'):
            if key not in draft.get('confirmed',{}):draft.setdefault('assumptions',{})[key]=DEFAULTS[key]
        return {**draft, 'version': row['version']}

    def _source(self, user, session, mid, evidence=None):
        self.db.owned_session(user, session)
        source = next((m for m in self.db.visible_messages(user, session) if m['id'] == mid and m['role'] == 'user'), None)
        if not source or (evidence and evidence.casefold() not in source['content'].casefold()):
            raise ValueError('Use an exact quote from a visible user message in this chat.')
        return source

    def activate(self, user, session, mid, *, reset=False):
        self._source(user, session, mid)
        old = self.load(user, session)
        if old and not reset:
            return old
        draft = {'schema_version': 1, 'confirmed': {}, 'assumptions': dict(DEFAULTS),
                 'destinations': [], 'windows': [], 'selected': {}, 'preparation': [],
                 'source_message_ids': [mid], 'created_message_id': mid}
        self._save(user, session, mid, draft)
        return self.load(user, session)

    def _save(self, user, session, mid, draft):
        self.db.execute('''INSERT INTO travel_plans(user_id,session_id,source_message_id,version,draft,updated) VALUES(?,?,?,1,?,?)
            ON CONFLICT(user_id,session_id) DO UPDATE SET source_message_id=excluded.source_message_id,
            version=travel_plans.version+1,draft=excluded.draft,updated=excluded.updated''',
            (user, session, mid, json.dumps(draft), time.time()))

    def update(self, user, session, mid, patch, sources, today):
        self._source(user, session, mid)
        draft = self.load(user, session)
        if not draft:
            raise ValueError('Start a travel draft for this request first.')
        draft.pop('version', None)
        allowed_urls = {s['url'] for s in sources}
        changed = set()
        for item in patch.confirmed:
            self._source(user, session, item.source_message_id, item.evidence)
            if not grounded_detail(item):
                raise ValueError('Confirmed values must be stated in the quoted user evidence; keep other values provisional.')
            prior = draft['confirmed'].get(item.field)
            if prior and prior['source_message_id'] > item.source_message_id:
                continue
            if item.field in {'duration_days', 'travelers', 'rooms'} and (type(item.value) is not int or not 1 <= item.value <= 365):
                raise ValueError('Travel counts and duration must be positive integers.')
            if item.field in {'travelers','rooms'} and item.value > 20:
                raise ValueError('Traveler and room counts must be between 1 and 20.')
            if not prior or prior['value'] != item.value:
                changed.add(item.field)
            draft['confirmed'][item.field] = item.model_dump()
            draft['assumptions'].pop(item.field, None)
            draft['source_message_ids'].append(item.source_message_id)
        # A changed destination/date/quality must not retain incompatible old choices.
        if changed & {'destination', 'duration_days', 'start_date', 'end_date', 'season', 'horizon', 'budget', 'hotel_quality', 'cabin'}:
            draft['windows'] = []
            draft['selected'].pop('window_label', None)
        if 'destination' in changed:
            draft['destinations'] = []
            draft['selected'] = {}
        for field in ('destinations', 'windows'):
            items = getattr(patch, field)
            if items is None:
                continue
            if any(url not in allowed_urls for item in items for url in item.source_urls):
                raise ValueError('Candidate sources must come from verified research in this run.')
            if field == 'windows':
                days = draft['confirmed'].get('duration_days', {}).get('value', draft['assumptions'].get('duration_days', 7))
                horizon = today.replace(year=today.year+1) if (today.month, today.day) != (2, 29) else date(today.year+1, 2, 28)
                restricted = any(k in draft['confirmed'] for k in ('horizon', 'start_date', 'end_date'))
                if len({w.label for w in items}) != len(items):
                    raise ValueError('Use a different comparison label for each window.')
                for window in items:
                    if window.start_date < today or (window.end_date-window.start_date).days != days-1:
                        raise ValueError('Candidate windows must be future dates with the requested trip length (inclusive days).')
                    if not restricted and window.end_date > horizon:
                        raise ValueError('Without a supplied horizon, use the next twelve months.')
            replacements = [item.model_dump(mode='json') for item in items]
            if field == 'windows' and replacements != draft['windows']:
                draft['selected'].pop('window_label', None)
            if field == 'destinations' and draft['selected'].get('destination') not in {item.name for item in items}:
                draft['selected'].pop('destination', None)
                draft['selected'].pop('window_label', None)
            draft[field] = replacements
        if patch.selected:
            choice = patch.selected
            source = self._source(user, session, choice.source_message_id, choice.evidence)
            choosing = bool(re.search(r'\b(choose|pick|select|go with|option|first|second|third|yes|sounds good|you decide)\b', source['content'], re.I))
            named = bool(choice.destination and choice.destination.casefold() in source['content'].casefold())
            if not choosing and not named:
                raise ValueError('Save candidates first; selection requires a user choice or delegated choice.')
            if choice.destination and choice.destination not in {d['name'] for d in draft['destinations']} and choice.destination.casefold() not in source['content'].casefold():
                raise ValueError('Select a saved destination or one explicitly named by the user.')
            if choice.window_label and choice.window_label not in {w['label'] for w in draft['windows']}:
                raise ValueError('Select an existing candidate window.')
            draft['selected'].update(choice.model_dump(exclude_defaults=True))
            draft['source_message_ids'].append(choice.source_message_id)
        if patch.preparation is not None:
            # Completion is recorded only by the existing verified execution flow, not model assertion.
            if any(item.status == 'completed' for item in patch.preparation):
                raise ValueError('Keep preparation items suggested/requested; report verified execution separately.')
            draft['preparation'] = [item.model_dump() for item in patch.preparation]
        draft['source_message_ids'] = sorted(set(draft['source_message_ids'] + [mid]))
        self._save(user, session, draft['created_message_id'], draft)
        return self.load(user, session)

    def metrics(self, user, session, run, reply):
        event = self.db.one("SELECT payload FROM events WHERE run_id=? AND kind='travel_intent' ORDER BY id DESC LIMIT 1", (run,))
        if not event:
            return
        intent = json.loads(event['payload'])['intent']
        if intent == 'none':
            return
        saved = self.db.one("SELECT payload FROM events WHERE run_id=? AND kind='travel_draft_updated' ORDER BY id LIMIT 1", (run,))
        draft = self.load(user, session) or {}
        names = [item['name'] for item in draft.get('destinations', [])]
        names += [item['start_date'] for item in draft.get('windows', [])]
        options = bool(saved and any(name.casefold() in reply.casefold() for name in names))
        created = self.db.one('SELECT created FROM runs WHERE id=?', (run,))['created']
        self.db.event(run, 'travel_response_metrics', {'intent': intent, 'has_options': options,
            'clarification_only': bool('?' in reply and not options and len(reply.split()) < 60),
            'seconds_to_first_options': json.loads(saved['payload']).get('elapsed_seconds',round(time.time()-created, 3)) if options else None})
