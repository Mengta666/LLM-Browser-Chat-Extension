"""请求级知识库配置快照、预算与客观读取进度。"""

import json
from storage import chat_store as store


def init_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_kb_runtime (
        chat_id TEXT NOT NULL, request_id TEXT NOT NULL, state_json TEXT NOT NULL,
        PRIMARY KEY(chat_id,request_id))''')


def load(chat_id, request_id):
    conn = store._get_conn()
    with store._lock:
        row = conn.execute('SELECT state_json FROM chat_kb_runtime WHERE chat_id=? AND request_id=?',
                           (chat_id, request_id)).fetchone()
        return json.loads(row[0]) if row else None


def save(conn, chat_id, request_id, state):
    conn.execute('''INSERT INTO chat_kb_runtime VALUES(?,?,?) ON CONFLICT(chat_id,request_id)
        DO UPDATE SET state_json=excluded.state_json''', (chat_id, request_id, json.dumps(state, ensure_ascii=False)))


def checkpoint(chat_id, request_id, attempt, state):
    conn = store._get_conn()
    with store._lock, conn:
        store.ensure_turn_active(chat_id, request_id, attempt)
        save(conn, chat_id, request_id, state)


def workspace_progress(state):
    workspace=state.get('workspace', {})
    packs=list(workspace.get('packs',{}).values())
    return {k:v for k,v in workspace.items() if k in
        ('version','phase','archived_count','omitted_count','raw_citations','derived_citations')} | {
        'completed_packs':sum(p['status']=='complete' for p in packs),
        'failed_packs':sum(p['status']=='failed' for p in packs),
        'saved_parts':sum(len(p.get('parts',[])) for p in packs),
        'pending_parts':sum(len(p.get('plan',[]))-len(p.get('parts',[])) for p in packs if p['status']=='processing')}


def progress(chat_id, request_id):
    from search import kb_coverage, kb_read_queue
    state = load(chat_id, request_id)
    if state is None:
        return None
    kb_coverage.reconcile_pending(state)
    coverage = kb_read_queue.report(state)
    reason = state.get('stop_reason', '')
    needs = state.get('needs', [])
    if reason == 'no_progress' and needs and all(n['status'] in ('ready', 'unresolved') for n in needs) and not coverage['missing_scope_count']:
        reason = 'needs_assessed'
    conn = store._get_conn()
    with store._lock:
        request = conn.execute('SELECT status,phase FROM chat_turns WHERE chat_id=? AND request_id=?',
                               (chat_id, request_id)).fetchone()
    return {'type': 'kb_progress', 'status': 'done', 'kb_stop_reason': reason,
            'request_phase': request['phase'] if request else '',
            'request_status': request['status'] if request else '', 'restored': True,
            'kb_pending': state['pending'], 'kb_read_coverage': coverage,
            'kb_workspace': workspace_progress(state)}
