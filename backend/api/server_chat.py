"""可靠会话入口；成功落库后才发送完成确认，不复用异步历史写入。"""

import hashlib
import json
import sqlite3

from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from agent.memory import chat_context as context
from agent.memory import config as C
from storage import chat_store as store


def handle(item):
    from api import chat
    if not item.chat_id or not item.request_id or item.expected_last_seq is None:
        return JSONResponse(status_code=422, content={'error': {'code': 'session_fields_required'}})
    if len(item.messages) != 1 or item.messages[0].get('role') != 'user':
        return JSONResponse(status_code=422, content={'error': {'code': 'single_user_message_required'}})
    current = item.messages[0]
    user_text = chat._extract_last_user_text(item.messages)
    if not isinstance(current.get('content'), (str, list)) or not current['content']:
        return JSONResponse(status_code=422, content={'error': {'code': 'empty_input'}})
    digest = hashlib.sha256(json.dumps({'messages': item.messages, 'model': item.model,
        'kb_id': item.kb_id, 'search_query': item.search_query}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        retry_json = json.dumps(item.model_dump(), ensure_ascii=False) if isinstance(current['content'], str) else ''
        turn = store.begin_turn(item.chat_id, item.request_id, digest, item.expected_last_seq, user_text or '[图片]', retry_json)
    except store.SessionError as exc:
        return JSONResponse(status_code=exc.status, content={'error': {'code': exc.code}})
    except Exception:
        return JSONResponse(status_code=503, content={'error': {'code': 'history_unavailable'}})

    def events():
        saved = turn['status'] == 'completed'
        try:
            if saved:
                record = store.get_request(item.chat_id, item.request_id)
                answer = next(m for m in record['messages'] if m['role'] == 'assistant')
                for step in json.loads(answer['tools'] or '[]'):
                    yield {'enhancement_step': step}
                yield {'choices': [{'delta': {'content': answer['content']}, 'index': 0}]}
            else:
                from search.tools import WEB_SEARCH_TOOL, KB_SEARCH_TOOL
                tools = []
                if chat._SEARCH_ON and not item.search_query.strip():
                    tools.append(WEB_SEARCH_TOOL)
                if item.kb_id:
                    tools.append(KB_SEARCH_TOOL)
                parts = [chat._CHAT_BASE_SYSTEM]
                memory = chat._build_memory_system(user_text, chat_id=item.chat_id)
                if memory:
                    parts.append(memory)
                if item.kb_id:
                    from storage import kb_store
                    kb = kb_store.get_kb(item.kb_id)
                    if not kb or kb.get('deleted_at'):
                        raise store.SessionError('kb_not_found', 404)
                    parts.append(f'当前绑定知识库「{kb["name"]}」(id: {item.kb_id})。优先使用 kb_search 查询该库，不混用其他知识库。')
                messages = context.prepare(item.chat_id, current, parts, tools)
                full_text = ''
                steps = []
                if item.search_query.strip():
                    from search.tools import format_search_results
                    yield {'enhancement_step': {'type': 'web_search', 'status': 'running', 'query': item.search_query}}
                    results = chat.search_web(item.search_query.strip()) if chat._SEARCH_ON else []
                    if not results:
                        from search.searxng import search_searxng
                        results = search_searxng(item.search_query.strip())
                    step = {'type': 'web_search', 'status': 'done', 'query': item.search_query,
                            'result_count': len(results), 'sources': [
                                {'index': i + 1, 'title': result.title, 'url': result.url,
                                 'snippet': (result.snippet or '')[:200]} for i, result in enumerate(results)]}
                    steps.append(step)
                    yield {'enhancement_step': step}
                    messages.append({'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': 'manual_search', 'type': 'function', 'function': {
                            'name': 'web_search', 'arguments': json.dumps({'query': item.search_query})}}]})
                    messages.append({'role': 'tool', 'tool_call_id': 'manual_search', 'content': format_search_results(results)})
                    context.check_budget(messages, tools)
                if tools:
                    from api.agentic import run_agentic_loop
                    for event in run_agentic_loop(item.model, messages, tools, chat_id=item.chat_id,
                                                  stream=item.stream, enforce_budget=True,
                                                  start_index=1 + sum(len(step.get('sources', [])) for step in steps)):
                        if event['type'] == 'enhancement_step':
                            yield {'enhancement_step': event['step']}
                        elif event['type'] == 'error':
                            raise RuntimeError('model_or_tool_failed')
                        elif event['type'] == 'final':
                            full_text = event['content']
                            steps.extend(event.get('steps', []))
                            yield {'choices': [{'delta': {'content': full_text}, 'index': 0}]}
                else:
                    response = chat._llm_client.chat.completions.create(model=item.model, messages=messages,
                        stream=item.stream, timeout=chat.CHAT_LLM_TIMEOUT, max_tokens=C.CHAT_MAX_OUTPUT_TOKENS)
                    if item.stream:
                        try:
                            for chunk in response:
                                if chunk.choices:
                                    if chunk.choices[0].finish_reason == 'length':
                                        raise store.SessionError('model_output_truncated', 502)
                                    text = chunk.choices[0].delta.content or ''
                                    full_text += text
                                    if text:
                                        yield {'choices': [{'delta': {'content': text}, 'index': 0}]}
                        finally:
                            response.close()
                    else:
                        if response.choices[0].finish_reason == 'length':
                            raise store.SessionError('model_output_truncated', 502)
                        full_text = response.choices[0].message.content or ''
                        yield {'choices': [{'delta': {'content': full_text}, 'index': 0}]}
                if not full_text.strip():
                    raise RuntimeError('empty_answer')
                record = store.complete_turn(item.chat_id, item.request_id, turn['attempt'], full_text, steps)
                saved = True
                try:
                    chat._schedule_memory_write(user_text, full_text, item.chat_id)
                except Exception:
                    pass
            yield {'session_meta': {'chat_id': item.chat_id, 'request_id': item.request_id,
                   'status': 'completed', 'persisted': True, 'last_seq': record['last_seq'],
                   'user_seq': record['user_seq'], 'assistant_seq': record['assistant_seq']},
                   'choices': [{'delta': {}, 'index': 0, 'finish_reason': 'stop'}]}
        except Exception as exc:
            code = exc.code if isinstance(exc, store.SessionError) else (
                'context_budget_exceeded' if isinstance(exc, context.ContextBudgetError) else
                'history_unavailable' if isinstance(exc, sqlite3.Error) else 'turn_failed')
            try:
                if not saved:
                    store.fail_turn(item.chat_id, item.request_id, turn['attempt'], code)
            except Exception:
                code = 'history_unavailable'
            yield {'error': {'code': code}, 'session_meta': {'chat_id': item.chat_id,
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
    return JSONResponse(content={'choices': [{'message': {'role': 'assistant', 'content': answer}}],
                                 'session_meta': meta, 'enhancement_steps': steps})
