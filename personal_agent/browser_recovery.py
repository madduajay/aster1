"""Website-independent observations, pause semantics and bounded recovery.

Contains no provider or browser dependency; shared by both Python runtimes.
"""
import hashlib
import json
import re
import time


# Trusted observation code only. Never evaluates code supplied by a website/model.
PAGE_OBSERVER = r"""(()=>{
const clean=x=>String(x||'').replace(/\s+/g,' ').trim();
const visible=x=>{const r=x.getBoundingClientRect(),s=getComputedStyle(x);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&!x.closest('[inert],[aria-hidden="true"]')};
const dialogs=Array.from(document.querySelectorAll('dialog[open],[role=dialog],[aria-modal=true]')).filter(visible);
const roots=dialogs.length?dialogs:Array.from(document.querySelectorAll('main,[role=main],article')).filter(visible);
const scope=roots.length?roots:[document.body];
const text=clean(scope.map(e=>e?.innerText||'').join(' ')).slice(0,18000);
const fields=Array.from(document.querySelectorAll('input,textarea,select')).filter(visible).map(e=>({
tag:e.tagName,type:e.type||'',autocomplete:e.autocomplete||'',name:clean(e.getAttribute('aria-label')||Array.from(e.labels||[]).map(l=>l.innerText).join(' ')||e.placeholder||e.name).slice(0,160),
required:!!e.required||e.getAttribute('aria-required')==='true',empty:!e.value,valid:(!e.validity||e.validity.valid)&&e.getAttribute('aria-invalid')!=='true',sensitive:e.matches('input[type=password],input[autocomplete^="cc-"],input[autocomplete="one-time-code"]')||/password|passcode|verification.?code|one.?time|\botp\b|\bcvv\b|\bcvc\b|card.?number/i.test([e.name,e.autocomplete,e.getAttribute('aria-label'),...Array.from(e.labels||[]).map(l=>l.innerText)].join(' '))})).slice(0,40);
const human=/(?:verify|confirm)\s+(?:that\s+)?you(?:\s+are|['’]re)\s+(?:a\s+)?human|complete the captcha|checking your browser|press\s*(?:&|and)\s*hold.{0,80}\b(?:human|bot)\b/i.test(text)||Array.from(document.querySelectorAll('iframe[src*="recaptcha"],iframe[src*="hcaptcha"]')).some(visible);
return {url:location.href,title:document.title,text,dialog:!!dialogs.length,fields,
headings:Array.from(document.querySelectorAll('h1,h2')).filter(visible).slice(0,10).map(e=>clean(e.innerText)),
selections:Array.from(document.querySelectorAll('input:checked,select,[aria-selected=true],[aria-checked=true],[aria-pressed=true],label[for]')).filter(e=>visible(e)&&(e.tagName!=='LABEL'||e.control?.checked)).map(e=>({name:e.getAttribute('aria-label')||e.name||clean(e.innerText).slice(0,150),value:e.control?.value||e.value||'',checked:e.control?.checked??e.checked??null})).slice(0,40),
links:Array.from(document.querySelectorAll('a[href]')).filter(visible).map(e=>e.href).slice(0,300),
prices:Array.from(new Set(text.match(/(?:\$|USD\s*)\s*\d[\d,.]*/g)||[])).slice(0,20),
manual_required:human||fields.some(f=>f.sensitive),human_required:human};
})()"""


def input_need(observed):
    """Only observed controls trigger an automatic handoff, never an agent claim."""
    fields=observed.get('fields',[])
    if observed.get('manual_required'):
        return {'kind':'manual','prompt':'The website needs you to complete login or human verification. Use the browser below, then choose Continue. Enter passwords and verification codes there, not in chat.'}
    if any(f.get('empty') and (f.get('type')=='tel' or re.search(r'phone|mobile',f.get('name',''),re.I)) for f in fields):
        return {'kind':'information','prompt':'What phone number should I use on this website? Include the country code. I will ask for approval before requesting a verification message.'}
    return None


def combine_observations(page,frames):
    """Prioritize a visible embedded dialog/form over the page behind it."""
    result=dict(page)
    frames=[f for f in frames if f.get('text') or f.get('fields')]
    result['frames']=[{k:f.get(k) for k in ('url','title','text','dialog','fields','manual_required')} for f in frames]
    relevant=[f for f in frames if f.get('dialog') or f.get('fields') or f.get('focused')]
    active=next((f for f in relevant if f.get('focused')),None) or next(iter(relevant),None)
    if active:
        result['background_text']=page.get('text','')[:6000]
        result['text']=active.get('text','')
        result['active_frame_url']=active.get('url')
        for k in ('fields','manual_required','human_required','dialog','prices'):
            result[k]=active.get(k,[] if k in {'fields','prices'} else False)
    else:
        result['text']=(page.get('text','')+' '+ ' '.join(f.get('text','') for f in frames))[:22000]
    return result


class RecoveryBudget:
    """Cap repeated failure and no-progress loops independently of model steps."""
    def __init__(self,max_failures=6,max_same=2,seconds=600):
        self.max_failures,self.max_same,self.seconds=max_failures,max_same,seconds
        self.started=time.monotonic();self.failures=0;self.attempts={};self.journal=[]

    def failure(self,operation,params,observed,phase):
        signature=hashlib.sha256(json.dumps([operation,params,observed.get('url'),observed.get('active_frame_url'),observed.get('text','')[:2500]],sort_keys=True).encode()).hexdigest()
        self.failures+=1;self.attempts[signature]=self.attempts.get(signature,0)+1
        self.journal.append({'operation':operation,'phase':phase,'attempt':self.attempts[signature]})
        return self.failures<=self.max_failures and self.attempts[signature]<=self.max_same and time.monotonic()-self.started<self.seconds


def input_text(action):
    return action.get('prompt','More information is needed to continue.')+ ('\nEnter it in the browser panel and choose Continue, or reply here. Type /stop to cancel.' if action.get('kind')!='manual' else '')
