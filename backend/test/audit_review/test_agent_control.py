"""自动化取消与步骤协议；使用既有隔离环境，不调用真实模型。"""
import importlib
import threading
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_backend import runtime, tmp_path


@pytest.fixture
def agent(runtime):
    loop = importlib.import_module('agent.loop')
    runtime.monkeypatch.setattr(loop, '_sessions', {})
    runtime.monkeypatch.setattr(loop, '_dump_screenshot', lambda *a: None)
    runtime.monkeypatch.setattr(loop, '_log_exec_tokens', lambda *a: None)
    runtime.monkeypatch.setattr(loop, '_call_llm', lambda *a: {'action': {'type': 'click', 'index': 1}})
    app = FastAPI()
    app.include_router(importlib.import_module('api.agent').router)
    with TestClient(app) as api:
        yield loop, api, runtime.monkeypatch


def body():
    return {'protocol_version': 2, 'request_id': 'start', 'session_id': 'synthetic-agent',
            'task': '合成测试', 'model': 'offline-test', 'budget_ms': 180000,
            'page_state': {'tab_id': 7, 'observation_id': 'obs-1', 'document_epoch': 'doc-1',
                           'interactive_elements': [{'id': 1, 'tag': 'button', 'text': '测试'}]}}


def test_editor_capabilities_and_focus_reach_model(runtime):
    from agent.context_builder import build_observation_message, _format_element, SYSTEM_PROMPT
    from agent.state import PageState
    editable = {'id': 7, 'tag': 'div', 'editor_type': 'codemirror5', 'editable': True,
                'focused': True, 'value': '草稿'}
    message = build_observation_message(PageState(
        focused_element='[7] codemirror5 frame=test-frame', interactive_elements=[editable]))
    for expected in ['当前焦点: [7]', 'frame=test-frame', 'editor=codemirror5', '可输入', '已聚焦', '草稿']:
        assert expected in message
    readonly = _format_element({**editable, 'editable': False, 'read_only': True})
    assert '不可输入' in readonly and '只读' in readonly
    assert 'clear=false' in SYSTEM_PROMPT and '不发送' in SYSTEM_PROMPT
    assert 'modifiers' in SYSTEM_PROMPT


def test_editing_params_survive_action_parser(runtime):
    from agent.loop import _parse_action
    typed = _parse_action('type', {'index': 7, 'text': '中文🙂\n草稿', 'clear': False})
    assert typed.params == {'text': '中文🙂\n草稿', 'clear': False}
    shortcut = _parse_action('press_key', {'index': 7, 'key': 'a', 'modifiers': ['Control']})
    assert shortcut.params == {'key': 'a', 'modifiers': ['Control']}


def test_cancelled_session_does_not_call_model(agent):
    loop, api, monkeypatch = agent
    session = loop.create_session('synthetic-agent', 'test', 'offline')
    loop.cancel_session(session.session_id)
    monkeypatch.setattr(loop, '_call_llm', lambda *a: pytest.fail('取消后不应调用模型'))
    from agent.state import PageState
    assert loop.run_step(session, PageState())['status'] == 'cancelled'


@pytest.mark.parametrize('action', [{'type': 'click', 'index': 1}, {'type': 'task_complete', 'summary': 'late'}])
def test_cancel_during_model_discards_late_result(agent, action):
    loop, api, monkeypatch = agent
    entered, release = threading.Event(), threading.Event()

    def delayed(*args):
        entered.set()
        assert release.wait(10)
        return {'action': action}

    monkeypatch.setattr(loop, '_call_llm', delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(api.post, '/v1/agent/execute', json=body())
        try:
            assert entered.wait(10)
            assert api.post('/v1/agent/cancel', json={'session_id': 'synthetic-agent'}).status_code == 200
        finally:
            release.set()
        result = pending.result(timeout=10).json()
    assert result['status'] == 'cancelled' and result['action'] is None
    assert loop.get_session('synthetic-agent').current_step == 0


def test_request_replay_and_conflict(agent):
    loop, api, _ = agent
    request = body()
    first = api.post('/v1/agent/execute', json=request)
    assert first.status_code == 200
    result = first.json()
    assert result['protocol_version'] == 2
    assert result['action']['action_id'] and result['action']['observation_id'] == 'obs-1'
    assert api.post('/v1/agent/execute', json=request).json() == result
    assert api.post('/v1/agent/execute', json={**request, 'task': 'different'}).status_code == 409
    step = {**request, 'request_id': 'next', 'page_state': {**request['page_state'], 'observation_id': 'obs-2'},
            'action_result': {'success': True, 'action_type': 'click',
                              'action_id': result['action']['action_id'], 'execution_state': 'completed'}}
    next_result = api.post('/v1/agent/step', json=step)
    assert next_result.status_code == 200
    assert api.post('/v1/agent/step', json=step).json() == next_result.json()
    assert loop.get_session(request['session_id']).current_step == 2
    assert api.post('/v1/agent/step', json={**step, 'request_id': 'old-result'}).status_code == 409


def test_cancel_before_execute_fences_delayed_start(agent):
    loop, api, monkeypatch = agent
    assert api.post('/v1/agent/cancel', json={'session_id': 'synthetic-agent'}).status_code == 200
    monkeypatch.setattr(loop, '_call_llm', lambda *a: pytest.fail('迟到启动不应调用模型'))
    assert api.post('/v1/agent/execute', json=body()).json()['status'] == 'cancelled'


def test_protocol_and_observation_are_required(agent):
    _, api, _ = agent
    assert api.post('/v1/agent/execute', json={**body(), 'protocol_version': 1}).status_code == 409
    assert api.post('/v1/agent/execute', json={**body(), 'page_state': {}}).status_code == 400


def test_pending_decision_can_be_queried_without_reexecution(agent):
    loop, api, monkeypatch = agent
    entered, release = threading.Event(), threading.Event()

    def delayed(*args):
        entered.set()
        assert release.wait(10)
        return {'action': {'type': 'wait', 'params': {'ms': 1}}}

    monkeypatch.setattr(loop, '_call_llm', delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(api.post, '/v1/agent/execute', json=body())
        try:
            assert entered.wait(10)
            state = api.post('/v1/agent/status', json={'session_id': 'synthetic-agent', 'request_id': 'start'}).json()
            assert state['status'] == 'processing'
            assert api.post('/v1/agent/execute', json=body()).json()['status'] == 'processing'
        finally:
            release.set()
        completed = pending.result(timeout=10).json()
    assert api.post('/v1/agent/status', json={'session_id': 'synthetic-agent', 'request_id': 'start'}).json() == completed


def test_unknown_execution_cannot_advance_model(agent):
    _, api, _ = agent
    first = api.post('/v1/agent/execute', json=body()).json()
    request = {**body(), 'request_id': 'unsafe', 'action_result': {
        'success': False, 'action_type': 'click', 'action_id': first['action']['action_id'], 'execution_state': 'unknown'}}
    assert api.post('/v1/agent/step', json=request).status_code == 409


@pytest.mark.parametrize('change', ['tab', 'observation', 'action'])
def test_old_or_foreign_step_is_rejected(agent, change):
    _, api, _ = agent
    first = api.post('/v1/agent/execute', json=body()).json()
    request = {**body(), 'request_id': 'next', 'page_state': {**body()['page_state'], 'observation_id': 'obs-2'},
               'action_result': {'success': True, 'action_type': 'click', 'execution_state': 'completed',
                                 'action_id': first['action']['action_id']}}
    if change == 'tab':
        request['page_state']['tab_id'] = 8
    elif change == 'observation':
        request['page_state']['observation_id'] = 'obs-1'
    else:
        request['action_result']['action_id'] = 'wrong-action'
    assert api.post('/v1/agent/step', json=request).status_code == 409


def test_cancel_overrides_cached_action(agent):
    _, api, _ = agent
    api.post('/v1/agent/execute', json=body())
    api.post('/v1/agent/cancel', json={'session_id': 'synthetic-agent'})
    for path, request in [('/execute', body()), ('/status', {'session_id': 'synthetic-agent', 'request_id': 'start'})]:
        response = api.post('/v1/agent' + path, json=request).json()
        assert response['status'] == 'cancelled' and response['action'] is None


def test_deadline_discards_late_model_action(agent):
    loop, api, monkeypatch = agent

    def late(session):
        session.decision_deadline = time.monotonic() - 1
        loop.get_session(session.session_id).decision_deadline = session.decision_deadline
        return {'action': {'type': 'click', 'index': 1}}

    monkeypatch.setattr(loop, '_call_llm', late)
    response = api.post('/v1/agent/execute', json=body()).json()
    assert response['status'] == 'error' and response['action'] is None


def test_inflight_session_not_evicted_and_other_request_rejected(agent):
    loop, api, monkeypatch = agent
    entered, release = threading.Event(), threading.Event()

    def delayed(session):
        entered.set()
        assert release.wait(10)
        return {'action': {'type': 'wait'}}

    monkeypatch.setattr(loop, '_call_llm', delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(api.post, '/v1/agent/execute', json=body())
        try:
            assert entered.wait(10)
            session = loop.get_session('synthetic-agent')
            session.last_activity = 0
            with loop._sessions_lock:
                assert loop._cleanup_expired_sessions() == []
                monkeypatch.setattr(loop, 'MAX_SESSIONS', 1)
                assert loop._evict_if_full() is None
            response = api.post('/v1/agent/execute', json={**body(), 'request_id': 'parallel'})
            assert response.status_code == 409
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 200


def test_cancel_stops_retry_loop_without_another_model_call(agent):
    loop, _, _ = agent
    session = loop.create_session('retry-test', 'test', 'offline')
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        loop.cancel_session(session.session_id)
        raise RuntimeError('synthetic failure')

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    assert loop._create_with_retry(client, 'offline', [], session) is None
    assert len(calls) == 1


def test_optional_compaction_shares_cancel_budget_but_not_failure_state(agent):
    loop, _, monkeypatch = agent
    from agent.state import AgentStatus, HistoryItem
    session = loop.create_session('compact-test', 'test', 'offline')
    session.decision_deadline = time.monotonic() + 30
    session.history_items = [HistoryItem(step=i, memory='synthetic') for i in range(30)]

    def fail(client, model, messages, session):
        assert session.cancel_event is original.cancel_event
        assert session.decision_deadline == original.decision_deadline
        session.status = AgentStatus.ERROR
        session.error = 'optional summary failed'
        return None

    original = session
    monkeypatch.setattr(loop, '_create_with_retry', fail)
    loop._maybe_compact_history(session)
    assert session.status == AgentStatus.RUNNING
    assert session.error is None
