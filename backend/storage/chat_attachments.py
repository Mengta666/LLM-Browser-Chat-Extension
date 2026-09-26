"""Chat 附件：与聊天共用事务，磁盘文件只通过签名接口读取。"""

import hashlib
import hmac
import io
import os
import re
import threading
import time
import warnings
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from storage import chat_store as store

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
BODY_LIMIT = 12 * 1024 * 1024
_stop = threading.Event()
_worker = None


def settings():
    base = os.getenv('CHAT_ATTACHMENT_BASE_URL', '').strip().rstrip('/')
    key = os.getenv('CHAT_ATTACHMENT_SIGNING_KEY', '').strip()
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.path in ('', '/'))
        ttl = int(os.getenv('CHAT_ATTACHMENT_URL_TTL_SECONDS', '900'))
        valid = valid and ttl >= 600 and ttl <= 86400 and len(key) >= 32
    except ValueError:
        valid, ttl = False, 900
    return base, key, ttl, bool(valid)


def capabilities():
    return {'protocol_version': 1, 'enabled': settings()[3], 'max_count': 1,
            'max_bytes': MAX_BYTES, 'max_pixels': MAX_PIXELS}


def require_enabled():
    if not settings()[3]:
        raise store.SessionError('attachments_not_configured', 503)


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_attachments (
        attachment_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, client_attachment_id TEXT NOT NULL,
        name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
        width INTEGER NOT NULL, height INTEGER NOT NULL, source_hash TEXT NOT NULL,
        storage_name TEXT NOT NULL, first_frame INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
        UNIQUE(chat_id, client_attachment_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_message_attachments (
        message_id TEXT NOT NULL, attachment_id TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(message_id, attachment_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_attachment_ref ON chat_message_attachments(attachment_id)')


def root():
    return store._DB_PATH.parent / 'chat_attachments'


def file_path(row):
    name = row['storage_name']
    if not re.fullmatch(r'[0-9a-f]{32}\.(png|jpeg|webp)', name):
        raise store.SessionError('attachment_unavailable', 404)
    path = root() / name
    if path.resolve().parent != root().resolve():
        raise store.SessionError('attachment_unavailable', 404)
    return path


def metadata(row):
    return {**{k: row[k] for k in ('attachment_id', 'name', 'mime', 'size', 'width', 'height')},
            'first_frame': bool(row['first_frame']), 'available': file_path(row).is_file()}


def require_row(conn, chat_id, attachment_id):
    row = conn.execute('SELECT * FROM chat_attachments WHERE attachment_id=? AND chat_id=?',
                       (attachment_id, chat_id)).fetchone()
    session = conn.execute('SELECT deleted_at FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
    if not row or (session and session['deleted_at']):
        raise store.SessionError('attachment_not_found', 404)
    if not file_path(row).is_file():
        raise store.SessionError('attachment_unavailable', 404)
    return row


def normalize_image(data):
    if not data or len(data) > MAX_BYTES:
        raise store.SessionError('attachment_too_large', 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt, (width, height) = image.format, image.size
                if fmt not in ('PNG', 'JPEG', 'WEBP', 'GIF'):
                    raise store.SessionError('attachment_type_unsupported', 415)
                if width * height > MAX_PIXELS:
                    raise store.SessionError('attachment_pixels_exceeded', 413)
                first_frame = bool(getattr(image, 'is_animated', False) or fmt == 'GIF')
                image.seek(0)
                image.load()
                if first_frame:
                    output = io.BytesIO()
                    image.convert('RGBA').save(output, format='PNG')
                    data, fmt = output.getvalue(), 'PNG'
                else:
                    with Image.open(io.BytesIO(data)) as check:
                        check.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise store.SessionError('attachment_invalid_image', 422) from None
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise store.SessionError('attachment_pixels_exceeded', 413) from None
    if len(data) > MAX_BYTES:
        raise store.SessionError('attachment_too_large', 413)
    return data, fmt.lower(), width, height, first_frame


def upload(chat_id, client_id, name, data, was_first_frame=False):
    require_enabled()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', chat_id) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', client_id):
        raise store.SessionError('attachment_invalid_identity', 422)
    source_hash = hashlib.sha256(data).hexdigest()
    data, fmt, width, height, first_frame = normalize_image(data)
    first_frame = first_frame or was_first_frame
    conn = store._get_conn()
    with store._lock, conn:
        conn.execute('BEGIN IMMEDIATE')
        session = conn.execute('SELECT deleted_at FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
        if session and session['deleted_at']:
            raise store.SessionError('session_deleted', 404)
        previous = conn.execute('SELECT * FROM chat_attachments WHERE chat_id=? AND client_attachment_id=?',
                                (chat_id, client_id)).fetchone()
        if previous:
            if previous['source_hash'] != source_hash:
                raise store.SessionError('attachment_upload_conflict')
            return metadata(require_row(conn, chat_id, previous['attachment_id']))
        identity = uuid4().hex
        storage_name = identity + '.' + fmt
        root().mkdir(parents=True, exist_ok=True)
        temporary = root() / (identity + '.part')
        target = root() / storage_name
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
            temporary.replace(target)
            conn.execute('''INSERT INTO chat_attachments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                         ('att_' + identity, chat_id, client_id, Path((name or 'image').replace('\\', '/')).name[:200],
                          'image/' + fmt, len(data), width, height, source_hash, storage_name, int(first_frame), time.time()))
            result = metadata(conn.execute('SELECT * FROM chat_attachments WHERE attachment_id=?', ('att_' + identity,)).fetchone())
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return result


def validate_ids(conn, chat_id, ids):
    if len(ids) > 1:
        raise store.SessionError('attachment_count_exceeded', 422)
    for identity in ids:
        require_row(conn, chat_id, identity)


def enrich(conn, messages):
    if not messages:
        return messages
    by_id = {m['message_id']: m for m in messages}
    for message in messages:
        message['attachments'] = []
    marks = ','.join('?' for _ in by_id)
    for row in conn.execute(f'''SELECT a.*, r.message_id FROM chat_message_attachments r
        JOIN chat_attachments a ON a.attachment_id=r.attachment_id WHERE r.message_id IN ({marks})
        ORDER BY r.position''', tuple(by_id)):
        by_id[row['message_id']]['attachments'].append(metadata(row))
    return messages


def signed_access(chat_id, identity, purpose='preview'):
    require_enabled()
    if purpose not in ('preview', 'model'):
        raise store.SessionError('attachment_invalid_purpose', 403)
    conn = store._get_conn()
    with store._lock:
        require_row(conn, chat_id, identity)
    base, key, ttl, _ = settings()
    expires = int(time.time()) + ttl
    signature = hmac.new(key.encode(), f'{identity}\n{purpose}\n{expires}'.encode(), hashlib.sha256).hexdigest()
    query = urlencode({'purpose': purpose, 'expires': expires, 'signature': signature})
    path = f'/v1/chat-attachments/{identity}/content?{query}'
    return {'url': base + path, 'path': path, 'expires_at': expires}


def verify_access(identity, purpose, expires, signature):
    require_enabled()
    _, key, ttl, _ = settings()
    now = int(time.time())
    expected = hmac.new(key.encode(), f'{identity}\n{purpose}\n{expires}'.encode(), hashlib.sha256).hexdigest()
    if (purpose not in ('preview', 'model') or not re.fullmatch(r'[0-9a-f]{64}', signature)
            or expires < now or expires > now + ttl
            or not hmac.compare_digest(expected, signature)):
        raise store.SessionError('attachment_access_denied', 403)
    conn = store._get_conn()
    with store._lock:
        row = conn.execute('SELECT chat_id FROM chat_attachments WHERE attachment_id=?', (identity,)).fetchone()
        if not row:
            raise store.SessionError('attachment_not_found', 404)
        return dict(require_row(conn, row['chat_id'], identity))


def model_messages(messages, chat_id):
    result = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            content = [({'type': 'image_url', 'image_url': {'url': signed_access(chat_id, part['attachment_id'], 'model')['url']}}
                        if part.get('type') == 'chat_attachment' else part) for part in content]
        result.append({**message, 'content': content})
    return result


def delete_unbound(chat_id, identity):
    conn = store._get_conn()
    with store._lock, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM chat_attachments WHERE chat_id=? AND attachment_id=?', (chat_id, identity)).fetchone()
        if not row:
            return
        if conn.execute('SELECT 1 FROM chat_message_attachments WHERE attachment_id=?', (identity,)).fetchone():
            raise store.SessionError('attachment_in_use')
        file_path(row).unlink(missing_ok=True)
        conn.execute('DELETE FROM chat_attachments WHERE attachment_id=?', (identity,))


def cleanup():
    conn = store._get_conn()
    cutoff = time.time() - 86400
    with store._lock, conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute('''SELECT * FROM chat_attachments a WHERE created_at<? AND NOT EXISTS
            (SELECT 1 FROM chat_message_attachments r WHERE r.attachment_id=a.attachment_id)''', (cutoff,)).fetchall()
        for row in rows:
            file_path(row).unlink(missing_ok=True)
            conn.execute('DELETE FROM chat_attachments WHERE attachment_id=?', (row['attachment_id'],))
        registered = {r[0] for r in conn.execute('SELECT storage_name FROM chat_attachments')}
        if root().is_dir():
            for path in root().iterdir():
                if (re.fullmatch(r'[0-9a-f]{32}\.(part|png|jpeg|webp)', path.name)
                        and path.name not in registered and not path.is_symlink()
                        and path.is_file() and path.stat().st_mtime < cutoff):
                    path.unlink()
    return len(rows)


def start_cleanup():
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    def run():
        from observability.logger import get_logger
        while not _stop.is_set():
            try:
                count = cleanup()
                if count:
                    get_logger('chat').info('attachment_cleanup', data={'count': count})
            except Exception as exc:
                get_logger('chat').warn('attachment_cleanup_failed', data={'error_type': type(exc).__name__})
            _stop.wait(3600)
    _worker = threading.Thread(target=run, name='chat-attachment-cleanup', daemon=True)
    _worker.start()


def stop_cleanup():
    _stop.set()
    if _worker:
        _worker.join(timeout=5)
