"""Persist verified itinerary sections for resumable, ordered SSE rendering."""
import json


def publish(db,user,run,plan=None,complete=False):
    owned=db.one('SELECT session_id FROM runs WHERE id=? AND user_id=?',(run,user))
    if not owned or not db.one("SELECT id FROM events WHERE run_id=? AND kind='trip_requested' LIMIT 1",(run,)):return
    from .presentation import run_results,checked,grouped_options,render_sections
    from .trips import active_scope
    from personal_agent.travel_planning import TravelPlans,planning_disclosure
    from personal_agent.steering import request_messages
    from personal_agent.sources import evidence_sources,vetted_text
    from personal_agent.public_errors import customer_text
    assumptions={}
    for row in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='travel_assumptions' ORDER BY id",(run,)):
        assumptions.update(json.loads(row['payload']))
    messages=request_messages(db,user,run)
    text='\n'.join(m['content'] for m in messages)
    summary=planning_disclosure(TravelPlans(db).load(user,owned['session_id']),assumptions,active_scope(db,user,run),text)
    sections=[('summary',0,summary)] if summary else []
    if plan:
        sections += [s for s in render_sections(plan) if complete or s[0]!='budget']
    else:
        records=checked([r for g in run_results(db,run) for r in g.get('results',[])])
        flights=[r for r in records if r['domain']=='flights']
        hotels=[r for r in records if r['domain']=='hotels']
        # Hold hotels until the flight check has an outcome, even if it finishes first.
        if flights:sections.append(('flights',10,grouped_options(flights)))
        if flights and hotels:sections.append(('hotels',20,grouped_options(hotels)))
    reset=db.one("SELECT COALESCE(MAX(id),0) n FROM events WHERE run_id=? AND kind='response_reset'",(run,))['n']
    prior={}
    for row in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='trip_section' AND id>? ORDER BY id",(run,reset)):
        item=json.loads(row['payload']);prior[item['key']]=item
    sources=evidence_sources(db,run)
    for key,order,body in sorted(sections,key=lambda s:s[1]):
        body=customer_text(vetted_text(body,sources,empty_fallback=False))
        if not body:continue
        item={'key':key,'order':order,'text':body}
        if prior.get(key)!=item:db.event(run,'trip_section',item)
