"""Conservative, website-independent recognition of explicit page-opening requests.

This recognizes the user's requested operation only. The model still resolves
official domains and task details; there are no brand/URL mappings here.
"""
import re


_ACTION=re.compile(r"^(?:please\s+)?(?:(?:I\s+(?:want|need)|I['’]d\s+like|let['’]?s|new\s+(?:task|request))\b|(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?P<verb>open|visit|navigate\s+to|go\s+to|load|bring\s+up|take\s+me\s+to|show(?:\s+me)?)\s+(?P<target>.+)",re.I|re.S)
_PAGE=re.compile(r'\b(?:home\s*page|main\s*page|web\s*site|web\s*page|landing\s*page|site)\b',re.I)
_ADDRESS=re.compile(r'https?://\S+|www\.\S+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b',re.I)


def requests_page_open(text):
    text=text.strip()
    match=_ACTION.match(text)
    if not match:return False
    target=match['target']
    if re.match(r'(?:how\b|why\b|whether\b|if\b)',target,re.I):return False
    if re.search(r"\b(?:do\s+not|don't|never)\s+(?:open|visit|navigate|load|browse)\b",text,re.I):return False
    if match['verb'].lower().startswith('show') and re.search(r'\b(?:link|url|address|source\s+code|html)\b',target,re.I):return False
    return bool(_PAGE.search(target) or _ADDRESS.search(target))


# Short replies to an explicit information question stay bound to that question.
# Instructions/questions in the composer start a fresh run; no website names here.
_NEW_REQUEST = re.compile(r"^(?:/|(?:please\s+)?(?:(?:I\s+(?:want|need)|I['’]d\s+like|let['’]?s|new\s+(?:task|request))\b|(?:can|could|would|will)\s+you\b|(?:what|why|how|where|when|who|which)\b|(?:open|visit|navigate|go|load|show|find|search|look|book|reserve|buy|add|remove|delete|cancel|stop|start|switch|tell|help|compare|check|explain|summarize|write|do|don't|instead|actually|forget)\b))", re.I)


def starts_new_request(text, kind='information'):
    text = text.strip()
    if _NEW_REQUEST.search(text):
        return True
    # Treat other text as a bound answer. In a manual handoff it is rejected,
    # so a private code/password is never logged as a fresh model request.
    return False
