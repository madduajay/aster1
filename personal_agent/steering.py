"""Durable same-chat instructions, applied between agent planning steps."""
import json
import time


class FollowupPending(Exception):
    """Yield a model boundary so Service can append the updated request."""


def pending(db, run):
    return bool(db.one('SELECT message_id FROM run_followups WHERE run_id=? AND applied_at IS NULL LIMIT 1', (run,)))


def request_messages(db, user, run):
    owned = db.one('SELECT * FROM runs WHERE id=? AND user_id=?', (run, user))
    if not owned:
        raise LookupError('Run not found')
    marker = db.one("SELECT payload FROM events WHERE run_id=? AND kind='request_message' ORDER BY id LIMIT 1", (run,))
    if marker:
        root = db.one("SELECT id,role,content FROM messages WHERE id=? AND session_id=? AND role='user'",
                      (json.loads(marker['payload'])['message_id'], owned['session_id']))
    else:
        root = db.one("SELECT id,role,content FROM messages WHERE session_id=? AND role='user' AND created<=? ORDER BY id DESC LIMIT 1",
                      (owned['session_id'], owned['created']))
    updates = db.rows("""SELECT m.id,m.role,m.content FROM run_followups f JOIN messages m ON m.id=f.message_id
        WHERE f.run_id=? AND m.session_id=? AND m.role='user' ORDER BY m.id""", (run, owned['session_id']))
    return ([root] if root else []) + updates


def prepare_context(db, user, run, context):
    messages = request_messages(db, user, run)
    if not messages:
        return context
    context = dict(context)
    context['current_user_message'] = messages[-1]
    context['active_request_messages'] = messages
    context['combined_user_request'] = '\n\n'.join(m['content'] for m in messages)
    owned=db.one('SELECT session_id,created FROM runs WHERE id=? AND user_id=?',(run,user))
    floor=db.owned_session(user,owned['session_id'])['context_floor']
    context['attached_files']=db.rows('''SELECT a.id,a.filename,a.media FROM message_attachments ma
        JOIN artifacts a ON a.id=ma.artifact_id JOIN messages m ON m.id=ma.message_id
        WHERE a.user_id=? AND m.session_id=? AND m.id>?
        AND m.id NOT IN (SELECT message_id FROM context_exclusions)
        ORDER BY m.id DESC,ma.rowid DESC LIMIT 10''',(user,owned['session_id'],floor))
    if 'completed_work' not in context:
        context['completed_work']=[{'answer':r['content']} for r in db.rows(
            "SELECT content FROM messages WHERE session_id=? AND role='assistant' AND created>=? ORDER BY id DESC LIMIT 5",
            (owned['session_id'],owned['created']))]
    if pending(db, run):
        usage = db.one('SELECT searches FROM run_usage WHERE run_id=?', (run,))
        context['research_start'] = usage['searches'] if usage else 0
        db.execute('UPDATE run_followups SET applied_at=? WHERE run_id=? AND applied_at IS NULL', (time.time(), run))
        db.event(run, 'followups_applied', {'message_ids': [m['id'] for m in messages[1:]]})
    return context


CONTINUATION = (
    'Combine the original active request and additional user messages into one goal. Latest corrections '
    'override conflicting details; keep compatible earlier requirements. Completed tool results remain '
    'evidence: do not redo fulfilled work, repeat purchases/bookings/submissions, or replay an uncertain '
    'action. Handle outstanding work and new additions. Inspect uncertain website outcomes before acting. '
    'Additional messages are instructions, not approval of any pending consequential action. '
    'Source IDs in active_request_messages are the real user messages; combined_user_request is only a recap.'
)
