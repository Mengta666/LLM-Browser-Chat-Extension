"""Agentic Loop 实现（对齐 Anthropic agent loop）。

循环调用 LLM 直到不再请求工具，支持 Tool Clearing（零 LLM 成本）压缩上下文。
"""
import json
import time
from typing import Any, Generator
from search.tools import parse_tool_arguments, web_search_guidance
from openai import APIConnectionError, APITimeoutError, APIStatusError
from agent.memory.chat_context import ContextBudgetError
from agent.memory.history_tools import HISTORY_TOOL_NAMES, TurnHistoryBudget
from api.evidence import EvidenceLedger
from search.excerpts import TurnWebBudget, COUNT_MODE as EXCERPT_COUNT_MODE
from search.context import fit_request, is_context_rejection, render_evidence, sync_source_context
from agent.token_utils import RequestTokenCounter

from api.chat import (
    _llm_client,
    CHAT_LLM_TIMEOUT,
    AGENTIC_MAX_ROUNDS,
    AGENTIC_TOOL_CLEAR_THRESHOLD,
    AGENTIC_KEEP_RECENT_TOOLS,
    _chat_log,
)


def run_agentic_loop(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict],
    *,
    max_rounds: int = None,
    chat_id: str = "",
    stream: bool = False,
    enforce_budget: bool = False,
    start_index: int = 1,
    bound_kb_id: str = "",
    initial_sources: list[dict] | None = None,
    allow_partial: bool = False,
    request_id: str = '',
    require_web_search: bool = False,
    deadline: float | None = None,
    history_upto_seq: int | None = None,
    check_active=None,
    counter=None,
    prepare_model_messages=None,
) -> Generator[dict, None, None]:
    """
    Agentic loop: 循环调用 LLM 直到不再请求工具（对齐 Anthropic）。

    Args:
        model: 模型名
        messages: 初始对话历史
        tools: 可用工具列表 [WEB_SEARCH_TOOL, KB_SEARCH_TOOL]
        max_rounds: 最大轮数（默认用全局配置）
        chat_id: 用于日志
        stream: 是否流式（暂不支持流式，正文整段返回）

    Yields:
        {"type": "enhancement_step", "step": {...}}  # 工具调用进度
        {"type": "final", "content": "...", "messages": [...]}  # 最终回答
        {"type": "error", "content": "..."}  # 错误
    """
    if max_rounds is None:
        max_rounds = AGENTIC_MAX_ROUNDS

    working_messages = list(messages)
    web_enabled = any(t['function']['name'] == 'web_search' for t in tools)
    if web_enabled:
        guidance = web_search_guidance()
        if require_web_search:
            guidance += '\n用户要求本轮联网：必须先结合上下文调用 web_search，再根据检索结果回答。'
        if working_messages and working_messages[0]['role'] == 'system':
            working_messages[0] = {**working_messages[0], 'content': working_messages[0]['content'] + '\n\n' + guidance}
        else:
            working_messages.insert(0, {'role': 'system', 'content': guidance})
    ledger = EvidenceLedger(start_index, initial_sources)
    all_steps = []  # 收集所有工具调用步骤(供持久化)
    tool_count, search_count = 0, 0
    web_count = 0
    web_queries = {}
    reader_cache = {}
    reader_budget = TurnWebBudget()
    web_contexts = {}
    history_budget = TurnHistoryBudget(chat_id, history_upto_seq) if chat_id and history_upto_seq is not None else None
    context_retry_used = False
    if deadline is None and tools:
        deadline = time.monotonic() + max(1, CHAT_LLM_TIMEOUT - 10)
    counter = counter or RequestTokenCounter(model, deadline=deadline)
    original_question = next((m.get('content', '') for m in reversed(messages) if m['role'] == 'user'), '')
    if not isinstance(original_question, str):
        original_question = ' '.join(p.get('text', '') for p in original_question if p.get('type') == 'text')

    empty_retry_used = False
    runtime_guidance = ''
    base_system = working_messages[0]['content'] if working_messages and working_messages[0]['role'] == 'system' else None
    for round_idx in range(max_rounds + 1):
        if round_idx == max_rounds and not empty_retry_used:
            break
        if check_active:
            check_active()
        if deadline is not None and time.monotonic() >= deadline:
            yield {"type": "error", "code": "time_budget_exceeded", "content": "本轮生成时间预算已用尽", "steps": all_steps}
            return
        _chat_log.debug("agentic_round", session_id=chat_id, data={"round": round_idx + 1})

        # 1. Tool Clearing: token 超限时清除旧 tool_result（零 LLM）
        tokens_est = counter(working_messages, tools)
        if tokens_est > AGENTIC_TOOL_CLEAR_THRESHOLD:
            old_count = len(working_messages)
            working_messages = _clear_old_tool_results(working_messages, keep=AGENTIC_KEEP_RECENT_TOOLS)
            _chat_log.info("agentic_tool_cleared", session_id=chat_id, data={
                "round": round_idx + 1,
                "old_count": old_count,
                "new_count": len(working_messages),
                "tokens_before": tokens_est
            })

        # 2. 最后一轮强制不给 tools（防死循环，逼 LLM 出文本）
        is_last_round = (round_idx >= max_rounds - 1)
        final_reserve = min(30, max(1, CHAT_LLM_TIMEOUT / 3)) if tools else 0
        finishing = deadline is not None and deadline - time.monotonic() <= final_reserve
        call_tools = None if empty_retry_used or is_last_round or tool_count >= 12 or finishing else [
            t for t in tools if (t['function']['name'] != 'web_search' or web_count < 3)
            and (t['function']['name'] != 'kb_search' or search_count < 8)
            and (t['function']['name'] not in HISTORY_TOOL_NAMES or (history_budget and history_budget.available))]
        force_search = require_web_search and web_count == 0
        if force_search:
            call_tools = [t for t in (call_tools or []) if t['function']['name'] == 'web_search']
            if not call_tools:
                yield {'type': 'error', 'code': 'required_search_not_completed',
                       'content': '本轮要求联网，但搜索尚未执行且工具预算已用尽。', 'steps': all_steps}
                return
        call_tools = call_tools or None
        allowed_tools = {tool["function"]["name"] for tool in (call_tools or [])}
        notes = []
        if history_budget and not history_budget.available:
            notes.append('本轮历史回查额度已用尽，不再调用历史工具；其他工具是否可用以本次 tools 为准。')
        if not call_tools:
            reason = ('空正文补救' if empty_retry_used else '时间预留' if finishing else
                      '轮数上限' if is_last_round else '次数上限' if tool_count >= 12 else '无可用工具')
            notes.append(f'进入最终回答阶段（{reason}）。禁止再请求工具，必须在 content 中直接回答用户问题。'
                         '依据已获得的内容说明结论；不足时明确已读范围、缺项和不能确认的部分。'
                         '预算耗尽不等于没有相关信息，未读部分不得声称已核实。')
        if notes and history_budget:
            notes.append('服务端记录的本轮历史读取范围（字符左闭右开；记录被工具清理不代表其正文仍可见）：' +
                         json.dumps(history_budget.read_ranges, ensure_ascii=False))
        note = '\n'.join(notes)
        if note != runtime_guidance:
            content = (base_system + '\n\n' if base_system is not None else '') + note
            if base_system is None and not runtime_guidance:
                working_messages.insert(0, {'role': 'system', 'content': content})
            else:
                working_messages[0] = {**working_messages[0], 'content': content}
            runtime_guidance = note

        # 3. 调用 LLM（非流式，中间轮需判断 tool_calls）
        started = time.monotonic()
        try:
            extra = {}
            if force_search:
                extra['tool_choice'] = {'type': 'function', 'function': {'name': 'web_search'}}
            if enforce_budget or bound_kb_id or web_enabled:
                from agent.memory import chat_context, config
                tokens, removed = fit_request(working_messages, call_tools, web_contexts, counter=counter)
                _chat_log.info('agentic_context_budget', session_id=chat_id, data={
                    'request_id': request_id, 'round': round_idx + 1, 'input_tokens': tokens,
                    'input_limit': chat_context.input_budget(counter), **counter.last,
                    'removed_sources': removed, 'web_used': reader_budget.used, 'web_remaining': reader_budget.remaining})
                extra['max_tokens'] = config.CHAT_MAX_OUTPUT_TOKENS
            client = _llm_client.with_options(max_retries=0) if deadline is not None and _llm_client.max_retries else _llm_client
            for attempt in range(2):
                try:
                    if check_active:
                        check_active()
                    resp = client.chat.completions.create(
                        model=model, messages=prepare_model_messages(working_messages) if prepare_model_messages else working_messages, tools=call_tools, stream=False,
                        timeout=max(.1, deadline - time.monotonic() - (final_reserve if call_tools else 0)) if deadline is not None else CHAT_LLM_TIMEOUT,
                        **extra,
                    )
                    break
                except APIStatusError as exc:
                    if (attempt or context_retry_used or not web_contexts or not is_context_rejection(exc)
                            or (deadline is not None and deadline - time.monotonic() <= final_reserve)):
                        raise
                    before = counter(working_messages, call_tools)
                    fit_request(working_messages, call_tools, web_contexts, target=max(0, int(before * .8)), counter=counter)
                    context_retry_used = True
                    _chat_log.warn('agentic_context_reduced_retry', session_id=chat_id,
                                   data={'request_id': request_id, 'round': round_idx + 1, **counter.last})
        except Exception as exc:
            _chat_log.error("agentic_llm_failed", session_id=chat_id, data={
                "round": round_idx + 1, "error_type": type(exc).__name__,
            })
            if isinstance(exc, ContextBudgetError):
                code, content = "context_budget_exceeded", "上下文超过模型输入预算"
            elif is_context_rejection(exc):
                code, content = 'context_budget_exceeded', '模型服务拒绝了上下文长度，请核对实际窗口和预留额度。'
            elif isinstance(exc, APITimeoutError):
                code, content = "model_timeout", "模型调用超时"
            elif isinstance(exc, APIConnectionError):
                code, content = "model_connection_error", "无法连接模型服务"
            elif (isinstance(exc, APIStatusError) and exc.status_code in (400, 422)
                  and any(isinstance(m.get('content'), list) and any(p.get('type') == 'chat_attachment' for p in m['content']) for m in working_messages)):
                code, content = 'image_model_request_failed', '模型未能处理图片请求，请检查视觉能力、图片地址访问及模型输入额度；附件已保留。'
            elif (force_search and isinstance(exc, APIStatusError) and exc.status_code in (400, 422)
                  and any(word in str(exc).lower() for word in ('tool_choice', 'tool choice', 'function calling'))):
                code, content = 'search_tool_unsupported', '当前模型接口不支持所需的搜索工具调用，请检查模型服务的工具调用配置。'
            else:
                code, content = "model_call_failed", "模型调用失败"
            yield {"type": "error", "code": code, "content": content, "steps": all_steps}
            return

        if check_active:
            check_active()
        sync_source_context(all_steps, web_contexts)
        msg = resp.choices[0].message
        _chat_log.info('agentic_generation_finished', session_id=chat_id, data={
            'request_id': request_id,
            'round': round_idx + 1, 'finish_reason': resp.choices[0].finish_reason,
            'max_output_tokens': extra.get('max_tokens'), 'output_chars': len(msg.content or ''),
            'elapsed_s': round(time.monotonic() - started, 3),
            'usage': {k: getattr(resp.usage, k, None) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')} if resp.usage else None})
        if resp.choices[0].finish_reason == "length":
            _chat_log.warn("agentic_output_truncated", session_id=chat_id,
                           data={"round": round_idx + 1})
            if allow_partial and not force_search and not msg.tool_calls and (msg.content or '').strip():
                yield {'type': 'final', 'content': msg.content, 'finish_reason': 'length',
                       'messages': working_messages, 'steps': all_steps}
            else:
                yield {"type": "error", "code": "model_output_truncated",
                       "content": "模型输出已截断，回答未完成", "steps": all_steps}
            return
        pending_calls = [tc.model_dump() for tc in (msg.tool_calls or [])]
        if force_search and not any(tc['function']['name'] == 'web_search' for tc in pending_calls):
            yield {'type': 'error', 'code': 'required_search_not_called',
                   'content': '模型未执行本轮要求的联网搜索，未将未核实回答作为搜索结果输出。', 'steps': all_steps}
            return

        # 4. 无 tool_call → 最终回答，结束循环
        if not pending_calls:
            final_content = msg.content or ""
            if not final_content.strip():
                remaining = deadline - time.monotonic() if deadline is not None else CHAT_LLM_TIMEOUT
                if not empty_retry_used and resp.choices[0].finish_reason == 'stop' and remaining >= 5:
                    empty_retry_used = True
                    _chat_log.warn('agentic_empty_answer_retry', session_id=chat_id,
                                   data={'request_id': request_id, 'round': round_idx + 1, 'remaining_s': round(remaining, 3)})
                    continue
                yield {"type": "error", "code": "empty_model_answer",
                       "content": "模型未生成可用正文，本轮回答未完成；未将空回答保存为成功，也未重复执行工具。", "steps": all_steps}
                return
            yield {"type": "final", "content": final_content, "finish_reason": resp.choices[0].finish_reason,
                   "messages": working_messages, "steps": all_steps}
            return

        if empty_retry_used:
            yield {'type': 'error', 'code': 'final_answer_unavailable',
                   'content': '模型在收尾补救时仍请求工具，未生成可用回答；没有重复执行工具。', 'steps': all_steps}
            return

        # 5. 有 tool_call → 执行所有工具
        # 追加 LLM 决策（assistant 消息）
        working_messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": pending_calls
        })

        for tc in pending_calls:
            tool_name = tc["function"]["name"]
            tool_args_str = tc["function"]["arguments"]
            tool_id = tc["id"]

            error_code = "invalid_tool_arguments"
            try:
                if tool_name not in allowed_tools:
                    error_code = "tool_unavailable"
                    raise ValueError("本轮工具不可用，请使用允许的工具或直接回答")
                args_dict = parse_tool_arguments(tool_name, tool_args_str)
                if deadline is not None and deadline - time.monotonic() <= final_reserve:
                    error_code = "tool_budget_exceeded"
                    raise ValueError("本轮时间预算已用尽")
                cost = len(args_dict.get("questions") or [args_dict.get("query")]) if tool_name == "kb_search" else 0
                if tool_count >= 12 or search_count + cost > 8:
                    error_code = "tool_budget_exceeded"
                    raise ValueError("本轮工具预算已用尽，请只使用已有证据并说明缺失项")
                if tool_name == 'web_search':
                    if web_count >= 3 or deadline - time.monotonic() <= final_reserve:
                        error_code = 'tool_budget_exceeded'
                        raise ValueError('本轮搜索预算已用尽，请根据已有结果回答并说明未核实部分')
                    query_key = ' '.join(args_dict['query'].split()).casefold()
                    if query_key in web_queries and web_queries[query_key] != 'error':
                        error_code = 'duplicate_search'
                        raise ValueError('相同查询已执行，请使用已有结果或更换搜索词')
                if tool_name in ("kb_search", "kb_list_documents"):
                    if not bound_kb_id or args_dict["kb_id"] != bound_kb_id:
                        error_code = "kb_scope_mismatch"
                        raise ValueError("只能查询当前请求绑定的知识库，请使用绑定的 kb_id")
                if tool_name in HISTORY_TOOL_NAMES and (not history_budget or not history_budget.available):
                    error_code = 'tool_budget_exceeded'
                    raise ValueError('本轮历史回查不可用或预算已用尽')
            except ValueError as exc:
                error_step = {
                    "type": tool_name, "tool_call_id": tool_id, "status": "error",
                    "query": "", "kb_id": "", "result_count": 0, "sources": [],
                    "error_code": error_code, "error": str(exc),
                }
                all_steps.append(error_step)
                yield {"type": "enhancement_step", "step": error_step}
                working_messages.append({
                    "role": "tool", "tool_call_id": tool_id,
                    "content": json.dumps({"error": error_code, "message": str(exc)}, ensure_ascii=False),
                })
                continue

            query = args_dict.get("query", "")
            kb_id_arg = args_dict.get("kb_id", "")
            tool_count += 1
            search_count += cost

            # 发送 enhancement_step 事件（running）
            yield {
                "type": "enhancement_step",
                "step": {
                    "type": tool_name,
                    "tool_call_id": tool_id,
                    "status": "running",
                    "query": query,
                    "kb_id": kb_id_arg
                }
            }

            # 执行工具
            if check_active:
                check_active()
            options = {'timeout': max(.1, deadline - time.monotonic() - final_reserve)} if tool_name == 'web_search' else {}
            if tool_name == 'web_search':
                options['reader_cache'] = reader_cache
                options['reader_budget'] = reader_budget
                from agent.memory import chat_context
                used_tokens = counter(working_messages, call_tools)
                options['reader_context_tokens'] = max(0, min(reader_budget.remaining, chat_context.input_budget(counter)
                    - used_tokens - 2500))
            elif tool_name in HISTORY_TOOL_NAMES:
                from agent.memory import chat_context
                options['history_budget'] = history_budget
                used_tokens = counter(working_messages, call_tools)
                options['history_context_tokens'] = max(0, chat_context.input_budget(counter)
                    - used_tokens - 256)
            result_text, search_results, result_meta = _execute_tool(tool_name, args_dict, start_index=ledger.next_index, **options)
            if check_active:
                check_active()
            if tool_name == 'web_search':
                web_count += 1
                web_queries[query_key] = result_meta.get('outcome')
                _chat_log.info('web_search_finished', session_id=chat_id, data={
                    'request_id': request_id, 'original_question': original_question[:200],
                    'query': query, 'attempt': web_count, 'result_count': len(search_results), **result_meta})

            # 构建搜索结果元数据(供前端渲染引用面板)
            numbered, sources_meta = ledger.register(tool_name, kb_id_arg, search_results or [], query)
            if tool_name == "kb_search" and numbered:
                from search.tools import _format_kb_chunks
                result_meta["retrievals"] = [{**r, "source_indices": list(dict.fromkeys(
                    c["citation_index"] for c in numbered if c.get("question", query) == r["question"]))}
                    for r in result_meta.get("retrievals", [])]
                unique = {c["citation_index"]: c for c in numbered}
                result_text = json.dumps(result_meta, ensure_ascii=False) + "\n检索仅覆盖以下局部窗口，不证明全文覆盖。\n" + _format_kb_chunks(list(unique.values()))
            elif tool_name == "web_search" and sources_meta:
                web_contexts[tool_id] = (numbered, result_meta)
                result_text = render_evidence(numbered, result_meta)
                candidate = working_messages + [{'role': 'tool', 'tool_call_id': tool_id, 'content': result_text}]
                try:
                    fit_request(candidate, call_tools, web_contexts, counter=counter)
                except ContextBudgetError:
                    yield {'type': 'error', 'code': 'context_budget_exceeded',
                           'content': '基础会话与工具信息已超过输入预算，未继续调用模型。', 'steps': all_steps}
                    return
                result_text = candidate[-1]['content']
                _chat_log.info('web_context_selected', session_id=chat_id, data={
                    'request_id': request_id, 'tool_call_id': tool_id,
                    'used': reader_budget.used, 'remaining': reader_budget.remaining, 'count_mode': EXCERPT_COUNT_MODE,
                    'sources': [{'index': r['citation_index'], 'original_chars': r.get('content_length', 0),
                        'selected_chars': len(r.get('content', '')), 'selected_blocks': r.get('selected_blocks', 0),
                        'context_tokens': r.get('context_tokens', 0), 'context_status': r.get('context_status'),
                        'structure_mode': r.get('structure_mode')} for r in numbered]})

            # 发送 enhancement_step 事件（done）+ 收集到 all_steps
            done_step = {
                "type": tool_name,
                "tool_call_id": tool_id,
                "status": "error" if result_meta.get("outcome") == "error" else "done",
                "query": query,
                "kb_id": kb_id_arg,
                "result_count": len(search_results) if search_results else 0,
                "sources": sources_meta,
                **result_meta,
            }
            all_steps.append(done_step)
            sync_source_context(all_steps, web_contexts)
            yield {"type": "enhancement_step", "step": done_step}

            # 追加工具结果
            working_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": result_text
            })

        # 6. 继续下一轮（循环回到顶部）

    # 7. 达到 max_rounds 仍未结束 → 强制终止
    _chat_log.warn("agentic_max_rounds_reached", session_id=chat_id, data={"max_rounds": max_rounds})
    yield {"type": "error", "code": "max_rounds_exceeded", "content": f"达到最大轮数 {max_rounds}，强制结束", "steps": all_steps}


def _execute_tool(tool_name: str, args_dict: dict, start_index: int = 1, *, timeout: float | None = None,
                  reader_cache=None, reader_context_tokens=None, reader_budget=None,
                  history_budget=None, history_context_tokens=0) -> tuple[str, list, dict]:
    """执行单个工具，返回正文、引用片段和结构化业务状态。"""
    if tool_name == "web_search":
        from search.tools import execute_web_search
        return execute_web_search(args_dict['query'], timeout=timeout, reader_cache=reader_cache,
                                  reader_context_tokens=reader_context_tokens, reader_budget=reader_budget)
    elif tool_name in ("kb_search", "kb_list_documents"):
        from search.tools import execute_kb_tool
        return execute_kb_tool(tool_name, args_dict, start_index=start_index)
    elif tool_name in HISTORY_TOOL_NAMES and history_budget:
        try:
            return history_budget.execute(tool_name, args_dict, max_tokens=history_context_tokens)
        except ValueError as exc:
            meta = {'outcome': 'error', 'error_code': 'history_budget_exceeded', 'error': str(exc)}
            return json.dumps(meta, ensure_ascii=False), [], meta
    else:
        return f"未知工具: {tool_name}", [], {"outcome": "error", "error_code": "tool_unavailable", "error": "工具不可用"}


def _clear_old_tool_results(messages: list[dict], keep: int = 3) -> list[dict]:
    """
    Tool Clearing（对齐 Anthropic）：清除旧 tool 消息内容，保留最近 keep 个。
    纯字符串操作，零 LLM 调用。

    Args:
        messages: 当前对话历史
        keep: 保留最近 N 个工具结果

    Returns:
        压缩后的 messages
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep:
        return messages

    to_clear = set(tool_indices[:-keep] if keep else tool_indices)  # 除最近 keep 个
    result = []
    for i, msg in enumerate(messages):
        if i in to_clear:
            result.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": "[工具结果已清除以节省上下文]"
            })
        else:
            result.append(msg)
    return result
