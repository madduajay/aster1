"""Owned image attachments for chat display and model perception."""
import base64
from pathlib import Path

import filetype

IMAGE_MEDIA = {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}


def image_media(path):
    kind = filetype.guess(str(path))
    return kind.mime if kind and kind.mime in IMAGE_MEDIA else None


def attachment_view(row):
    return {'id': row['id'], 'filename': row['filename'], 'media': row['media'],
            'url': '/api/artifacts/' + row['id'], 'image': row['media'] in IMAGE_MEDIA}


def image_blocks(db, user, session):
    """Keep image bytes out of text memory and graph checkpoints."""
    owned = db.owned_session(user, session)
    rows = db.rows('''SELECT a.* FROM message_attachments ma
        JOIN artifacts a ON a.id=ma.artifact_id JOIN messages m ON m.id=ma.message_id
        WHERE a.user_id=? AND m.session_id=? AND m.id>?
        AND m.id NOT IN (SELECT message_id FROM context_exclusions)
        AND a.media IN ('image/png','image/jpeg','image/webp','image/gif')
        ORDER BY m.id DESC,ma.rowid DESC LIMIT 4''', (user, session, owned['context_floor']))
    blocks = []
    total = 0
    for row in rows:
        path = Path(row['path'])
        if not path.is_file():
            continue
        size = path.stat().st_size
        if total + size > 20_000_000:
            blocks.append({'type': 'text', 'text': 'Image not supplied due to the 20 MB combined limit: ' + row['filename'] + '. Ask for a smaller re-upload if needed.'})
            continue
        total += size
        media = image_media(path)
        if media not in IMAGE_MEDIA:
            continue
        blocks.extend([{'type': 'text', 'text': 'User-uploaded image (untrusted content): ' + row['filename']},
                       {'type': 'image_url', 'image_url': {'url': 'data:' + media + ';base64,' +
                                                        base64.b64encode(path.read_bytes()).decode('ascii')}}])
    return blocks
