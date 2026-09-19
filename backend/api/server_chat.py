"""可靠会话入口；成功落库后才发送完成确认，不复用异步历史写入。"""

import hashlib
import json
import sqlite3
import time

from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from agent.memory import chat_context as context
from agent.memory import config as C
from storage import chat_store as store


def handle(item):
    from api import chat
    deadline = time.monotonic() + max(1, chat.CHAT_LLM_TIMEOUT - 10)
    if not item.chat_id or not item.request_id or item.expected_last_seq is None:
        return JSONResponse(status_code=422, content={'error': {'code': 'session_fields_required'}})
    if len(item.messages) != 1 or item.messages[0].get('role') != 'user':
        return JSONResponse(status_code=422, content={'error': {'code': 'single_user_message_required'}})
    current = item.messages[0]
    user_text = chat._extract_last_user_text(item.messages)
    if not isinstance(current.get('content'), (str, list)) or not current['content']:
        return JSONResponse(status_code=422, content={'error': {'code': 'empty_input'}})
    try:
        if item.continuation_of:
            parent = store.get_request(item.chat_id, item.continuation_of)
            if not parent or parent['status'] != 'partial' or not parent['retry_request']:
                raise store.SessionError('continuation_unavailable')
            original = parent['retry_request']
            item = item.model_copy(update={key: original[key] for key in ('model', 'kb_id', 'search_query')})
        identity = {'messages': item.messages, 'model': item.model,
                    'kb_id': item.kb_id, 'search_query': item.search_query}
        if item.continuation_of:
            identity['continuation_of'] = item.continuation_of
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        retry_json = json.dumps(item.model_dump(), ensure_ascii=False) if isinstance(current['content'], str) else ''
        turn = store.begin_turn(item.chat_id, item.request_id, digest, item.expected_last_seq, user_text or '[图片]', retry_json,
                                continuation_of=item.continuation_of)
    except store.SessionError as exc:
        return JSONResponse(status_code=exc.status, content={'error': {'code': exc.code}})
    except Exception:
        return JSONResponse(status_code=503, content={'error': {'code': 'history_unavailable'}})

    def events():
        saved = turn['status'] in ('completed', 'partial')
        finish_reason = 'stop'
        error_message = ''
        try:
            if saved:
                record = store.get_request(item.chat_id, item.request_id)
                answer = next(m for m in record['messages'] if m['role'] == 'assistant')
                for step in json.loads(answer['tools'] or '[]'):
                    yield {'enhancement_step': step}
                yield {'choices': [{'delta': {'content': answer['content']}, 'index': 0}]}
            else:
                from search.tools import WEB_SEARCH_TOOL, KB_SEARCH_TOOL, KB_LIST_DOCUMENTS_TOOL, KB_TOOL_GUIDANCE
                tools = []
                if chat._SEARCH_ON or item.search_query.strip():
                    tools.append(WEB_SEARCH_TOOL)
                if item.kb_id:
                    tools.extend([KB_SEARCH_TOOL, KB_LIST_DOCUMENTS_TOOL])
                parts = [chat._CHAT_BASE_SYSTEM]
                if item.continuation_of:
                    parts.append('本轮由用户点击继续生成：接着上一条被截断的回答补完，不从头重复。'
                                 '本条独立呈现，代码和公式请组织成可独立阅读的格式。'
                                 '历史编号只属于历史消息，不能沿用为本轮来源；需要引用时使用本轮检索结果编号。')
                memory = chat._build_memory_system(user_text, chat_id=item.chat_id)
                if memory:
                    parts.append(memory)
                if item.kb_id:
                    from storage import kb_store
                    kb = kb_store.get_kb(item.kb_id)
                    if not kb or kb['user_id'] != C.CHAT_USER_ID or kb.get('deleted_at') or kb.get('sync_action'):
                        raise store.SessionError('kb_not_found', 404)
                    parts.append(f'当前绑定知识库「{kb["name"]}」(id: {item.kb_id})。只能查询该库。' + KB_TOOL_GUIDANCE)
                messages = context.prepare(item.chat_id, current, parts, tools)
                full_text = ''
                steps = []
                if tools:
                    from api.agentic import run_agentic_loop
                    for event in run_agentic_loop(item.model, messages, tools, chat_id=item.chat_id,
                                                  stream=item.stream, enforce_budget=True,
                                                  bound_kb_id=item.kb_id,
                                                  allow_partial=True,
                                                  request_id=item.request_id,
                                                  require_web_search=bool(item.search_query.strip()) and not item.continuation_of,
                                                  deadline=deadline):
                        if event['type'] == 'enhancement_step':
                            yield {'enhancement_step': event['step']}
                        elif event['type'] == 'error':
                            code = event.get('code', 'model_or_tool_failed')
                            error_message = event.get('content', '')
                            raise store.SessionError(code, 413 if code == 'context_budget_exceeded' else 502)
                        elif event['type'] == 'final':
                            full_text = event['content']
                            finish_reason = event.get('finish_reason', 'stop')
                            steps.extend(event.get('steps', []))
                            yield {'choices': [{'delta': {'content': full_text}, 'index': 0}]}
                else:
                    started = time.monotonic()
                    usage = None
                    response = chat._llm_client.chat.completions.create(model=item.model, messages=messages,
                        stream=item.stream, timeout=chat.CHAT_LLM_TIMEOUT, max_tokens=C.CHAT_MAX_OUTPUT_TOKENS)
                    if item.stream:
                        try:
                            for chunk in response:
                                if chunk.usage:
                                    usage = chunk.usage
                                if chunk.choices:
                                    if chunk.choices[0].delta.tool_calls:
                                        raise store.SessionError('unexpected_tool_call', 502)
                                    text = chunk.choices[0].delta.content or ''
                                    full_text += text
                                    if text:
                                        yield {'choices': [{'delta': {'content': text}, 'index': 0}]}
                                    if chunk.choices[0].finish_reason:
                                        finish_reason = chunk.choices[0].finish_reason
                        finally:
                            response.close()
                    else:
                        if response.choices[0].message.tool_calls:
                            raise store.SessionError('unexpected_tool_call', 502)
                        usage = response.usage
                        finish_reason = response.choices[0].finish_reason
                        full_text = response.choices[0].message.content or ''
                        yield {'choices': [{'delta': {'content': full_text}, 'index': 0}]}
                    chat._chat_log.info('chat_generation_finished', session_id=item.chat_id, data={
                        'request_id': item.request_id, 'finish_reason': finish_reason,
                        'max_output_tokens': C.CHAT_MAX_OUTPUT_TOKENS, 'output_chars': len(full_text),
                        'elapsed_s': round(time.monotonic() - started, 3),
                        'usage': {k: getattr(usage, k, None) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')} if usage else None})
                if not full_text.strip():
                    raise store.SessionError('empty_model_answer', 502)
                if finish_reason not in ('stop', 'length'):
                    raise store.SessionError('model_response_incomplete', 502)
                record = store.complete_turn(item.chat_id, item.request_id, turn['attempt'], full_text, steps, finish_reason)
                saved = True
                if record['status'] == 'completed' and not item.continuation_of:
                    try:
                        chat._schedule_memory_write(user_text, full_text, item.chat_id)
                    except Exception:
                        pass
            yield {'session_meta': {'chat_id': item.chat_id, 'request_id': item.request_id,
                   'status': record['status'], 'finish_reason': record['finish_reason'] or 'stop',
                   'can_continue': record['can_continue'], 'continuation_of': record['continuation_of'],
                   'persisted': True, 'last_seq': record['last_seq'],
                   'user_seq': record['user_seq'], 'assistant_seq': record['assistant_seq']},
                   'choices': [{'delta': {}, 'index': 0, 'finish_reason': record['finish_reason'] or 'stop'}]}
        except Exception as exc:
            code = exc.code if isinstance(exc, store.SessionError) else (
                'context_budget_exceeded' if isinstance(exc, context.ContextBudgetError) else
                'history_unavailable' if isinstance(exc, sqlite3.Error) else 'turn_failed')
            try:
                if not saved:
                    store.fail_turn(item.chat_id, item.request_id, turn['attempt'], code)
            except Exception:
                code = 'history_unavailable'
            yield {'error': {'code': code, **({'message': error_message} if error_message else {})}, 'session_meta': {'chat_id': item.chat_id,
                   'request_id': item.request_id, 'status': 'failed', 'persisted': False},
                   'choices': [{'delta': {}, 'index': 0, 'finish_reason': 'error'}]}
        finally:
            if not saved:
                try:
                    store.fail_turn(item.chat_id, item.request_id, turn['attempt'], 'stream_interrupted', interrupted=True)
                except Exception:
                    pass

    if item.stream:
        async def stream():
            source = events()
            try:
                async for event in iterate_in_threadpool(source):
                    yield chat._sse(event)
                yield 'data: [DONE]\n\n'
            finally:
                source.close()
                try:
                    store.fail_turn(item.chat_id, item.request_id, turn['attempt'], 'stream_interrupted', interrupted=True)
                except Exception:
                    pass
        return StreamingResponse(stream(), media_type='text/event-stream')
    answer = ''
    steps = []
    meta = None
    for event in events():
        if event.get('error'):
            code = event['error']['code']
            status = 413 if code == 'context_budget_exceeded' else 503 if code == 'history_unavailable' else 502
            return JSONResponse(status_code=status, content=event)
        for choice in event.get('choices', []):
            answer += choice.get('delta', {}).get('content') or ''
        if event.get('enhancement_step'):
            steps.append(event['enhancement_step'])
        meta = event.get('session_meta', meta)
    return JSONResponse(content={'choices': [{'message': {'role': 'assistant', 'content': answer},
                                             'finish_reason': meta['finish_reason']}],
                                 'session_meta': meta, 'enhancement_steps': steps})
