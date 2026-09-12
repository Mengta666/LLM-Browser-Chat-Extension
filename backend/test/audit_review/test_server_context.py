"""服务端会话协议验收：实际 HTTP 路由、SDK、SQLite，模型与网络边界隔离。"""

import importlib
import json
import sqlite3
import asyncio
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from test_backend import runtime, tmp_path, completion, set_model, seed_history


def request(request_id='r1', seq=0, text='hello', **kwargs):
    return {'context_mode': 'server', 'chat_id': 'server_audit', 'request_id': request_id,
            'expected_last_seq': seq, 'model': 'offline-test', 'stream': False,
            'messages': [{'role': 'user', 'content': text}], **kwargs}


def completed_turn(runtime, req, content='question'):
    seq = (runtime.history.get_session('server_audit') or {}).get('last_seq', 0)
    turn = runtime.history.begin_turn('server_audit', req, req, seq, content)
    return runtime.history.complete_turn('server_audit', req, turn['attempt'], 'answer-'+req, [])


def test_server_roundtrip_and_replay(runtime):
    calls = []
    def model(req):
        calls.append(json.loads(req.content))
        return completion('stored-answer')
    with set_model(runtime, model):
        first = runtime.api.post('/v1/chat/completions', json=request())
        replay = runtime.api.post('/v1/chat/completions', json=request())
    assert first.status_code == replay.status_code == 200
    assert len(calls) == 1
    assert replay.json()['session_meta']['persisted']
    assert runtime.history.count_messages('server_audit') == 2
    state = runtime.api.get('/v1/sessions/server_audit/requests/r1').json()
    assert state['status'] == 'completed'
    assert state['last_seq'] == 2
    assert [m['seq'] for m in state['messages']] == [1, 2]


def test_server_multiple_turns_keep_history_and_current_once(runtime):
    calls = []
    def model(req):
        calls.append(json.loads(req.content))
        return completion('answer')
    with set_model(runtime, model):
        assert runtime.api.post('/v1/chat/completions', json=request()).status_code == 200
        assert runtime.api.post('/v1/chat/completions', json=request('r2', 2, 'next')).status_code == 200
    assert [m['content'] for m in calls[-1]['messages'] if m['role'] != 'system'] == ['hello', 'answer', 'next']


@pytest.mark.parametrize('change,code', [({'messages': [{'role': 'user', 'content': 'changed'}]}, 'request_conflict'),
                                       ({'request_id': 'r2'}, 'history_conflict')])
def test_conflict_does_not_call_model_or_write(runtime, change, code):
    with set_model(runtime, lambda req: completion()):
        assert runtime.api.post('/v1/chat/completions', json=request()).status_code == 200
    body = request(**change)
    response = runtime.api.post('/v1/chat/completions', json=body)
    assert response.status_code == 409
    assert response.json()['error']['code'] == code
    assert runtime.history.count_messages('server_audit') == 2


def test_running_and_concurrent_requests_are_exclusive(runtime):
    def begin(req):
        try:
            return runtime.history.begin_turn('server_audit', req, req, 0, 'hello')['status']
        except runtime.history.SessionError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, ['r1', 'r2']))
    assert sorted(results) == ['chat_busy', 'running']
    assert runtime.history.count_messages('server_audit') == 1


def test_failed_turn_retry_and_stale_attempt(runtime):
    first = runtime.history.begin_turn('server_audit', 'r1', 'hash', 0, 'hello')
    runtime.history.fail_turn('server_audit', 'r1', first['attempt'], 'test')
    assert runtime.history.get_request('server_audit', 'r1')['can_retry']
    second = runtime.history.begin_turn('server_audit', 'r1', 'hash', 0, 'hello')
    assert second['attempt'] == 2
    with pytest.raises(runtime.history.SessionError, match='stale_attempt'):
        runtime.history.complete_turn('server_audit', 'r1', 1, 'late', [])
    runtime.history.complete_turn('server_audit', 'r1', 2, 'good', [])
    assert runtime.history.count_messages('server_audit') == 2


def test_failed_history_is_not_context_and_old_retry_rejected(runtime):
    first = runtime.history.begin_turn('server_audit', 'r1', 'hash', 0, 'failed-question')
    runtime.history.fail_turn('server_audit', 'r1', first['attempt'], 'test')
    completed_turn(runtime, 'r2', 'valid-question')
    snapshot = runtime.history.context_snapshot('server_audit')
    assert [m['content'] for m in snapshot['messages']] == ['valid-question', 'answer-r2']
    with pytest.raises(runtime.history.SessionError, match='retry_stale'):
        runtime.history.begin_turn('server_audit', 'r1', 'hash', 0, 'failed-question')


def test_storage_failure_does_not_report_success(runtime):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic write failure')
    runtime.monkeypatch.setattr(runtime.history, 'complete_turn', unavailable)
    with set_model(runtime, lambda req: completion('visible-but-unsaved')):
        response = runtime.api.post('/v1/chat/completions', json=request())
    assert response.status_code >= 500
    assert response.json()['session_meta']['persisted'] is False
    assert runtime.history.get_request('server_audit', 'r1')['status'] == 'failed'
    assert runtime.history.count_messages('server_audit') == 1


def test_initial_storage_failure_does_not_call_model(runtime):
    runtime.monkeypatch.setattr(runtime.history, 'begin_turn', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError()))
    response = runtime.api.post('/v1/chat/completions', json=request())
    assert response.status_code == 503


def test_stream_completion_ack_follows_commit(runtime):
    def model(req):
        assert json.loads(req.content)['stream']
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=(
            'data: {"id":"x","object":"chat.completion.chunk","created":0,"model":"offline-test","choices":[{"index":0,"delta":{"content":"stream-answer"}}]}\n\n'
            'data: [DONE]\n\n').encode())
    with set_model(runtime, model):
        response = runtime.api.post('/v1/chat/completions', json=request(stream=True))
    assert response.status_code == 200
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
    assert frames[-1]['session_meta']['persisted']
    assert runtime.history.get_request('server_audit', 'r1')['status'] == 'completed'
    assert response.text.index('stream-answer') < response.text.index('session_meta') < response.text.index('[DONE]')


def test_restart_recovers_interrupted_turn(runtime):
    runtime.history.begin_turn('server_audit', 'r1', 'hash', 0, 'hello')
    runtime.history._conn.close()
    runtime.history._conn = None
    state = runtime.history.get_request('server_audit', 'r1')
    assert state['status'] == 'interrupted' and state['can_retry']


@pytest.mark.parametrize('size', [201, 260, 520, 5002])
def test_pagination_returns_every_message_without_gaps(runtime, size):
    # 单事务构造大量已提交旧消息，不把测试时间耗在逐条 fsync 上。
    runtime.history.ensure_session('server_audit')
    conn = runtime.history._get_conn()
    with conn:
        conn.executemany('INSERT INTO chat_messages(message_id,chat_id,role,content,created_at,seq) VALUES(?,?,?,?,?,?)', [
            (str(i), 'server_audit', 'user' if i % 2 else 'assistant', f'message-{i}', '2026-01-01', i) for i in range(1, size+1)])
        conn.execute('UPDATE chat_sessions SET last_seq=? WHERE chat_id=?', (size, 'server_audit'))
    before = None
    seen = []
    while True:
        params = {'limit': 200, **({'before_seq': before} if before is not None else {})}
        data = runtime.api.get('/v1/sessions/server_audit/messages', params=params).json()
        if before is None:
            assert data['messages'][-1]['seq'] == size
        seen.extend(m['seq'] for m in data['messages'])
        if not data['has_more']:
            break
        before = data['before_seq']
    assert sorted(seen) == list(range(1, size+1))


def test_summary_tail_current_input_and_system_are_preserved(runtime):
    context = importlib.import_module('agent.memory.chat_context')
    for i in range(10):
        completed_turn(runtime, f'r{i}', f'question-{i}')
    runtime.monkeypatch.setattr(context.compact, '_summarize_llm', lambda *args: 'one-summary')
    assert context.compress('server_audit')
    runtime.history.begin_turn('server_audit', 'current', 'current', 20, 'CURRENT')
    messages = context.prepare('server_audit', {'role': 'user', 'content': 'CURRENT'}, ['BASE', 'KB_HINT'])
    assert messages[0]['content'].count('one-summary') == 1
    assert 'BASE' in messages[0]['content'] and 'KB_HINT' in messages[0]['content']
    assert [m['content'] for m in messages[1:]] == [value for i in range(7,10) for value in (f'question-{i}', f'answer-r{i}')] + ['CURRENT']
    assert context.compress('server_audit') is False
    assert context.prepare('server_audit', messages[-1], ['BASE', 'KB_HINT']) == messages


def test_long_message_is_fully_summarized_and_failure_does_not_advance(runtime):
    context = importlib.import_module('agent.memory.chat_context')
    completed_turn(runtime, 'long', 'A'*50000+'TAIL_SENTINEL')
    for i in range(3):
        completed_turn(runtime, f'r{i}')
    prompts = []
    def summarize(system, prompt):
        prompts.append(prompt)
        return 'summary'
    runtime.monkeypatch.setattr(context.compact, '_summarize_llm', summarize)
    assert context.compress('server_audit')
    assert len(prompts) >= 2
    assert 'TAIL_SENTINEL' in ''.join(prompts)
    assert runtime.history.context_snapshot('server_audit')['upto_seq'] == 2
    assert not runtime.history.publish_summary('server_audit', 'old', 2, 0)


def test_summary_failure_and_budget_guard(runtime):
    context = importlib.import_module('agent.memory.chat_context')
    for i in range(4):
        completed_turn(runtime, f'r{i}')
    runtime.monkeypatch.setattr(context.compact, '_summarize_llm', lambda *args: '')
    assert context.compress('server_audit') is False
    assert runtime.history.context_snapshot('server_audit')['upto_seq'] == 0
    runtime.monkeypatch.setattr(runtime.config, 'CHAT_CONTEXT_LENGTH', 100)
    with pytest.raises(context.ContextBudgetError):
        context.prepare('server_audit', {'role': 'user', 'content': 'new'}, ['BASE'])


def test_legacy_mode_cannot_write_server_session(runtime):
    completed_turn(runtime, 'r1')
    response = runtime.api.post('/v1/chat/completions', json={'chat_id': 'server_audit', 'messages': [{'role': 'user', 'content': 'legacy'}]})
    assert response.status_code == 409


def test_old_database_migration_is_lossless_and_backed_up(runtime):
    conn = sqlite3.connect(str(runtime.history._DB_PATH))
    with conn:
        conn.execute("CREATE TABLE chat_sessions(chat_id TEXT PRIMARY KEY,title TEXT,created_at TEXT,updated_at TEXT,deleted_at TEXT DEFAULT '',summary TEXT,summary_msg_count INTEGER,summary_updated_at TEXT)")
        conn.execute("CREATE TABLE chat_messages(message_id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created_at TEXT)")
        conn.execute("INSERT INTO chat_sessions VALUES('old','title','t','t','','legacy-summary',2,'t')")
        conn.executemany("INSERT INTO chat_messages VALUES(?,'old',?,?,?)", [('m2','assistant','a','t'), ('m1','user','u','s')])
    conn.close()
    messages = runtime.history.get_messages('old')
    assert [(m['message_id'], m['seq']) for m in messages] == [('m1',1),('m2',2)]
    session = runtime.history.get_session('old')
    assert session['summary'] == 'legacy-summary'
    assert session['context_summary'] == '' and session['summary_upto_seq'] == 0
    assert len(list(runtime.tmp_path.glob('history.migration-*.sqlite3'))) == 1
    runtime.history.begin_turn('old','new','hash',2,'new')
    assert [m['content'] for m in runtime.history.context_snapshot('old')['messages']] == ['u','a']


def test_closing_stream_releases_turn_without_saving_partial_answer(runtime):
    server = importlib.import_module('api.server_chat')
    def model(req):
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=(
            'data: {"id":"x","object":"chat.completion.chunk","created":0,"model":"offline-test","choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            'data: [DONE]\n\n').encode())
    async def disconnect():
        response = server.handle(runtime.chat.ChatRequest(**request(stream=True)))
        frame = await anext(response.body_iterator)
        assert 'partial' in frame and 'session_meta' not in frame
        await response.body_iterator.aclose()
    with set_model(runtime, model):
        asyncio.run(disconnect())
    state = runtime.history.get_request('server_audit','r1')
    assert state['status'] == 'interrupted' and state['can_retry']
    assert runtime.history.count_messages('server_audit') == 1


def test_middle_summary_batch_failure_keeps_old_cursor(runtime):
    context = importlib.import_module('agent.memory.chat_context')
    completed_turn(runtime, 'long', 'A'*150000)
    for i in range(3):
        completed_turn(runtime, f'r{i}')
    outputs = iter(['first-summary', ''])
    runtime.monkeypatch.setattr(context.compact, '_summarize_llm', lambda *a: next(outputs))
    assert not context.compress('server_audit')
    snapshot = runtime.history.context_snapshot('server_audit')
    assert snapshot['upto_seq'] == 0 and snapshot['version'] == 0 and snapshot['summary'] == ''
    assert len(snapshot['messages']) == 8


def test_automatic_hard_compaction_keeps_base_kb_and_boundary(runtime):
    context = importlib.import_module('agent.memory.chat_context')
    for i in range(5):
        completed_turn(runtime, f'r{i}', f'question-{i}')
    runtime.monkeypatch.setattr(runtime.config, 'CHAT_COMPACT_HARD_RATIO', 0)
    runtime.monkeypatch.setattr(context.compact, '_summarize_llm', lambda *a: 'COMPACTED')
    kb_id = runtime.kb.create_kb('Synthetic KB')['kb_id']
    calls = []
    def model(req):
        calls.append(json.loads(req.content))
        return completion()
    with set_model(runtime, model):
        response = runtime.api.post('/v1/chat/completions', json=request('current',10,'CURRENT',kb_id=kb_id))
    assert response.status_code == 200
    sent = calls[0]['messages']
    assert len([m for m in sent if m['role'] == 'system']) == 1
    assert runtime.chat._CHAT_BASE_SYSTEM in sent[0]['content'] and kb_id in sent[0]['content']
    assert sent[0]['content'].count('COMPACTED') == 1
    assert sent[1]['content'] == 'question-2' and sent[-1]['content'] == 'CURRENT'
    assert len(sent) == 8


def test_budget_counts_tool_arguments_and_images(runtime):
    context = importlib.import_module('agent.memory.chat_context')
    base = [{'role':'user','content':'hi'}]
    tool_call = {'role':'assistant','content':'','tool_calls':[{'id':'x','type':'function','function':{'name':'kb_search','arguments':'A'*50000}}]}
    assert context.request_tokens(base+[tool_call]) > context.request_tokens(base) + 1000
    image = {'role':'user','content':[{'type':'text','text':'hi'},{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]}
    runtime.history.begin_turn('server_audit','r1','hash',0,'hi')
    sent = context.prepare('server_audit',image,['BASE'])
    assert sent[-1] == image and len(sent) == 2
    assert context.request_tokens([image]) >= context.request_tokens(base) + 1000


def test_tool_result_budget_is_checked_before_second_model_call(runtime):
    calls = []
    runtime.monkeypatch.setattr(runtime.config,'CHAT_CONTEXT_LENGTH',8500)
    runtime.monkeypatch.setattr(runtime.agentic,'_execute_tool',lambda *a,**k: ('A'*50000,[]))
    def model(req):
        calls.append(req)
        return completion(arguments='{"query":"test","kb_id":"synthetic"}')
    with set_model(runtime, model):
        events = list(runtime.agentic.run_agentic_loop('offline-test',[{'role':'user','content':'test'}],[],enforce_budget=True))
    assert len(calls) == 1 and events[-1]['type'] == 'error'


def test_oversized_current_input_is_rejected_without_model_call(runtime):
    runtime.monkeypatch.setattr(runtime.config,'CHAT_CONTEXT_LENGTH',8500)
    response = runtime.api.post('/v1/chat/completions',json=request(text='A'*50000))
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'context_budget_exceeded'
    assert runtime.history.get_request('server_audit','r1')['can_retry']


def test_truncated_model_output_is_not_saved_as_completed(runtime):
    def model(req):
        payload = json.loads(completion('unfinished').content)
        payload['choices'][0]['finish_reason'] = 'length'
        return httpx.Response(200,json=payload)
    with set_model(runtime,model):
        response = runtime.api.post('/v1/chat/completions',json=request())
    assert response.status_code == 502
    assert response.json()['error']['code'] == 'model_output_truncated'
    assert runtime.history.count_messages('server_audit') == 1


def test_overlong_summary_gets_one_bounded_reduction_attempt(runtime):
    context = importlib.import_module('agent.memory.chat_context')
    for i in range(4):
        completed_turn(runtime,f'r{i}')
    prompts = []
    def summarize(system,prompt):
        prompts.append(prompt)
        return 'A'*10000 if len(prompts) == 1 else 'concise-summary'
    runtime.monkeypatch.setattr(context.compact,'_summarize_llm',summarize)
    assert context.compress('server_audit')
    assert len(prompts) == 2 and 'A'*10000 in prompts[1]
    assert runtime.history.context_snapshot('server_audit')['summary'] == 'concise-summary'


def test_manual_search_still_works_with_auto_search_off_and_replays_citations(runtime):
    search = importlib.import_module('search.searxng')
    result = SimpleNamespace(title='Synthetic source',url='https://example.invalid/source',snippet='synthetic fact')
    queries = []; calls = []
    def search_once(query):
        queries.append(query)
        return [result]
    runtime.monkeypatch.setattr(search,'search_searxng',search_once)
    def model(req):
        calls.append(json.loads(req.content))
        return completion('synthetic fact [1]')
    with set_model(runtime,model):
        response = runtime.api.post('/v1/chat/completions',json=request(search_query='test search'))
        replay = runtime.api.post('/v1/chat/completions',json=request(search_query='test search'))
    assert response.status_code == replay.status_code == 200
    assert len(queries) == len(calls) == 1
    assert calls[0]['messages'][-1]['role'] == 'tool'
    assert 'synthetic fact' in calls[0]['messages'][-1]['content']
    assert replay.json()['enhancement_steps'][0]['sources'][0]['url'] == result.url
