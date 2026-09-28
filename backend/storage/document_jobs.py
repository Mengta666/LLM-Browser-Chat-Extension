"""全文只读分析：不可变原文指纹、分片结果、汇总前沿和请求级调用账本。"""

from uuid import uuid4

from storage import chat_store as store, history_index as history


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS document_jobs (
        job_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, source_seq INTEGER NOT NULL,
        source_hash TEXT NOT NULL, source_length INTEGER NOT NULL, task TEXT NOT NULL,
        fingerprint TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
        request_id TEXT NOT NULL, attempt INTEGER NOT NULL, next_offset INTEGER NOT NULL DEFAULT 0,
        error_code TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS document_executions (
        chat_id TEXT NOT NULL, request_id TEXT NOT NULL, job_id TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,request_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS document_nodes (
        node_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, kind TEXT NOT NULL,
        offset INTEGER NOT NULL, end INTEGER NOT NULL, content TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL, elapsed_ms INTEGER NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS document_nodes_job ON document_nodes(job_id,active,offset)')


def get(job_id, chat_id):
    conn = store._get_conn()
    with store._lock:
        row = conn.execute('SELECT * FROM document_jobs WHERE job_id=? AND chat_id=?', (job_id, chat_id)).fetchone()
        return dict(row) if row else None


def progress(job):
    conn = store._get_conn()
    with store._lock:
        calls = conn.execute('SELECT calls FROM document_executions WHERE chat_id=? AND request_id=?',
                             (job['chat_id'], job['request_id'])).fetchone()
    return {k: job[k] for k in ('job_id', 'status', 'error_code', 'request_id')} | {
        'source_ref': f'M{job["source_seq"]}', 'saved_chars': job['source_length'],
        'processed_range': [0, job['next_offset']], 'unprocessed_range': [job['next_offset'], job['source_length']],
        'calls': calls[0] if calls else 0, 'max_calls': 16,
        'notice': '处理范围是辅助分析覆盖，不是主模型逐字已读；处理完成不保证结论正确。'}


def request_progress(chat_id, request_id):
    conn = store._get_conn()
    with store._lock:
        row = conn.execute('SELECT j.*,e.calls AS execution_calls FROM document_jobs j JOIN document_executions e ON e.job_id=j.job_id WHERE e.chat_id=? AND e.request_id=?',
                           (chat_id, request_id)).fetchone()
        if not row:
            return None
        result = progress(dict(row))
        result['calls'] = row['execution_calls']
        if row['request_id'] != request_id:
            result.update(status='continued', request_id=request_id, continued_in=row['request_id'],
                          latest_task_status=row['status'],
                          notice='此任务已由后续提问继续；范围为任务最新处理进度，不是原请求完成状态或主模型已读范围。')
        return result


def resumable(chat_id):
    conn = store._get_conn()
    with store._lock:
        rows = conn.execute("SELECT * FROM document_jobs WHERE chat_id=? AND status!='completed' ORDER BY updated_at DESC LIMIT 3", (chat_id,)).fetchall()
        return [{'job_id': r['job_id'], 'source_ref': f'M{r["source_seq"]}', 'task': r['task'],
                 'status': r['status']} for r in rows]


def claim(chat_id, request_id, attempt, upto_seq, source_ref, task, fingerprint, job_id=None):
    conn = store._get_conn()
    seq = int(source_ref[1:])
    with store._lock, conn:
        store.ensure_turn_active(chat_id, request_id, attempt)
        existing = conn.execute('SELECT job_id FROM document_executions WHERE chat_id=? AND request_id=?', (chat_id, request_id)).fetchone()
        if existing:
            if job_id and job_id != existing['job_id']:
                raise store.SessionError('document_request_job_conflict')
            job_id = existing['job_id']
        previous = get(job_id, chat_id) if job_id else None
        if job_id and not previous:
            raise store.SessionError('document_job_not_found')
        if previous:
            raw = conn.execute('SELECT content FROM chat_messages WHERE chat_id=? AND seq=?', (chat_id, seq)).fetchone()
            if (not raw or seq > upto_seq or previous['source_seq'] != seq or previous['task'] != task
                    or previous['source_hash'] != history.fingerprint(raw['content']) or previous['fingerprint'] != fingerprint):
                raise store.SessionError('document_task_mismatch')
            if previous['status'] == 'running':
                raise store.SessionError('document_job_busy')
            if previous['status'] != 'completed':
                conn.execute("UPDATE document_jobs SET status='running',generation=generation+1,request_id=?,attempt=?,error_code='',updated_at=? WHERE job_id=?",
                             (request_id, attempt, store._now_iso(), job_id))
            original = raw['content']
        else:
            raw = next((r for r in history.records(chat_id, upto_seq, request_id) if r['seq'] == seq), None)
            if not raw:
                raise store.SessionError('document_source_unavailable')
            original = raw['content']
            if not original:
                raise store.SessionError('document_source_empty')
            job_id = 'D' + uuid4().hex
            conn.execute('''INSERT INTO document_jobs(job_id,chat_id,source_seq,source_hash,source_length,task,
                fingerprint,status,request_id,attempt,updated_at) VALUES(?,?,?,?,?,?,?,'running',?,?,?)''',
                (job_id, chat_id, seq, history.fingerprint(original), len(original), task, fingerprint, request_id, attempt, store._now_iso()))
        conn.execute('INSERT OR IGNORE INTO document_executions(chat_id,request_id,job_id) VALUES(?,?,?)', (chat_id, request_id, job_id))
        return get(job_id, chat_id), original


def check(job):
    store.ensure_turn_active(job['chat_id'], job['request_id'], job['attempt'])
    current = get(job['job_id'], job['chat_id'])
    if not current or current['generation'] != job['generation'] or current['status'] != 'running':
        raise store.SessionError('document_interrupted')


def reserve_call(job):
    conn = store._get_conn()
    with store._lock, conn:
        check(job)
        cur = conn.execute('UPDATE document_executions SET calls=calls+1 WHERE chat_id=? AND request_id=? AND calls<16',
                           (job['chat_id'], job['request_id']))
        if not cur.rowcount:
            raise store.SessionError('document_call_budget_exceeded')


def frontier(job):
    conn = store._get_conn()
    with store._lock:
        return [dict(r) for r in conn.execute('SELECT * FROM document_nodes WHERE job_id=? AND active=1 ORDER BY offset', (job['job_id'],))]


def checkpoint(job, offset, end, content, *, replaced=(), input_tokens=0, output_tokens=0, elapsed_ms=0):
    conn = store._get_conn()
    with store._lock, conn:
        check(job)
        current = get(job['job_id'], job['chat_id'])
        if replaced:
            nodes = [r for r in frontier(job) if r['node_id'] in replaced]
            if (len(nodes) != len(replaced) or nodes[0]['offset'] != offset or nodes[-1]['end'] != end
                    or any(a['end'] != b['offset'] for a, b in zip(nodes, nodes[1:]))):
                raise store.SessionError('document_checkpoint_conflict')
            conn.executemany('UPDATE document_nodes SET active=0 WHERE node_id=? AND job_id=?', [(i, job['job_id']) for i in replaced])
        elif offset != current['next_offset'] or not offset < end <= current['source_length']:
            raise store.SessionError('document_checkpoint_conflict')
        conn.execute('''INSERT INTO document_nodes(job_id,kind,offset,end,content,input_tokens,output_tokens,elapsed_ms)
            VALUES(?,?,?,?,?,?,?,?)''', (job['job_id'], 'reduce' if replaced else 'piece', offset, end, content, input_tokens, output_tokens, elapsed_ms))
        if not replaced:
            conn.execute('UPDATE document_jobs SET next_offset=?,updated_at=? WHERE job_id=?', (end, store._now_iso(), job['job_id']))
        return get(job['job_id'], job['chat_id'])


def finish(job, status, error_code=''):
    conn = store._get_conn()
    with store._lock, conn:
        check(job)
        if status == 'completed':
            nodes = frontier(job)
            if job['next_offset'] != job['source_length'] or len(nodes) != 1 or nodes[0]['offset'] != 0 or nodes[0]['end'] != job['source_length']:
                raise store.SessionError('document_coverage_incomplete')
        conn.execute('UPDATE document_jobs SET status=?,error_code=?,updated_at=? WHERE job_id=?', (status, error_code, store._now_iso(), job['job_id']))
        return get(job['job_id'], job['chat_id'])


def interrupt(conn, chat_id=None, request_id=None, code='backend_restarted'):
    where, args = (" AND chat_id=? AND request_id=?", (chat_id, request_id)) if chat_id else ('', ())
    conn.execute("UPDATE document_jobs SET status='interrupted',generation=generation+1,error_code=? WHERE status='running'" + where,
                 (code, *args))
