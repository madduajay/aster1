"""Plain-language approval presentation; opaque action IDs stay out of messages."""
import json
import re
from urllib.parse import urlsplit


def approval_text(approval):
    action=approval.get('action',{}) if isinstance(approval,dict) else {}
    operation=action.get('operation','')
    title=action.get('summary') or action.get('control') or 'Continue this browser action'
    if operation=='request_domain':title='Allow the browser to open another website'
    lines=['Approval needed',str(title)]
    url=action.get('destination') or action.get('url')
    if url:
        try:host=urlsplit(url).hostname
        except ValueError:host=None
        if host:lines.append('Website: '+host)
    task=action.get('task')
    if task:lines.append('Your request: '+str(task)[:2000])
    selected=[]
    for item in action.get('selected_options',[]) or []:
        if not isinstance(item,dict):continue
        name=str(item.get('name') or '').strip();value=str(item.get('value') or '').strip()
        if name or value:selected.append(name+(': '+value if value and value!=name else '') if name else value)
    if selected:lines.append('Selected: '+', '.join(dict.fromkeys(selected)))
    prices=action.get('displayed_prices',[]) or []
    if prices:lines.append('Displayed price: '+', '.join(dict.fromkeys(str(p) for p in prices)))
    if operation=='native_dialog' and action.get('message'):lines.append('Website confirmation: '+str(action['message']))
    lines.append('Choose Approve or Cancel below, or type approve or cancel.')
    return '\n'.join(lines)


def display_message(role,content):
    """Present old approval messages cleanly without rewriting saved history."""
    if role=='user':
        match=re.fullmatch(r'(approve|reject) [a-f0-9]{8}',content.strip(),re.I)
        if match:return 'Approve' if match[1].lower()=='approve' else 'Cancel'
    if role=='assistant' and content.startswith('Approval required:\n'):
        start=content.find('{')
        if start>=0:
            try:
                details,end=json.JSONDecoder().raw_decode(content[start:])
                trailer=content[start+end:].strip()
                if isinstance(details,dict) and re.fullmatch(r'Reply approve ([a-f0-9]{8}) or reject \1\.',trailer):
                    return approval_text({'action':details})
            except (ValueError,TypeError):pass
    return content
