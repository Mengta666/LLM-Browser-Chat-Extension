"""原文位置、已发布历史目录与请求级工具账本。字符位置始终采用 Python 切片语义。"""

import hashlib
import json
import re

from storage import chat_store as store


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS history_blocks (
        block_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
        job_id TEXT NOT NULL DEFAULT '', batch_no INTEGER NOT NULL,
        overview TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER,
        UNIQUE(chat_id,job_id,batch_no))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS history_spans (
        block_id INTEGER NOT NULL, seq INTEGER NOT NULL, offset INTEGER NOT NULL,
        end INTEGER NOT NULL, PRIMARY KEY(block_id,seq,offset))''')
    conn.execute('CREATE INDEX IF NOT EXISTS history_blocks_chat ON history_blocks(chat_id,version)')
    conn.execute('''CREATE TABLE IF NOT EXISTS history_budgets (
        chat_id TEXT NOT NULL, request_id TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        calls INTEGER NOT NULL DEFAULT 0, ranges_json TEXT NOT NULL DEFAULT '[]',
        initial_text TEXT, PRIMARY KEY(chat_id,request_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_tool_runs (
        chat_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL,
        arguments TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL, step_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(chat_id,request_id,attempt,ordinal))''')


def archive_missing(conn, chat_id=None):
    params = (chat_id,) if chat_id else ()
    rows = conn.execute('''SELECT m.chat_id,m.seq,m.content FROM chat_messages m
        WHERE NOT EXISTS (SELECT 1 FROM history_blocks b JOIN history_spans p ON b.block_id=p.block_id
                          WHERE b.chat_id=m.chat_id AND p.seq=m.seq AND b.version IS NOT NULL)'''
        + (' AND m.chat_id=?' if chat_id else ''), params).fetchall()
    for row in rows:
        cur = conn.execute('''INSERT OR IGNORE INTO history_blocks
            (chat_id,job_id,batch_no,overview,kind,version) VALUES(?, '', ?, ?, 'archive', 0)''',
            (row['chat_id'], row['seq'], row['content'][:100].replace('\n', ' ')))
        if cur.rowcount:
            conn.execute('INSERT INTO history_spans VALUES(?,?,0,?)',
                         (cur.lastrowid, row['seq'], len(row['content'])))


def stage_batch(conn, job, spans, overview):
    cur = conn.execute('''INSERT INTO history_blocks(chat_id,job_id,batch_no,overview,kind,version)
        VALUES(?,?,?,?, 'compaction', NULL)''',
        (job['chat_id'], job['job_id'], job['next_batch'], overview))
    conn.executemany('INSERT INTO history_spans VALUES(?,?,?,?)',
                     [(cur.lastrowid, p['seq'], p['offset'], p['end']) for p in spans])


def publish(conn, job):
    blocks = conn.execute('SELECT block_id,batch_no FROM history_blocks WHERE chat_id=? AND job_id=? ORDER BY batch_no',
                          (job['chat_id'], job['job_id'])).fetchall()
    if len(blocks) != len(job['plan']):
        raise store.SessionError('compaction_coverage_incomplete')
    for index, block in enumerate(blocks):
        spans = [dict(r) for r in conn.execute('SELECT seq,offset,end FROM history_spans WHERE block_id=? ORDER BY seq,offset', (block['block_id'],))]
        if block['batch_no'] != index or spans != job['plan'][index]:
            raise store.SessionError('compaction_coverage_incomplete')
    expected = conn.execute('''SELECT m.seq,m.content FROM chat_messages m LEFT JOIN chat_turns t
        ON t.chat_id=m.chat_id AND t.request_id=m.request_id
        WHERE m.chat_id=? AND m.seq>? AND m.seq<=? AND (m.request_id='' OR t.status IN ('completed','partial')) ORDER BY m.seq''',
        (job['chat_id'], job['base_upto_seq'], job['through_seq'])).fetchall()
    coverage = {}
    for batch in job['plan']:
        for span in batch:
            previous = coverage.get(span['seq'], 0)
            if span['offset'] != previous or span['end'] < previous:
                raise store.SessionError('compaction_coverage_incomplete')
            coverage[span['seq']] = span['end']
    if any(coverage.get(m['seq']) != len(m['content']) for m in expected):
        raise store.SessionError('compaction_coverage_incomplete')
    conn.execute('UPDATE history_blocks SET version=? WHERE chat_id=? AND job_id=? AND version IS NULL',
                 (job['base_version'] + 1, job['chat_id'], job['job_id']))


def records(chat_id, upto_seq, request_id=''):
    conn = store._get_conn()
    with store._lock:
        return [dict(r) for r in conn.execute('''SELECT m.*,COALESCE(t.status,'completed') AS status
            FROM chat_messages m JOIN chat_sessions s ON s.chat_id=m.chat_id
            LEFT JOIN chat_turns t ON t.chat_id=m.chat_id AND t.request_id=m.request_id
            WHERE m.chat_id=? AND m.seq<=? AND s.deleted_at='' AND (
                m.request_id='' OR t.status IN ('completed','partial') OR
                (m.request_id=? AND t.status='running' AND m.role='user' AND m.seq=t.user_seq
                 AND s.active_request_id=t.request_id)) ORDER BY m.seq''', (chat_id, upto_seq, request_id))]


def list_blocks(chat_id, upto_seq, request_id='', *, block_ref=None, cursor=0, limit=20):
    rows = {r['seq']: r for r in records(chat_id, upto_seq, request_id)}
    conn = store._get_conn()
    with store._lock, conn:
        archive_missing(conn, chat_id)
        blocks = conn.execute('''SELECT * FROM history_blocks WHERE chat_id=? AND version IS NOT NULL
            AND block_id>? ORDER BY block_id''', (chat_id, 0 if block_ref else cursor)).fetchall()
        result = []
        for block in blocks:
            if block_ref and block['block_id'] != int(block_ref[1:]):
                continue
            spans = [dict(r) for r in conn.execute('SELECT seq,offset,end FROM history_spans WHERE block_id=? ORDER BY seq,offset', (block['block_id'],)) if r['seq'] in rows]
            if not spans:
                continue
            if not block_ref and block['kind'] == 'archive' and all(conn.execute('''SELECT 1 FROM history_spans p
                JOIN history_blocks b ON b.block_id=p.block_id WHERE b.chat_id=? AND b.kind='compaction'
                AND b.version IS NOT NULL AND p.seq=? LIMIT 1''', (chat_id, p['seq'])).fetchone() for p in spans):
                continue
            entry = {'block_ref': f'B{block["block_id"]}', 'kind': block['kind'], 'overview': block['overview'],
                     'first_ref': f'M{spans[0]["seq"]}', 'last_ref': f'M{spans[-1]["seq"]}', 'version': block['version']}
            if block_ref:
                entry['messages'] = [dict(source_ref=f'M{p["seq"]}', role=rows[p['seq']]['role'], status=rows[p['seq']]['status'],
                    content_length=len(rows[p['seq']]['content']), offset=p['offset'], end=p['end'],
                    read_args={'source_ref': f'M{p["seq"]}', 'offset': p['offset'], 'end': p['end']}) for p in spans if p['seq'] > cursor][:limit]
                remaining = [p for p in spans if p['seq'] > cursor]
                entry['next'] = {'block_ref': block_ref, 'cursor': int(entry['messages'][-1]['source_ref'][1:])} if len(remaining) > limit else None
            result.append(entry)
            if len(result) > limit:
                break
    return result[:limit], len(result) > limit


def query_records(chat_id, upto_seq, request_id='', *, query='', block_ref=None, source_ref=None):
    if source_ref and block_ref:
        raise ValueError('source_ref 和 block_ref 不能同时指定')
    rows = records(chat_id, upto_seq, request_id)
    if source_ref:
        rows = [r for r in rows if r['seq'] == int(source_ref[1:])]
    spans = {}
    if block_ref:
        blocks, _ = list_blocks(chat_id, upto_seq, request_id, block_ref=block_ref, limit=100000)
        spans = {int(m['source_ref'][1:]): (m['offset'], m['end']) for b in blocks for m in b.get('messages', [])}
        rows = [r for r in rows if r['seq'] in spans]
    explicit = {int(n) for n in re.findall(r'\bM([1-9][0-9]*)\b', query)}
    quoted = [a or b or c for a, b, c in re.findall(r'"([^"]+)"|“([^”]+)”|「([^」]+)」', query)]
    identifiers = re.findall(r'(?<!\w)[\w]+(?:[-_./:][\w]+)+(?!\w)', query)
    terms = list(dict.fromkeys(quoted + identifiers + query.split()))
    ranked = []
    for row in rows:
        base, stop = spans.get(row['seq'], (0, len(row['content'])))
        fragment = row['content'][base:stop]
        text = fragment.casefold()
        exact = row['seq'] in explicit
        phrases = sum(t.casefold() in text for t in quoted)
        ids = sum(bool(re.search(r'(?<!\w)' + re.escape(t) + r'(?!\w)', fragment, re.I)) for t in identifiers)
        hits = sum(t.casefold() in text for t in terms)
        if exact or phrases or ids or hits:
            start, preview = store._history_excerpt(fragment, quoted + identifiers or terms, size=180)
            matches = [(term, match) for term in quoted + identifiers or terms
                       for match in re.finditer(re.escape(term), fragment, re.I)
                       if start <= match.start() < start + len(preview)]
            focus = max(matches, key=lambda pair: (pair[0] in quoted, pair[0] in identifiers, len(pair[0])), default=None)
            at, end = (focus[1].start(), focus[1].end()) if focus else (start, start)
            start = max(0, at - 32)
            preview = fragment[start:max(start + 180, end + 32)]
            ranked.append(((exact, phrases, ids, hits, row['seq']), {**row, 'content_offset': base + start,
                           'match_offset': base + at, 'match_end': base + end, 'preview': preview}))
    return [row for _, row in sorted(ranked, key=lambda pair: pair[0], reverse=True)]


def load_budget(chat_id, request_id):
    conn = store._get_conn()
    with store._lock:
        row = conn.execute('SELECT * FROM history_budgets WHERE chat_id=? AND request_id=?', (chat_id, request_id)).fetchone()
        return dict(row) if row else None


def save_budget(conn, chat_id, request_id, used, calls, ranges, initial):
    conn.execute('''INSERT INTO history_budgets VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id,request_id)
        DO UPDATE SET used=excluded.used,calls=excluded.calls,ranges_json=excluded.ranges_json,initial_text=excluded.initial_text''',
        (chat_id, request_id, used, calls, json.dumps(ranges), initial))


def tool_runs(chat_id, request_id):
    conn = store._get_conn()
    with store._lock:
        return [dict(r) for r in conn.execute('SELECT * FROM chat_tool_runs WHERE chat_id=? AND request_id=? ORDER BY attempt,ordinal', (chat_id, request_id))]


def save_tool(chat_id, request_id, attempt, ordinal, call_id, name, arguments, result='', status='running', step=None, *, budget=None):
    if not request_id:
        return
    conn = store._get_conn()
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        parsed = {'invalid_arguments_hash': fingerprint(str(arguments))}
    def redact(value):
        if isinstance(value, dict):
            return {k: '[REDACTED]' if re.search(r'(?i)(?:password|secret|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|cookie)', k)
                    else redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value
    arguments = json.dumps(redact(parsed), ensure_ascii=False)
    with store._lock, conn:
        store.ensure_turn_active(chat_id, request_id, attempt)
        conn.execute('''INSERT INTO chat_tool_runs VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,request_id,attempt,ordinal) DO UPDATE SET
            result=excluded.result,status=excluded.status,step_json=excluded.step_json''',
            (chat_id, request_id, attempt, ordinal, call_id, name, arguments, result, status, json.dumps(step or {}, ensure_ascii=False)))
        if budget:
            save_budget(conn, chat_id, request_id, budget.used, budget.calls, budget.read_ranges, budget.initial_text)


def fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
