"""Browser Use action boundary, independent of provider prompts and dependencies."""
import hashlib
import json
import re
from urllib.parse import urlsplit

READ_ACTIONS={'wait','switch','close','extract','search_page','find_elements','find_controls','inspect_selector','scroll','find_text','screenshot','dropdown_options','done','request_user'}
WRITE_ACTIONS={'click','click_selector','hover_selector','drag_selector','input','upload_file','select_dropdown','send_keys','navigate','go_back','request_domain'}
ALLOWED_ACTIONS=READ_ACTIONS|WRITE_ACTIONS
RISK=re.compile(r'\b(?:buy(?:\s+now)?|pay(?:ment)?|place\s+(?:order|reservation)|order\s+now|purchase|book(?:\s+now)?|reserve|delete|remove|send|submit|post|publish|confirm|subscribe|save|sign\s*(?:in|out|up)|log\s*(?:in|out)|cancel|accept|enroll|register|transfer|withdraw|redeem|donate|authorize|agree|activate|update)\b',re.I)
CART_ADD=re.compile(r'^(?:add\s+to\s+(?:cart|bag|basket)|add\s+(?:item\s+)?to\s+(?:my\s+)?(?:cart|bag|basket))(?:\s*[($:].*)?$',re.I)
URL_MUTATION=re.compile(r'(?:^|[/=?&_-])(?:add|delete|remove|submit|send|publish|confirm|purchase|pay|cancel|logout|signout|subscribe)(?:$|[/=?&_-])',re.I)
UI_ONLY=re.compile(r'^(?:(?:open|close|toggle)\s+)?(?:menu|navigation|search|filters?|sort|details?|size\s+(?:guide|chart)|gallery|photos?|images?|zoom|next\s+(?:photo|image)|previous\s+(?:photo|image))$|^(?:show|view|hide)\s+(?:more|less|details|filters|options)$',re.I)
PREPARATION_CONTROL=re.compile(r'^(?:choose|select|show|view|expand|collapse|open|close|toggle|filter|sort|zoom)\b|^(?:more\s+)?(?:product\s+)?details$|^(?:next|previous)\s+(?:page|image|photo)$',re.I)
PRIVATE=re.compile(r'sk-[A-Za-z0-9_-]{12,}|\b(?:\d[ -]?){13,19}\b')


def action_url(url):
    # Hostnames such as booking.example describe a website, not an action.
    p=urlsplit(url)
    return p.path+'?'+p.query+'#'+p.fragment


def search_action(name,params,observed):
    """Recognize search from live control/form semantics, never brand mappings."""
    node=observed.get('node') or {};form=node.get('form')
    path=urlsplit(observed.get('url','')).path
    if node.get('sensitive') or re.search(r'/(?:account|settings|profile|security|billing|payment|checkout)(?:/|$)',path,re.I):return False
    if form and (form.get('method','').lower()!='get' or form.get('sensitive') or RISK.search(urlsplit(form.get('action','')).path)):return False
    field=node.get('type')=='search' or node.get('role')=='searchbox'
    context=node.get('search_context') or node.get('tag')=='SEARCH'
    if name=='send_keys':return field and params.get('keys','').casefold()=='enter'
    if name in {'click','click_selector'}:
        label=node.get('name','').strip()
        if context and (re.match(r'^(?:search\b|open search\b)',label,re.I) or label.casefold() in {'cancel','close','close search','cancel search'}):return True
        return node.get('type')=='button' and 'form' in node and not form and label.casefold() in {'search','open search'}
    return False


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def classify_action(name,params,observed,inspect_only=False):
    if name not in ALLOWED_ACTIONS:return 'blocked','This action is disabled by the application.'
    node=observed.get('node') or {}
    if name=='request_user':return 'input',''
    if name=='screenshot' and params.get('file_name'):return 'blocked','Screenshot file writes are disabled; use the viewport image.'
    if name=='done' and params.get('files_to_display'):return 'blocked','Files are delivered only through the owned download API.'
    if name=='find_elements' and any(a not in {'href','src','aria-label','aria-labelledby','aria-selected','aria-checked','aria-disabled','title','name','type','id','class','checked','selected','disabled','for','role'} for a in params.get('attributes',[])):
        return 'blocked','Only public control attributes may be extracted; private field values are excluded.'
    if name in READ_ACTIONS:return 'read',''
    if observed.get('manual_required'):return 'blocked','Manual login or human verification is required; use request_user with kind=manual.'
    if inspect_only and name not in {'navigate','go_back'}:return 'blocked','This recovery task is inspection only.'
    if name=='input' and (node.get('sensitive') or PRIVATE.search(params.get('text',''))):
        return 'blocked','Enter passwords, payment credentials, and secrets manually.'
    account=bool(re.search(r'/(?:account|settings|profile|security|billing|payment)(?:/|$)',urlsplit(observed.get('url','')).path,re.I))
    if name=='navigate':
        destination=action_url(params.get('url',''))
        if URL_MUTATION.search(destination):
            if inspect_only:return 'blocked','Inspection cannot navigate to a transaction URL.'
            return 'approval',''
        # Viewing an application, cart or checkout page does not submit it.
        return 'read',''
    if name=='request_domain':return 'scope',''
    if name=='go_back':return 'read',''
    if search_action(name,params,observed):return 'preparation',''
    if name=='send_keys':
        if params.get('keys') in {'TAB','Tab','ESC','Escape','ARROWUP','ARROWDOWN','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','HOME','END','PageUp','PageDown'}:
            return 'preparation',''
        if params.get('keys') not in {'Enter','ENTER','Space','SPACE','Backspace','BACKSPACE','Delete','DELETE'}:
            return 'blocked','Only explicit navigation/editing keys are supported.'
        if params.get('keys','').casefold() in {'backspace','delete'} and (node.get('tag') in {'INPUT','TEXTAREA'} or node.get('editable')) and not account and not node.get('sensitive'):return 'preparation',''
        if params.get('keys','').casefold() in {'enter','space'} and node.get('role') in {'button','link','checkbox','radio','option','tab','combobox','listbox'}:
            return classify_action('click', {}, observed, inspect_only)
        return 'approval',''
    if name=='input' and not account:return 'preparation',''
    if name=='select_dropdown' and not account:return 'preparation',''
    if name=='hover_selector':return 'preparation',''
    if name in {'click','click_selector'}:
        label=node.get('name','').strip();href=node.get('href','')
        # Focusing a field never submits its form, including private manual fields
        # (manual_required above still prevents automated private interaction).
        if (node.get('tag') in {'INPUT','TEXTAREA'} and node.get('type') not in {'submit','button','image','checkbox','radio','file'}) or node.get('editable'):
            return 'preparation',''
        form=node.get('form')
        form_submission=bool(form and (form.get('method','').lower() != 'get' or form.get('sensitive')))
        if UI_ONLY.fullmatch(label) and not form_submission and not URL_MUTATION.search(action_url(href)):
            return 'preparation',''
        if not account and CART_ADD.fullmatch(label) and not (form or {}).get('sensitive') and not re.search(r'/(?:checkout|payment)(?:/|$)', urlsplit(observed.get('url','')).path, re.I):
            return 'mutation',''  # Reversible cart preparation; still receipt/duplicate protected.
        if node.get('tag')=='A' and href and not RISK.search(label) and not URL_MUTATION.search(action_url(href)):
            return 'read',''  # Link navigation is not the eventual form submission.
        if not account and not RISK.search(label) and not URL_MUTATION.search(action_url(href)):
            if node.get('role') in {'radio','checkbox','option','tab','combobox','listbox','slider','spinbutton'} or node.get('tag')=='SELECT':return 'preparation',''
            form=node.get('form')
            restricted=bool(re.search(r'/(?:checkout|payment|billing|apply|application)(?:/|$)',urlsplit(observed.get('url','')).path,re.I))
            if PREPARATION_CONTROL.search(label) and (node.get('type')=='button' or node.get('type')=='submit' and not form) and not restricted and (not form or form.get('method','').lower()=='get' and not form.get('sensitive')):
                return 'preparation',''
    return 'approval',''


def action_binding(name,params,observed):
    node=observed.get('node') or {}
    return fingerprint({'action':name,'parameters':params,'url':observed.get('url'),
        'node':node,'destination_node':observed.get('destination_node'),'selections':observed.get('selections',[]),'prices':observed.get('prices',[])})


def duplicate_key(name,params,observed):
    node=observed.get('node') or {}
    return fingerprint({'action':name,'parameters':{k:v for k,v in params.items() if k not in {'index'}},
        'url':observed.get('url'),'node':{k:node.get(k) for k in ('tag','name','role','type','href','id','name_attr','form')},
        'field_value':node.get('value') if name=='send_keys' else None,
        'selections':observed.get('selections',[]),'prices':observed.get('prices',[])})


def legacy_duplicate_key(name,params,observed):
    node=observed.get('node') or {}
    return fingerprint({'action':name,'parameters':{k:v for k,v in params.items() if k!='index'},
        'url':observed.get('url'),'node':{k:node.get(k) for k in ('tag','name','role','type','href','id','name_attr')}})
