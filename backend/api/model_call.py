"""聊天决策与收尾共用的流式接收；完整结束后才交付工具调用。"""

import hashlib
import json
import queue
import threading
import time

import httpx
from openai import APITimeoutError
from openai.types.chat import ChatCompletion
from agent.memory import config as C
from observability.logger import get_logger
from storage.chat_store import SessionError

_log = get_logger('chat_context')


def fingerprint(messages, tools=None):
    """对消息和工具定义生成稳定摘要，供请求日志和诊断关联使用。"""
    return hashlib.sha256(json.dumps([messages, tools], ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def events(client, *, model, messages, timeout, deadline=None, idle_timeout=None, check_active=None,
             clock=time.monotonic, phase='decision', chat_id='', request_id='', input_tokens=None, count_meta=None,
             kb_answer=False, **options):
    """在线程中接收模型流，区分首包、空闲和总期限超时，完整结束后返回组装响应。"""
    started = clock()
    expires = deadline if deadline is not None else started + timeout
    idle_timeout = timeout if idle_timeout is None else idle_timeout
    if check_active:
        check_active()
    if clock() >= expires:
        raise APITimeoutError(request=httpx.Request('POST', str(client.base_url)))
    policy = C.chat_generation_extra(model, phase, kb_answer=kb_answer)
    if policy:
        extra = dict(options.get('extra_body') or {})
        if policy.get('chat_template_kwargs'):
            extra['chat_template_kwargs'] = {**(extra.get('chat_template_kwargs') or {}), **policy['chat_template_kwargs']}
        extra.update({k:v for k,v in policy.items() if k != 'chat_template_kwargs'})
        options['extra_body'] = extra
    info = {'request_id': request_id, 'phase': phase, 'input_tokens': input_tokens, 'kb_answer': kb_answer,
            'input_fingerprint': fingerprint(messages, options.get('tools')), 'timeout_s': max(0, expires - started),
            'first_response_timeout_s': timeout, 'idle_timeout_s': idle_timeout,
            **(count_meta or {}),
            'reasoning_effort': (options.get('extra_body') or {}).get('chat_template_kwargs', {}).get('reasoning_effort', 'upstream_default'),
            'enable_thinking': (options.get('extra_body') or {}).get('chat_template_kwargs', {}).get('enable_thinking', 'upstream_default'),
            'thinking_token_budget': (options.get('extra_body') or {}).get('thinking_token_budget'),
            'max_output_tokens': options.get('max_tokens')}
    _log.info('model_call_started', session_id=chat_id, data=info)
    inbox = queue.Queue(maxsize=64)
    stopped = threading.Event()
    streams = []

    def send(item):
        """把上游事件放入队列，遵守停止标记和截止时间。"""
        received_at = clock()
        while not stopped.is_set():
            try:
                inbox.put((*item, received_at), timeout=.1)
                return
            except queue.Full:
                pass

    def receive():
        """建立上游流并收集文本、工具参数和用量，向消费线程发送事件或异常。"""
        response = None
        try:
            if stopped.is_set():
                return
            response = client.chat.completions.create(model=model, messages=messages, stream=True,
                stream_options={'include_usage': True},
                timeout=max(.1, min(max(timeout, idle_timeout), expires - clock())), **options)
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
    first_progress = last_progress = None
    reasoning_chars = 0

    def timed_out(kind):
        """构造带超时类型和已接收进度信息的模型超时异常。"""
        error = APITimeoutError(request=httpx.Request('POST', str(client.base_url)))
        error.timeout_kind = kind
        error.received_progress = first_progress is not None
        return error

    thread.start()
    try:
        while True:
            if check_active:
                check_active()
            if clock() >= expires:
                raise timed_out('deadline')
            waiting_until = started + timeout if last_progress is None else last_progress + idle_timeout
            try:
                kind, value, received_at = inbox.get(timeout=.1)
            except queue.Empty:
                if clock() >= waiting_until:
                    raise timed_out('first_response' if last_progress is None else 'idle')
                continue
            # 使用实际接收时间，不让空心跳或消费队列的时间延长有效输出等待。
            if received_at >= waiting_until:
                raise timed_out('first_response' if last_progress is None else 'idle')
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
            tool_progress = any(p.get('id') or p.get('function', {}).get('name')
                                or p.get('function', {}).get('arguments') for p in value['tool_calls'])
            if value['content'] or value['reasoning_chars'] or tool_progress or value['finish_reason']:
                if first_progress is None:
                    first_progress = received_at
                last_progress = received_at
            if value['content']:
                content.append(value['content'])
                yield value['content']
            reasoning_chars += value['reasoning_chars']
            if value['finish_reason']:
                finish = value['finish_reason']
                info['upstream_finish_reason'] = finish
            if value['usage']:
                usage = value['usage']
            for part in value['tool_calls']:
                item = calls.setdefault(part['index'], {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                item['id'] += part.get('id', '')
                for key in ('name', 'arguments'):
                    item['function'][key] += part.get('function', {}).get(key, '')
        # 部分兼容接口以 stop 结束完整工具调用；只规范化已获准且参数完整的调用。
        if calls and finish == 'stop':
            allowed = {t['function']['name'] for t in options.get('tools') or [] if t.get('type') == 'function'}
            choice = options.get('tool_choice')
            if choice == 'none':
                allowed = set()
            elif isinstance(choice, dict):
                allowed &= {choice.get('function', {}).get('name')}
            ids = set()
            for call in calls.values():
                function = call['function']
                if not call['id'] or call['id'] in ids or function['name'] not in allowed:
                    raise SessionError('model_response_incomplete', 502)
                ids.add(call['id'])
                try:
                    arguments = json.loads(function['arguments'])
                except (ValueError, TypeError):
                    raise SessionError('model_response_incomplete', 502) from None
                if not isinstance(arguments, dict):
                    raise SessionError('model_response_incomplete', 502)
            finish = 'tool_calls'
        if not finish or calls and finish not in ('tool_calls', 'length'):
            raise SessionError('model_response_incomplete', 502)
        if calls and finish == 'tool_calls' and any(not c['id'] or not c['function']['name'] for c in calls.values()):
            raise SessionError('model_response_incomplete', 502)
        response = ChatCompletion.model_validate({**metadata, 'object': 'chat.completion', 'usage': usage,
            'generation_diagnostics': {'reasoning_chars': reasoning_chars},
            'choices': [{'index': 0, 'finish_reason': finish, 'message': {'role': 'assistant',
                'content': ''.join(content), 'tool_calls': [calls[i] for i in sorted(calls)] or None}}]})
        _log.info('model_call_completed', session_id=chat_id, data={**info,
            'elapsed_s': round(clock() - started, 3), 'first_response_s': first, 'finish_reason': finish,
            'first_progress_s': None if first_progress is None else round(first_progress - started, 3),
            'last_progress_s': None if last_progress is None else round(last_progress - started, 3),
            'output_chars': len(response.choices[0].message.content or ''), 'reasoning_chars': reasoning_chars, 'usage': usage})
        return response
    except Exception as exc:
        if isinstance(exc, APITimeoutError) and not hasattr(exc, 'timeout_kind'):
            exc.timeout_kind = 'transport'
            exc.received_progress = first_progress is not None
        _log.warn('model_call_failed', session_id=chat_id, data={**info,
            'elapsed_s': round(clock() - started, 3), 'first_response_s': first,
            'first_progress_s': None if first_progress is None else round(first_progress - started, 3),
            'last_progress_s': None if last_progress is None else round(last_progress - started, 3),
            'idle_s': None if last_progress is None else round(clock() - last_progress, 3),
            'timeout_kind': getattr(exc, 'timeout_kind', None), 'output_chars': sum(map(len, content)),
            'error_type': type(exc).__name__, 'error_code': getattr(exc, 'code', None),
            'received_tool_parts': len(calls), 'reasoning_chars': reasoning_chars})
        raise
    finally:
        stopped.set()
        # close 可能等待底层 socket；不让它阻塞停止按钮和会话状态落库。
        if thread.is_alive() and streams:
            threading.Thread(target=streams[0].close, name='chat-stream-close', daemon=True).start()


def complete(client, **options):
    """消费模型流中的文本事件，同步取得完整结束后的响应对象。"""
    stream = events(client, **options)
    while True:
        try:
            next(stream)
        except StopIteration as done:
            return done.value
