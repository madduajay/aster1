"""Browser Use action boundary, independent of provider prompts and dependencies."""
import hashlib
import json
import re
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

READ_ACTIONS={'wait','switch','close','extract','read_page','search_page','find_elements','find_controls','inspect_selector','scroll','find_text','screenshot','dropdown_options','done','request_user'}
WRITE_ACTIONS={'click','click_selector','hover_selector','drag_selector','input','upload_file','select_dropdown','send_keys','navigate','go_back','request_domain'}
ALLOWED_ACTIONS=READ_ACTIONS|WRITE_ACTIONS
RISK=re.compile(r'\b(?:buy(?:\s+now)?|pay(?:ment)?|place\s+(?:order|reservation)|order\s+now|purchase|book(?:\s+now)?|reserve|delete|remove|send|submit|post|publish|confirm|subscribe|save|sign\s*(?:in|out|up)|log\s*(?:in|out)|cancel|accept|enroll|register|transfer|withdraw|redeem|donate|authorize|agree|activate|update|close\s+(?:account|card|subscription|membership)|open\s+(?:account|credit\s+line))\b',re.I)
CART_ADD=re.compile(r'^(?:add\s+to\s+(?:cart|bag|basket)|add\s+(?:item\s+)?to\s+(?:my\s+)?(?:cart|bag|basket))(?:\s*[($:].*)?$',re.I)
URL_MUTATION=re.compile(r'(?:^|[/=?&_-])(?:add|delete|remove|submit|send|publish|confirm|purchase|pay|cancel|logout|signout|subscribe)(?:$|[/=?&_-])',re.I)
UI_ONLY=re.compile(r'^(?:(?:open|close|toggle)\s+)?(?:menu|navigation|search|filters?|sort|details?|size\s+(?:guide|chart)|gallery|photos?|images?|zoom|next\s+(?:photo|image)|previous\s+(?:photo|image))$|^(?:show|view|hide)\s+(?:more|less|details|filters|options)$',re.I)
PREPARATION_CONTROL=re.compile(r'^(?:choose|select|show|view|expand|collapse|open|close|toggle|filter|sort|zoom)\b|^(?:more\s+)?(?:product\s+)?details$|^(?:next|previous)\s+(?:page|image|photo)$',re.I)
PRIVATE=re.compile(r'sk-[A-Za-z0-9_-]{12,}|\b(?:\d[ -]?){13,19}\b')
PUBLIC_ATTRIBUTES=frozenset({
    'href','src','alt','target','rel','download','action','method','form','formaction',
    'aria-label','aria-labelledby','aria-describedby','aria-description','aria-selected',
    'aria-checked','aria-disabled','aria-haspopup','aria-expanded','aria-controls','aria-current',
    'aria-modal','aria-hidden','aria-required','aria-invalid','title','name','type','id','class',
    'checked','selected','disabled','readonly','required','multiple','for','role','placeholder',
    'tabindex','autocomplete','inputmode','min','max','step','pattern','maxlength','minlength',
    'data-testid','data-test','data-qa','data-cy','data-state',
})
ATTRIBUTE_RESTRICTION='Some requested attributes are restricted. Retry using public element metadata; field values and private attributes are excluded.'
RESERVATION_ENTRY=re.compile(r'^(?:reserve\s+(?:a\s+)?table|book\s+(?:a\s+)?table|make\s+(?:a\s+)?reservation|reservations?|book\s+now|reserve\s+now)$',re.I)
RESERVATION_PATH=re.compile(r'(?:^|/)(?:reservations?|reserve|book|booking|venues)(?:/|$)',re.I)


@dataclass(frozen=True)
class BrowserPolicy:
    name:str
    targeted_handoffs:bool
    follow_providers:bool
    routine_controls:bool
    recovery_failures:int
    recovery_same:int
    max_steps:int
    model_calls:int
    task_seconds:int


POLICIES={
    'poc':BrowserPolicy('poc',True,True,True,12,3,45,60,600),
    'strict':BrowserPolicy('strict',False,False,False,6,2,30,40,240),
}


def browser_policy(name=None):
    value=name or os.environ.get('PERSONAL_AGENT_BROWSER_POLICY','poc')
    if value not in POLICIES:raise ValueError('Unknown browser policy')
    return POLICIES[value]


def policy_instructions(policy=None):
    p=browser_policy(policy)
    if p.name=='strict':return 'Browser policy: strict. Require explicit approval for provider transitions and consequential actions.'
    return ('Browser policy: PoC. Complete routine browsing, searches, form navigation, ordinary field preparation, option selection and requested cart additions without conversational permission. '
        'Color/size/date/time selection, applying filters, opening sign-in UI and ordinary Next/Continue steps do not need individual approvals. Prefer necessary-only or reject-optional choices on cookie banners when available. '
        'Judge the actual target and form, not a risky-sounding word alone or an unrelated field elsewhere on the page. '
        'Follow relevant observed public provider links/redirects; request_domain handles scope automatically. '
        'Ask only for genuinely missing information that blocks the current task. An empty field is not by itself a blocker. '
        'Final submissions/purchases/messages/destructive actions use the application approval flow; financial applications and transactions remain a direct user handoff. '
        'Passwords, payment secrets, OTPs and genuine human verification remain manual. Never repeat a possibly completed submission. '
        'Verification uncertainty is an unverified result, not proof that the requested option is unavailable.')


def manual_action_required(name,params,observed,policy=None):
    p=browser_policy(policy)
    if not p.targeted_handoffs:return bool(observed.get('manual_required'))
    if observed.get('human_required'):return True
    node=observed.get('node') or {}
    if re.search(r'i am not a robot|verify.{0,20}human|captcha',node.get('name',''),re.I) and name in {'click','click_selector','input','send_keys'}:return True
    if name=='input' and PRIVATE.search(params.get('text','')):return True
    if node.get('sensitive') and name not in READ_ACTIONS:return True
    form=node.get('form') or {}
    if form.get('sensitive'):
        if name=='send_keys' and params.get('keys','').casefold() in {'enter','space'}:return True
        if name in {'click','click_selector'} and (node.get('type') in {'submit','image'} or RISK.search(node.get('name',''))):return True
    if form.get('financial') and ((name in {'click','click_selector'} and (node.get('type') in {'submit','image'} or RISK.search(node.get('name','')))) or name=='send_keys' and params.get('keys','').casefold()=='enter'):return True
    # Older observations may not distinguish a field from a real challenge.
    return bool(observed.get('manual_required') and 'human_required' not in observed and not observed.get('fields'))


def sanitize_find_elements(params):
    """Keep discovery useful without asking the model to retry an attribute list."""
    attributes=params.get('attributes')
    if attributes is None:return dict(params)
    public=list(dict.fromkeys(a.strip().lower() for a in attributes if isinstance(a,str) and a.strip().lower() in PUBLIC_ATTRIBUTES))
    if attributes and not public:public=['href','id','role','aria-label']
    return {**params,'attributes':public}


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



def navigation_action(name, params, observed,policy=None):
    """Proven page navigation, independent of website names and action history.

    A button's label alone is insufficient. Require a safe link destination,
    a navigation landmark, a menu disclosure, a tab or native details control.
    Reservation entry links/dialogs may qualify from their observed destination
    or popup semantics. Submissions, transaction URLs and sensitive forms do not.
    """
    if name == 'go_back':
        return True
    if name == 'navigate':
        url = params.get('url', '')
        return urlsplit(url).scheme in {'http', 'https'} and not URL_MUTATION.search(action_url(url))
    node = observed.get('node') or {}
    if name == 'send_keys':
        if params.get('keys', '').casefold() not in {'enter', 'space'}:
            return False
    elif name not in {'click', 'click_selector'}:
        return False
    if node.get('sensitive') or node.get('disabled'):
        return False
    label, href = node.get('name', ''), node.get('href', '')
    if CART_ADD.fullmatch(label.strip()) or URL_MUTATION.search(action_url(href)):
        return False
    form = node.get('form')
    if form and (form.get('sensitive') or node.get('type') in {'submit', 'image'} or form.get('method', '').lower() != 'get'):
        return False
    restricted=bool(re.search(r'/(?:account|settings|profile|security|billing|payment|checkout)(?:/|$)',urlsplit(observed.get('url','')).path,re.I))
    if not restricted and not form and RESERVATION_ENTRY.fullmatch(label.strip()):
        if node.get('tag')=='A' and urlsplit(href).scheme in {'http','https'} and RESERVATION_PATH.search(urlsplit(href).path):return True
        if node.get('tag')=='BUTTON' and node.get('type')=='button' and node.get('has_popup')=='dialog':return True
    if RISK.search(label) and not (browser_policy(policy).routine_controls and node.get('tag')=='A' and re.match(r'^(?:view|see|learn|explore|compare|shop|open|sign\s*in|log\s*in|apply\s+now)\b',label,re.I)):return False
    if node.get('tag') == 'A' and href:
        return urlsplit(href).scheme in {'http', 'https'}
    # Keyboard activation of a textbox may submit a form, even inside a header.
    if node.get('tag') in {'INPUT', 'TEXTAREA', 'SELECT'} or node.get('editable'):
        return False
    role = node.get('role')
    if role == 'tab' or node.get('details_toggle'):
        return True
    if node.get('navigation_context') and role in {'button', 'menuitem', 'link'}:
        return True
    menu = node.get('has_popup') in {'true', 'menu', 'listbox'}
    disclosure = node.get('expanded') in {'true', 'false'} and node.get('controls_navigation')
    return bool((menu or disclosure) and role in {'button', 'menuitem', 'link'})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def routine_preparation(name,params,observed,inspect_only=False):
    """PoC UI preparation, excluding private forms and commitment controls."""
    node=observed.get('node') or {};form=node.get('form') or {}
    if node.get('sensitive') or form.get('sensitive') or form.get('financial'):return False
    if URL_MUTATION.search(action_url(node.get('href',''))) or URL_MUTATION.search(action_url(form.get('action',''))):return False
    label=node.get('name','').strip();kind=node.get('type','');role=node.get('role','')
    account=bool(re.search(r'/(?:account|settings|profile|security|billing|payment)(?:/|$)',urlsplit(observed.get('url','')).path,re.I))
    if CART_ADD.fullmatch(label) or re.search(r'\b(?:add to (?:wishlist|favorites)|freeze|unfreeze|lock|unlock)\b',label,re.I):return False
    if re.fullmatch(r'(?:execute|commit|finali[sz]e|finish|complete)(?:\s+(?:order|purchase|booking|reservation|application|checkout|payment))?',label,re.I):return False
    if name in {'click','click_selector'}:
        cookies=bool(re.search(r'\bcookies?\b',observed.get('text',''),re.I))
        if cookies and re.fullmatch(r'(?:reject(?: all)?|decline(?: all)?|accept(?: all| cookies| necessary cookies)?|necessary only|only necessary|continue without accepting)\s*[→›»]?',label,re.I):return True
        if re.fullmatch(r'(?:close[_ ]popup|close dialog|dismiss|room icon|room details|room options)',label,re.I):return True
        # Search and calendar controls are viewing operations even when the
        # website implements them using POST forms or non-button elements.
        if (node.get('search_context') or re.match(r'^(?:search|find)\b',label,re.I)) and not RISK.search(label):return True
        if re.fullmatch(r'(?:checkout|go to checkout|proceed to checkout|continue to checkout)',label,re.I):return not inspect_only
        months=r'jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?'
        if not account and not form and re.fullmatch(r'\d{1,2}\s+(?:'+months+r')\s+\d{1,2}',label,re.I):return True
        href=node.get('href','')
        if account and node.get('tag')=='A' and urlsplit(href).scheme in {'http','https'} and not RISK.search(label):return True
        if account and node.get('tag') in {'A','BUTTON'} and not form and re.fullmatch(r'(?:checking|savings)(?:\s+account)?[ .•·*\-]*\d{0,4}',label,re.I):return True
    if name=='send_keys' and params.get('keys','').casefold()=='enter' and node.get('search_context'):return True
    if name=='input' and not account:
        return kind=='search' or role=='searchbox' or node.get('search_context') or bool(re.search(r'\bsearch\b',label,re.I))
    if name in {'click','click_selector'}:
        # Opening sign-in UI is navigation; submitting credentials stays manual.
        if re.fullmatch(r'(?:sign\s*in|log\s*in|login)',label,re.I) and not form and kind not in {'submit','image'}:
            return not any(f.get('sensitive') or f.get('type')=='password' for f in (observed.get('fields') or []))
        filter_action=bool(re.fullmatch(r'(?:apply|clear|reset|remove)(?:\s+(?:all|selected))?\s+filters?',label,re.I))
        read_label=bool(re.fullmatch(r'(?:statements?|transactions?|activity|balances?|benefits?|rewards?|overview|details?)(?:\s+(?:history|overview|details|summary))?|(?:payment|billing)\s+history',label,re.I))
        if filter_action and kind=='button' and not account:return True
        if not inspect_only and re.fullmatch(r'next|continue|back|previous|save and continue|continue to (?:shipping|delivery|review|checkout)',label,re.I):
            return kind in {'submit','button'}
        if RISK.search(label):return False
        if kind=='button' or role in {'button','radio','checkbox','option','tab','combobox','listbox'} or node.get('tag')=='SELECT':
            if kind not in {'submit','image'} and (not account or read_label or UI_ONLY.fullmatch(label) or PREPARATION_CONTROL.search(label)):return True
    if name=='select_dropdown' and not account and not RISK.search(label):return True
    return False


def classify_action(name,params,observed,inspect_only=False,policy=None):
    profile=browser_policy(policy)
    if name not in ALLOWED_ACTIONS:return 'blocked','This action is disabled by the application.'
    node=observed.get('node') or {}
    if name=='request_user':return 'input',''
    if name=='screenshot' and params.get('file_name'):return 'blocked','Screenshot file writes are disabled; use the viewport image.'
    if name=='done' and params.get('files_to_display'):return 'blocked','Files are delivered only through the owned download API.'
    if name=='find_elements' and any(a not in PUBLIC_ATTRIBUTES for a in params.get('attributes') or []):
        return 'blocked',ATTRIBUTE_RESTRICTION
    if name in READ_ACTIONS:return 'read',''
    if manual_action_required(name,params,observed,profile.name):return 'blocked','This control requires private input or human verification; use request_user with kind=manual.'
    navigation = navigation_action(name, params, observed,profile.name)
    routine=profile.routine_controls and routine_preparation(name,params,observed,inspect_only)
    search=search_action(name,params,observed)
    if inspect_only and not navigation and not routine and not (search and profile.routine_controls):return 'blocked','This recovery task is inspection only.'
    if navigation:return 'read',''
    if routine or search:return 'preparation',''
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
        if params.get('keys','').casefold() in {'backspace','delete','space'} and (node.get('tag') in {'INPUT','TEXTAREA'} or node.get('editable')) and (not account or profile.routine_controls) and not node.get('sensitive'):return 'preparation',''
        if params.get('keys','').casefold() in {'enter','space'} and node.get('role') in {'button','link','checkbox','radio','option','tab','combobox','listbox'}:
            return classify_action('click', {}, observed, inspect_only,profile.name)
        return 'approval',''
    if name=='input' and (not account or profile.routine_controls):return 'preparation',''
    if name=='select_dropdown' and (not account or profile.routine_controls):return 'preparation',''
    if name=='hover_selector':return 'preparation',''
    if name in {'click','click_selector'}:
        label=node.get('name','').strip();href=node.get('href','')
        # Focusing a field never submits its form, including private manual fields
        # (manual_required above still prevents automated private interaction).
        if (node.get('tag') in {'INPUT','TEXTAREA'} and node.get('type') not in {'submit','button','image','checkbox','radio','file'}) or node.get('editable'):
            return 'preparation',''
        form=node.get('form')
        if profile.routine_controls and not URL_MUTATION.search(action_url(href)):
            if UI_ONLY.fullmatch(label) and node.get('type')=='button' and (not (form or {}).get('sensitive') or label.casefold() not in {'search','open search'}):return 'preparation',''
            if PREPARATION_CONTROL.search(label) and not RISK.search(label) and node.get('type')=='button':return 'preparation',''
            if label.casefold() in {'back','previous'} and node.get('type')=='button':return 'preparation',''
        form_submission=bool(form and (form.get('method','').lower() != 'get' or form.get('sensitive')))
        if UI_ONLY.fullmatch(label) and not form_submission and not URL_MUTATION.search(action_url(href)):
            return 'preparation',''
        if not account and CART_ADD.fullmatch(label) and not (form or {}).get('sensitive') and not re.search(r'/(?:checkout|payment)(?:/|$)', urlsplit(observed.get('url','')).path, re.I):
            return 'mutation',''  # Reversible cart preparation; still receipt/duplicate protected.
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
        'node':node,'destination_node':observed.get('destination_node'),'selections':observed.get('selections',[]),'prices':observed.get('prices',[]),
        **({'switch_target':observed.get('switch_target')} if name=='switch' else {})})


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
