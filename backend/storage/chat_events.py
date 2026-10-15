"""请求事件档案；游标仅在同一执行尝试内有效。"""

import json

from storage import chat_store as store


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_request_events (
        chat_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
        event_id INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(chat_id,request_id,attempt,event_id))''')


def head(chat_id, request_id):
    conn = store._get_conn()
    with store._lock:
        row = conn.execute('''SELECT t.* FROM chat_turns t JOIN chat_sessions s USING(chat_id)
            WHERE t.chat_id=? AND t.request_id=? AND s.deleted_at='' ''', (chat_id, request_id)).fetchone()
        return dict(row) if row else None


def append(chat_id, request_id, attempt, payload):
    encoded = json.dumps(payload, ensure_ascii=False)
    conn = store._get_conn()
    with store._lock, conn:
        row = head(chat_id, request_id)
        if not row or row['attempt'] != attempt or row['status'] in ('cancelled', 'interrupted'):
            raise store.SessionError('stale_attempt')
        seq = conn.execute('''SELECT COALESCE(MAX(event_id),0)+1 FROM chat_request_events
            WHERE chat_id=? AND request_id=? AND attempt=?''', (chat_id, request_id, attempt)).fetchone()[0]
        conn.execute('INSERT INTO chat_request_events VALUES(?,?,?,?,?,?)',
                     (chat_id, request_id, attempt, seq, encoded, store._now_iso()))
        return seq


def page(chat_id, request_id, attempt, after=0, limit=100):
    conn = store._get_conn()
    with store._lock:
        current = head(chat_id, request_id)
        if not current:
            raise store.SessionError('request_not_found', 404)
        if current['attempt'] != attempt:
            raise store.SessionError('request_attempt_changed')
        rows = conn.execute('''SELECT event_id,payload FROM chat_request_events
            WHERE chat_id=? AND request_id=? AND attempt=? AND event_id>? ORDER BY event_id LIMIT ?''',
                            (chat_id, request_id, attempt, after, limit)).fetchall()
        return current, [{'id': r['event_id'], 'data': json.loads(r['payload'])} for r in rows]
