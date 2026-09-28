"""聊天决策与收尾共用的流式接收；完整结束后才交付工具调用。"""

import hashlib
import json
import queue
import threading
import time

import httpx
from openai import APITimeoutError
from openai.types.chat import ChatCompletion
from observability.logger import get_logger
from storage.chat_store import SessionError

_log = get_logger('chat_context')


def fingerprint(messages, tools=None):
    return hashlib.sha256(json.dumps([messages, tools], ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def events(client, *, model, messages, timeout, deadline=None, check_active=None,
             clock=time.monotonic, phase='decision', chat_id='', request_id='', input_tokens=None, count_meta=None, **options):
    started = clock()
    expires = min(deadline, started + timeout) if deadline is not None else started + timeout
    if check_active:
        check_active()
    if clock() >= expires:
        raise APITimeoutError(request=httpx.Request('POST', str(client.base_url)))
    info = {'request_id': request_id, 'phase': phase, 'input_tokens': input_tokens,
            'input_fingerprint': fingerprint(messages, options.get('tools')), 'timeout_s': max(0, expires - started),
            **(count_meta or {})}
    _log.info('model_call_started', session_id=chat_id, data=info)
    inbox = queue.Queue(maxsize=64)
    stopped = threading.Event()
    streams = []

    def send(item):
        while not stopped.is_set():
            try:
                inbox.put(item, timeout=.1)
                return
            except queue.Full:
                pass

    def receive():
        response = None
        try:
            if stopped.is_set():
                return
            response = client.chat.completions.create(model=model, messages=messages, stream=True,
                stream_options={'include_usage': True}, timeout=max(.1, expires - clock()), **options)
            streams.append(response)
            send(('headers', response.response.headers.get('x-request-id')))
            for chunk in response:
                if stopped.is_set():
                    break
                choice = chunk.choices[0] if chunk.choices else None
                delta = choice.delta if choice else None
                # 不将隐藏推理放入队列、响应对象或持久化日志。
                send(('chunk', {'id': chunk.id, 'created': chunk.created, 'model': chunk.model,
                    'content': delta.content if delta else None,
                    'reasoning_chars': len(getattr(delta, 'reasoning_content', None) or getattr(delta, 'reasoning', None) or ''),
                    'tool_calls': [t.model_dump(exclude_none=True) for t in delta.tool_calls or []] if delta else [],
                    'finish_reason': choice.finish_reason if choice else None,
                    'usage': chunk.usage.model_dump() if chunk.usage else None}))
            send(('done', None))
        except Exception as exc:
            send(('error', exc))
        finally:
            if response is not None:
                response.close()

    thread = threading.Thread(target=receive, name='chat-model-stream', daemon=True)
    content, calls, usage, finish = [], {}, None, None
    metadata = {'id': '', 'created': 0, 'model': model}
    first = None
    reasoning_chars = 0
    thread.start()
    try:
        while True:
            if check_active:
                check_active()
            if clock() >= expires:
                raise APITimeoutError(request=httpx.Request('POST', str(client.base_url)))
            try:
                kind, value = inbox.get(timeout=.1)
            except queue.Empty:
                continue
            if kind == 'error':
                raise value
            if kind == 'done':
                break
            if kind == 'headers':
                info['upstream_request_id'] = value
                continue
            if first is None:
                first = round(clock() - started, 3)
                _log.info('model_call_first_response', session_id=chat_id, data={**info, 'elapsed_s': first})
            metadata = {k: value[k] for k in metadata}
            info['upstream_completion_id'] = metadata['id']
            if value['content']:
                content.append(value['content'])
                yield value['content']
            reasoning_chars += value['reasoning_chars']
            if value['finish_reason']:
                finish = value['finish_reason']
            if value['usage']:
                usage = value['usage']
            for part in value['tool_calls']:
                item = calls.setdefault(part['index'], {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                item['id'] += part.get('id', '')
                for key in ('name', 'arguments'):
                    item['function'][key] += part.get('function', {}).get(key, '')
        if not finish or calls and finish not in ('tool_calls', 'length'):
            raise SessionError('model_response_incomplete', 502)
        if calls and finish == 'tool_calls' and any(not c['id'] or not c['function']['name'] for c in calls.values()):
            raise SessionError('model_response_incomplete', 502)
        response = ChatCompletion.model_validate({**metadata, 'object': 'chat.completion', 'usage': usage,
            'choices': [{'index': 0, 'finish_reason': finish, 'message': {'role': 'assistant',
                'content': ''.join(content), 'tool_calls': [calls[i] for i in sorted(calls)] or None}}]})
        _log.info('model_call_completed', session_id=chat_id, data={**info,
            'elapsed_s': round(clock() - started, 3), 'first_response_s': first, 'finish_reason': finish,
            'output_chars': len(response.choices[0].message.content or ''), 'reasoning_chars': reasoning_chars, 'usage': usage})
        return response
    except Exception as exc:
        _log.warn('model_call_failed', session_id=chat_id, data={**info,
            'elapsed_s': round(clock() - started, 3), 'first_response_s': first,
            'error_type': type(exc).__name__, 'error_code': getattr(exc, 'code', None),
            'received_tool_parts': len(calls), 'reasoning_chars': reasoning_chars})
        raise
    finally:
        stopped.set()
        # close 可能等待底层 socket；不让它阻塞停止按钮和会话状态落库。
        if thread.is_alive() and streams:
            threading.Thread(target=streams[0].close, name='chat-stream-close', daemon=True).start()


def complete(client, **options):
    stream = events(client, **options)
    while True:
        try:
            next(stream)
        except StopIteration as done:
            return done.value
