"""Compose sourced trip days around Company-checked inventory only."""
import json
import re
from datetime import date,timedelta
from .contracts import canonical,describe,leaves,identity
from personal_agent.sources import canonical_url


def run_results(db,run):
    return [json.loads(r['payload']) for r in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='availability_result' ORDER BY id",(run,))]


def checked(results):
    unique={}
    for result in leaves(results):unique[result.get('query_id') or json.dumps(result,sort_keys=True)]=result
    return list(unique.values())


def linked_result(result):
    text=describe(result);sources=result.get('sources',[])
    for option in result.get('options',[]):
        name=option.get('name') or option.get('carrier')
        if name:
            label='['+name+']('+sources[0]['url']+')' if sources else name
            if option.get('photo_url'):label='![Venue photo]('+option['photo_url']+') '+label
            text=text.replace(name,label,1)
    descriptions=list(dict.fromkeys(o['description'] for o in result.get('options',[]) if o.get('description')))
    if descriptions:text+=' — '+'; '.join(descriptions)
    return text


def availability_text(groups,trip=False):
    results=[r for group in groups for r in group['results']]
    if trip:
        flat=checked(results)
        flight=next((o for r in flat if r['domain']=='flights' for o in r.get('options',[]) if o.get('arrival')),None)
        if flight:
            from .trips import required_meals
            results=[r for r in flat if r['domain']!='dining' or not r['criteria'].get('meal') or r['criteria']['meal'] in required_meals(r['criteria'].get('date',''),flight['arrival'],flight.get('return_departure',''))]
    return grouped_options(results)


def grouped_options(results):
    """One table per category, with repeated venue/date checks consolidated."""
    unique={}
    for result in checked(results):
        key=json.dumps([result['domain'],identity(result['domain'],result['criteria'])],sort_keys=True)
        unique[key]=result
    records=list(unique.values());sections=[]
    requested_counts={};default_targets={}
    for result in results:
        if result.get('requested_option_count'):requested_counts[result['domain']]=result['requested_option_count']
        elif result.get('option_target'):default_targets[result['domain']]=result['option_target']
    def cell(value):return str(value).replace('|',r'\|').replace('\n',' ')
    def named(name,result):
        sources=result.get('sources',[])
        return '['+name+']('+sources[0]['url']+')' if sources else name
    def pictured(option,label):
        if option.get('photo_url'):label='![Venue photo]('+option['photo_url']+') '+label
        if option.get('description'):label+=' — '+option['description'].replace('\n',' ')
        return label
    for domain in ('hotels','dining','flights'):
        group=[r for r in records if r['domain']==domain]
        if not group:continue
        completed=[r for r in group if r.get('status')=='completed'];rows={};notes=[]
        for result in group:
            c=result['criteria']
            if result.get('status')!='completed':
                known=identity(domain,{k:v for k,v in c.items() if k!='connections'})
                # A successful retry supersedes an earlier generic failure.
                if any(all(identity(domain,r['criteria']).get(k,r['criteria'].get(k))==v for k,v in known.items()) for r in completed):continue
                note=describe(result)
                if note not in notes:notes.append(note)
                continue
            if not result.get('available'):
                note=describe(result)
                if note not in notes:notes.append(note)
                continue
            for option in result.get('options',[]):
                name=option.get('name') or option.get('carrier')
                label=named(name,result)
                if domain=='dining':
                    if c.get('meal'):label+=' · '+c['meal'].title()
                    label=pictured(option,label)
                    details=', '.join(option['times']);day=c.get('date','')
                    key=(canonical(name),canonical(c.get('location')),c.get('party_size'),details)
                    row=rows.setdefault(key,[label,[],details]);
                    if day not in row[1]:row[1].append(day)
                elif domain=='hotels':
                    label=pictured(option,label)
                    stay=option['check_in']+' to '+option['check_out']
                    detail=(str(option['stars'])+'-star · ' if option.get('stars') else '')+option['room_type']
                    rows[(canonical(name),canonical(c.get('destination')),stay,c.get('guests'),c.get('rooms'))]=[label,stay,detail]
                else:
                    from .routes import connections,airline
                    if not airline(name):continue
                    stops=connections(option.get('connections',[]))
                    route=option['origin']+' → '+option['destination']+(' via '+' → '.join(stops) if stops else '')
                    detail=option['cabin']+' · '+option['departure']+('; return '+option['return_departure'] if option.get('return_departure') else '')
                    rows[(canonical(name),route,detail)]=[label+' '+option['flight_number'],route,detail]
        if domain in requested_counts and len(rows)<requested_counts[domain]:notes.append('I could verify '+str(len(rows))+' '+('hotel' if domain=='hotels' else 'flight')+' options in this check; you asked for '+str(requested_counts[domain])+'.')
        elif domain in default_targets and len(rows)<min(4,default_targets[domain]):notes.append('I could verify '+str(len(rows))+' '+('hotel' if domain=='hotels' else 'flight')+' options in this check.')
        body=[]
        if rows:
            columns={'hotels':['Hotel','Stay','Room'],'dining':['Venue','Dates','Available times'],'flights':['Flight','Route','Dates / cabin']}[domain]
            body=['| '+' | '.join(columns)+' |','| --- | --- | --- |']
            for row in rows.values():
                values=[', '.join(sorted(value)) if isinstance(value,list) else value for value in row]
                body.append('| '+' | '.join(cell(v) for v in values)+' |')
        if notes:body+=['','\n'.join('- '+note for note in notes)]
        if body:sections.append('\n'.join(body).strip())
    return '\n\n'.join(sections)


def compose(db,gateway,user,run,days,hotel_name='',flight_carrier='',dining_estimate=0,activities_estimate=0,*,complete=True):
    owned=db.one('SELECT session_id FROM runs WHERE id=? AND user_id=?',(run,user))
    if not owned:raise LookupError('Run not found')
    groups=run_results(db,run)
    all_results=[r for group in groups for r in group['results']]
    if not all_results:all_results=gateway.recent(user,owned['session_id'])
    records=checked(all_results)
    hotel=[r for r in records if r['domain']=='hotels' and r.get('available')]
    flights=[r for r in records if r['domain']=='flights' and r.get('available')]
    if not hotel or not flights:return {'status':'needs_checks','category':'travel','message':'Complete hotel and flight availability before composing the trip.'}
    selected=next((r for r in flights if not flight_carrier or any(canonical(o['carrier'])==canonical(flight_carrier) for o in r['options'])),flights[0])
    arrival=selected['options'][0].get('arrival','');returning=selected['options'][0].get('return_departure','')
    start=min(r['criteria'].get('check_in',r['criteria'].get('departure_date','9999')) for r in hotel+flights)
    end=max(r['criteria'].get('check_out',r['criteria'].get('return_date',r['criteria'].get('departure_date',''))) for r in hotel+flights)
    start=date.fromisoformat(start);end=date.fromisoformat(end)
    if not 0<=(end-start).days<=30:raise ValueError('Trip must span at most 31 days')
    expected=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    required=expected if complete else expected[:len(days)]
    if not days or [d.get('date') for d in days]!=required:return {'status':'needs_plan','message':'Provide exactly one ordered day for every date, starting at departure.','dates':expected}
    by_url={};actual_urls={};activity_urls=set();entities={};structured=False
    for row in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='research_evidence'",(run,)):
        evidence=json.loads(row['payload'])
        if 'attractions' in evidence:
            structured=True
            for item in evidence['attractions']:entities[canonical(item['name'])]=item
        for source in evidence.get('sources',[]):
            key=canonical_url(source['url']);actual_urls[key]=source['url'];by_url.setdefault(key,[]).append(evidence['answer']+' '+source['title'])
            activity_urls.add(key)
    for group in groups:
        for evidence in group.get('research',[]):
            for source in evidence.get('sources',[]):
                key=canonical_url(source['url']);actual_urls[key]=source['url'];by_url.setdefault(key,[]).append(evidence.get('answer','')+' '+source['title'])
    inventory_names={canonical(o.get('name') or o.get('carrier')) for r in records for o in r.get('options',[])}
    from .activities import concrete
    entities={k:e for k,e in entities.items() if concrete(e['name']) and k not in inventory_names}
    activity_visits={}
    from .trips import required_meals
    meal_plan=any(r['domain']=='dining' and r['criteria'].get('meal') for r in records)
    missing_dates=[];missing_meals=[];dining_choices={};missing_activities=[];validated=[]
    for day in days:
        transit=day.get('transit','none')
        if transit not in {'none','outbound','arrival','return'}:raise ValueError('Unknown travel-day type')
        if arrival and day['date']<arrival[:10]:transit='outbound'
        elif returning and day['date']==returning[:10]:transit='return'
        elif arrival and day['date']==arrival[:10]:transit='arrival'
        else:transit='none'
        dining=[r for r in records if r['domain']=='dining' and r['criteria'].get('date')==day['date'] and r.get('status')=='completed']
        choices=[o['name'] for r in dining if r.get('available') for o in r.get('options',[])]
        names=[] if meal_plan else day.get('dining',[])
        meals=[]
        if meal_plan:
            for meal in required_meals(day['date'],arrival,returning):
                match=next((r for r in reversed(dining) if r.get('available') and r['criteria'].get('meal')==meal),None)
                if match:meals.append(match)
                else:missing_meals.append({'date':day['date'],'meal':meal})
        if not meal_plan and not names and transit not in {'outbound','return'}:
            if choices:dining_choices[day['date']]=choices
            else:missing_dates.append(day['date'])
        for name in names:
            match=next((r for r in dining if canonical(name) in {canonical(r['criteria'].get('venue')),*(canonical(o.get('name')) for o in r.get('options',[]))}),None)
            if not match:
                if choices:dining_choices[day['date']]=choices
                else:missing_dates.append(day['date'])
                continue
            if not match.get('sources'):
                supporting=next((url for url,texts in by_url.items() if any(canonical(name) in canonical(t) for t in texts)),None)
                if supporting:match={**match,'sources':[{'url':actual_urls[supporting],'title':name}]}
            meals.append(match)
        activities=[]
        if structured:
            periods=[] if transit in {'outbound','return'} else ['morning','afternoon','evening']
            if arrival and day['date']==arrival[:10]:
                hour=int(arrival[11:13])+2 # Immigration, baggage and hotel transfer.
                periods=[p for p,h in [('morning',9),('afternoon',14),('evening',19)] if h>=hour]
            for period in periods:
                pool=[e for e in entities.values() if e.get('period')==period]
                if not pool:missing_activities.append(period+' activities for '+day['date']);continue
                chosen=min(pool,key=lambda e:activity_visits.get(canonical(e['name']),0))
                name=canonical(chosen['name']);activity_visits[name]=activity_visits.get(name,0)+1
                activities.append({k:chosen[k] for k in ('name','source_url','period')})
        elif transit!='outbound':
            # Backward compatibility for existing evidence; never mine bold
            # headings or URL slugs into invented attraction entities.
            for item in day.get('activities',[])[:3]:
                name=item.get('name','');url=item.get('source_url','');period=item.get('period','')
                if not concrete(name) or canonical(name) in inventory_names or not any(canonical(name) in canonical(text) for text in by_url.get(canonical_url(url),[])):
                    missing_activities.append(name or 'unnamed activity');continue
                if period not in {'','morning','afternoon','evening'}:continue
                if arrival and day['date']==arrival[:10] and int(arrival[11:13])+2>=16 and period!='evening':continue
                if transit=='return':continue
                activities.append({'name':name,'source_url':url,'period':period})
        late_arrival=arrival and day['date']==arrival[:10] and int(arrival[11:13])>=18
        if not activities and transit not in {'outbound','return'} and not late_arrival:missing_activities.append('Activities for '+day['date'])
        validated.append({'date':day['date'],'transit':transit,'dining':meals,'activities':activities})
    if missing_dates:return {'status':'needs_checks','category':'dining','dates':sorted(set(missing_dates)),'message':'Call delegate_dining for these dates and use its returned venue names.'}
    if missing_meals:return {'status':'needs_checks','category':'dining','meals':missing_meals,'message':'Check these breakfast/lunch/dinner meals using the known trip location, dates and party size. Do not ask the user to repeat the trip dates.'}
    if meal_plan:
        visits={};repeated=[]
        for day in validated:
            for meal in day['dining']:
                name=meal['options'][0]['name'];key=identity('dining',{'venue':name})['venue'];visits[key]=visits.get(key,0)+1
                if visits[key]>2:repeated.append({'date':day['date'],'meal':meal['criteria']['meal'],'venue':name})
        if repeated:return {'status':'needs_checks','category':'dining','meals':repeated,'avoid_venues':list({r['venue'] for r in repeated}),'message':'Replace these repeated venues. Prefer unique venues; a venue may appear at most twice in the displayed trip.'}
    if dining_choices:return {'status':'needs_plan','dining_choices':dining_choices,'message':'Dining is already checked for these dates. Correct the plan using these exact venue names; do not repeat availability checks.'}
    if missing_activities:
        return {'status':'needs_research','activities':missing_activities,'message':'Research these exact attractions on their official/tourism sites; use returned citations and exact names.'}
    def pick(records,name,key):
        if not name:return records[0]
        result=next((r for r in records if any(canonical(o.get(key))==canonical(name) for o in r['options'])),None)
        if result is None:raise ValueError('Selected option has not been checked')
        return result
    chosen_hotel=pick(hotel,hotel_name,'name');chosen_flight=pick(flights,flight_carrier,'carrier')
    party=chosen_hotel['criteria']['guests'];count=len(expected)
    if dining_estimate<0 or activities_estimate<0:raise ValueError('Budget estimates must be positive')
    from .money import budget,convert
    requested=None
    floor=db.owned_session(user,owned['session_id'])['context_floor']
    for row in db.rows("SELECT content FROM messages WHERE session_id=? AND role='user' AND id>? ORDER BY id DESC LIMIT 30",(owned['session_id'],floor)):
        requested=budget(row['content'])
        if requested:break
    requested=requested or {'limit':None,'currency':'USD'}
    budget_limit=requested['limit'];currency=requested['currency']
    costs={'flights':chosen_flight['options'][0].get('total_price'),'hotel':chosen_hotel['options'][0].get('total_price'),
        'dining_estimate':dining_estimate or 90*party*count,'activities_estimate':activities_estimate or 60*party*count,'transport_estimate':400}
    quotes=db.one("SELECT payload FROM events WHERE run_id=? AND kind='exchange_rates' ORDER BY id DESC LIMIT 1",(run,))
    quotes=json.loads(quotes['payload']) if quotes else {}
    source_currencies={'flights':chosen_flight['options'][0].get('currency','USD'),'hotel':chosen_hotel['options'][0].get('currency','USD')}
    try:costs={key:convert(value,source_currencies.get(key,'USD'),currency,quotes) if value is not None else None for key,value in costs.items()}
    except ValueError:return {'status':'needs_rates','message':'Obtain exchange rates before comparing different budget and provider currencies.'}
    total=sum(v for v in costs.values() if v is not None)
    if budget_limit and total>budget_limit:return {'status':'needs_plan','message':'The selection/allocation exceeds the user budget. Keep checked fares and hotel prices unchanged; revise only estimates/selections or explain the budget gap.','costs':costs,'total':total,'limit':budget_limit}
    result={'status':'completed' if complete else 'in_progress','meal_plan':meal_plan,'days':validated,'hotels':hotel,'flights':flights,'selected_hotel':chosen_hotel['query_id'],
        'selected_flight':chosen_flight['query_id'],'budget':{'limit':budget_limit,'costs':costs,'total':total,'currency':currency,'exchange':quotes if currency!='USD' else {}}}
    db.event(run,'trip_plan' if complete else 'trip_plan_progress',result)
    from .streaming import publish
    publish(db,user,run,plan=result,complete=complete)
    return {'status':'completed' if complete else 'in_progress','days':len(days),'budget_total':total,'budget_limit':budget_limit,
            'next_dates':expected[len(days):]}


def render_sections(plan):
    sections=[('flights',10,grouped_options(plan['flights'])),
              ('hotels',20,grouped_options(plan['hotels']))]
    lines=[]
    budget=plan['budget'];costs=budget['costs']
    from .money import display
    currency=budget['currency'];money=lambda amount:display(amount,currency)
    lines+=['','**Budget ('+currency+')**',f"Flights {money(costs['flights'])}; hotel {money(costs['hotel'])}; dining estimate {money(costs['dining_estimate'])}; activities estimate {money(costs['activities_estimate'])}; transport estimate {money(costs['transport_estimate'])}." if costs['flights'] is not None and costs['hotel'] is not None else 'Flight/hotel prices were not returned; a complete total cannot be confirmed.']
    lines.append('Indicative total: '+money(budget['total'])+(' of '+money(budget['limit'])+' budget.' if budget['limit'] else '.'))
    if budget.get('exchange'):lines.append('Converted using [ECB reference rates]('+budget['exchange']['source_url']+') dated '+budget['exchange']['date']+'.')
    if budget['limit'] and budget['total']>budget['limit']:lines.append('This selection exceeds the stated budget; adjust the selections or allocations.')
    if plan.get('status')!='in_progress':sections.append(('budget',1000,'\n\n'.join(line for line in lines if line)))
    for index,day in enumerate(plan['days'],1):
        parts=[]
        if day['transit']!='none':parts.append({'outbound':'Outbound travel','arrival':'Arrival day','return':'Return travel'}[day['transit']])
        parts.extend((a['period'].title()+': ' if a['period'] else '')+'['+a['name']+']('+a['source_url']+')' for a in day['activities'])
        title=f"**Day {index} · {day['date']}**"
        if plan.get('meal_plan') or any(r['criteria'].get('meal') for r in day['dining']):
            meals=[]
            for meal in ('breakfast','lunch','dinner'):
                match=next((r for r in day['dining'] if r['criteria'].get('meal')==meal),None)
                meals.append('- **'+meal.title()+':** '+(linked_result(match) if match else 'Outside the destination dining window (flight travel).'))
            entertainment=['- **Entertainment:** '+(' · '.join(parts) if parts else 'Travel day')]
            body=title+'\n'+'\n'.join(meals+entertainment)
        else:
            parts.extend('Dining: '+linked_result(r) for r in day['dining'])
            body='- '+title+': '+' · '.join(parts)
        sections.append(('day:'+day['date'],30+index,body))
    return sorted(sections,key=lambda s:s[1])


def render(plan):
    return '\n\n'.join(body for _,_,body in render_sections(plan))
