"""Deterministic proofs for explicit page-opening goals; other goals use Terra."""
import re
import json
from urllib.parse import urlsplit,parse_qsl

from .browser_intent import navigation_only_request,original_user_request
from .sources import allowed_url


def research_pages(observed,actions,domains,status_for):
    """Only pages actually observed during this task, never link targets alone."""
    pages={}
    def keep(state):
        # Reserve evidence for every distinct page before retaining UI variants.
        # Repeated ads/loading states on a later page cannot evict earlier pages.
        group=pages.setdefault(state['url'],{})
        selection=json.dumps(state.get('selections',[]),sort_keys=True)
        old=group.get(selection)
        if old is None or len(state['text'])>=len(old['text']):
            group[selection]={k:state.get(k,[] if k=='selections' else '') for k in ('url','title','text','selections')}
        if len(group)>3:
            # Keep the richest state plus recent distinct selected-control states.
            richest=max(group,key=lambda k:len(group[k]['text']))
            remove=next(k for k in group if k!=richest);group.pop(remove)
    for action in actions:
        if action['status'] not in {'completed','executed','verified'}:continue
        for key in ('before_state','after_state'):
            try:state=json.loads(action.get(key) or '{}')
            except (ValueError,TypeError):continue
            url=state.get('url','')
            if state.get('text') and allowed_url(url,domains) and (status_for(url) or 0)<400:
                keep(state)
    url=observed.get('url','')
    if observed.get('text') and allowed_url(url,domains) and (status_for(url) or 0)<400:
        keep(observed)
    result=[state for group in pages.values() for state in group.values()]
    budget=max(1200,min(18000,120000//max(1,len(result))))
    return [{**state,'text':state['text'][:budget]} for state in result]


def research_link_candidates(observed,actions,request,domains,pages,products=False):
    """Navigation hints from actual observations, excluding already-read pages."""
    links=[]
    for action in actions:
        if action['status'] not in {'completed','executed','verified'}:continue
        for key in ('before_state','after_state'):
            try:state=json.loads(action.get(key) or '{}')
            except (ValueError,TypeError):continue
            links.extend(state.get('links',[]))
    links.extend(observed.get('links',[]))
    from .sources import canonical_url
    visited={canonical_url(p['url']) for p in pages}
    candidates=relevant_read_links({**observed,'links':links},request,domains)
    if products:
        from availability.media import item_page
        candidates=list(dict.fromkeys(candidates+[u for u in links if allowed_url(u,domains) and item_page(u) and re.search(r'/(?:p|product|products|dp|ip)/',urlsplit(u).path,re.I)]))[:24]
    return [url for url in candidates if canonical_url(url) not in visited]


def relevant_read_links(observed,request,domains):
    from .browser_policy import URL_MUTATION
    ignored={'the','and','for','with','from','this','that','please','show','all','read','summarize','summarise','tell','page','website'}
    terms=list(dict.fromkeys(t.rstrip('s') for t in re.findall(r'[a-z]{3,}',request.lower()) if t not in ignored))
    candidates=[];seen=set()
    current=urlsplit(observed.get('url',''))
    for url in observed.get('links',[]):
        if not allowed_url(url,domains):continue
        p=urlsplit(url);identity=(p.netloc,p.path,p.fragment)
        if identity in seen or URL_MUTATION.search(p.path) or re.search(r'/(?:login|log-in|logout|privacy|legal|terms)(?:/|$)',p.path,re.I):continue
        target=p.fragment if p.fragment and (p.netloc,p.path)==(current.netloc,current.path) else p.path+' '+p.fragment
        words={t.rstrip('s') for t in re.findall(r'[a-z]{3,}',target.lower())}
        score=sum(i+1 for i,term in enumerate(terms) if term in words)
        if score:candidates.append((score,url));seen.add(identity)
    selected=[];size=0
    for _,url in sorted(candidates,key=lambda pair:-pair[0]):
        if size+len(url)<=2500:selected.append(url);size+=len(url)
        if len(selected)==12:break
    return selected


def navigation_evidence(db,user,run,observed,actions):
    source=original_user_request(db,user,run)
    if not navigation_only_request(source) or observed.get('human_required') or len(observed.get('text','').strip())<20:return None
    if any(a['category'] in {'approval','mutation'} and a['status'] in {'dispatching','executed','unknown','verified'} for a in actions):return None
    current=urlsplit(observed.get('url',''))
    for url in re.findall(r'https?://[^\s<>]+',source):
        target=urlsplit(url.rstrip('.,;!?)'))
        if (current.scheme,current.netloc,current.path.rstrip('/'))!=(target.scheme,target.netloc,target.path.rstrip('/')):continue
        if not set(parse_qsl(target.query)).issubset(set(parse_qsl(current.query))):continue
        return {'success':True,'method':'observed_page','reason':'The explicitly requested page is open and its content was observed.',
                'evidence':[observed['url'],observed['text'][:1800]]}
    return None
