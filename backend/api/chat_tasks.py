"""单进程聊天执行器。网络订阅不拥有任务，也不能取消任务。"""

import asyncio
import json
import threading
import time

from fastapi.responses import StreamingResponse

from observability.logger import get_logger
from storage import chat_events, chat_store as store

_log = get_logger('chat_transport')
_lock = threading.RLock()
_tasks = {}
_closing = False
MAX_ACTIVE = 4
HEARTBEAT_SECONDS = 10


def start():
    global _closing
    with _lock:
        _closing = False


def submit(chat_id, request_id, digest, begin, factory):
    with _lock:
        previous = chat_events.head(chat_id, request_id)
        if previous and previous['request_hash'] != digest:
            raise store.SessionError('request_conflict')
        if previous and previous['status'] in ('running', 'completed', 'partial'):
            return previous
        if _closing or len(_tasks) >= MAX_ACTIVE:
            raise store.SessionError('chat_capacity_exceeded', 503)
        turn = begin()
        key = (chat_id, request_id, turn['attempt'])
        worker = threading.Thread(target=_run, args=(key, factory, turn), name='chat-request', daemon=True)
        _tasks[key] = worker
        worker.start()
        return turn


def _run(key, factory, turn):
    chat_id, request_id, attempt = key
    text = []
    chars = 0
    flushed = time.monotonic()
    source = None

    def flush():
        nonlocal chars, flushed
        if text:
            chat_events.append(*key, {'choices': [{'delta': {'content': ''.join(text)}, 'index': 0}]})
            text.clear()
            chars = 0
            flushed = time.monotonic()

    _log.info('task_started', session_id=chat_id, data={'request_id': request_id, 'attempt': attempt})
    try:
        source = factory(turn)
        for event in source:
            delta = event.get('choices', [{}])[0].get('delta', {}).get('content')
            if delta and set(event) == {'choices'}:
                text.append(delta)
                chars += len(delta)
                if chars >= 1024 or time.monotonic() - flushed >= .25:
                    flush()
            else:
                flush()
                chat_events.append(*key, event)
        flush()
    except Exception as exc:
        _log.warn('task_execution_error', session_id=chat_id, data={
            'request_id': request_id, 'attempt': attempt, 'error_type': type(exc).__name__,
            'error_code': getattr(exc, 'code', 'task_execution_failed')})
        store.fail_turn(*key, getattr(exc, 'code', 'task_execution_failed'))
    finally:
        try:
            if source is not None:
                source.close()
            store.fail_turn(*key, 'task_execution_incomplete')
        finally:
            with _lock:
                _tasks.pop(key, None)
        row = chat_events.head(chat_id, request_id)
        _log.info('task_finished', session_id=chat_id, data={'request_id': request_id,
            'attempt': attempt, 'status': row['status'] if row else 'deleted',
            'error_code': row['error_code'] if row else ''})


def stop():
    global _closing
    with _lock:
        _closing = True
        active = list(_tasks)
    for key in active:
        store.fail_turn(*key, 'backend_shutdown', interrupted=True)


def snapshot(chat_id, request_id):
    state = store.get_request(chat_id, request_id)
    # 不向订阅发送原请求参数或客户端配置。
    return {**{k: state[k] for k in ('chat_id', 'request_id', 'attempt', 'status', 'phase', 'error_code',
        'finish_reason', 'can_retry', 'can_cancel', 'can_continue', 'last_seq', 'user_seq', 'assistant_seq',
        'messages', 'tool_steps', 'kb_progress', 'context_compaction', 'document_analysis')},
        'persisted': state['status'] in ('completed', 'partial')}


def subscribe(chat_id, request_id, attempt, after=0):
    chat_events.page(chat_id, request_id, attempt, after, 1)

    async def stream():
        cursor = after
        last_ping = 0
        reason = 'subscriber_disconnected'
        _log.info('stream_subscribed', session_id=chat_id,
                  data={'request_id': request_id, 'attempt': attempt, 'after': after})
        try:
            while True:
                try:
                    current, batch = await asyncio.to_thread(chat_events.page, chat_id, request_id, attempt, cursor)
                except store.SessionError as exc:
                    yield 'data: ' + json.dumps({'error': {'code': exc.code}}) + '\n\n'
                    reason = exc.code
                    break
                if current['status'] in ('cancelled', 'interrupted'):
                    batch = []
                for event in batch:
                    cursor = event['id']
                    payload = {**event['data'], 'request_event': {'attempt': attempt, 'id': cursor}}
                    yield f'id: {attempt}:{cursor}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                with _lock:
                    active = (chat_id, request_id, attempt) in _tasks
                if not batch and current['status'] != 'running' and not active:
                    state = await asyncio.to_thread(snapshot, chat_id, request_id)
                    meta = {k: v for k, v in state.items() if k not in ('messages', 'tool_steps', 'kb_progress')}
                    yield 'data: ' + json.dumps({'request_snapshot': state, 'session_meta': meta}, ensure_ascii=False) + '\n\n'
                    yield 'data: [DONE]\n\n'
                    reason = 'task_terminal'
                    break
                now = time.monotonic()
                if now - last_ping >= HEARTBEAT_SECONDS:
                    last_ping = now
                    yield 'data: ' + json.dumps({'heartbeat': {'attempt': attempt, 'phase': current['phase'],
                        'status': current['status']}}) + '\n\n'
                await asyncio.sleep(.2)
        finally:
            _log.info('stream_detached', session_id=chat_id,
                      data={'request_id': request_id, 'attempt': attempt, 'after': cursor, 'reason': reason})

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def wait_result(chat_id, request_id, attempt):
    while True:
        state = chat_events.head(chat_id, request_id)
        with _lock:
            active = (chat_id, request_id, attempt) in _tasks
        if not active and (not state or state['attempt'] != attempt or state['status'] != 'running'):
            return snapshot(chat_id, request_id) if state else None
        time.sleep(.1)
