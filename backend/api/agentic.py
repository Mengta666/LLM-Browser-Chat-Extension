"""Agentic Loop 实现（对齐 Anthropic agent loop）。

循环调用 LLM 直到不再请求工具，支持 Tool Clearing（零 LLM 成本）压缩上下文。
"""
import copy
import json
import time
import itertools
from types import SimpleNamespace
from typing import Any, Generator
from search.tools import parse_tool_arguments, web_search_guidance
from openai import APIConnectionError, APITimeoutError, APIStatusError
from agent.memory.chat_context import ContextBudgetError
from agent.memory.history_tools import HISTORY_TOOL_NAMES, TurnHistoryBudget
from api.evidence import EvidenceLedger
from search.kb_documents import KB_TOOL_NAMES, TurnKBBudget
from search.excerpts import TurnWebBudget, COUNT_MODE as EXCERPT_COUNT_MODE
from search.context import fit_request, is_context_rejection, render_evidence, sync_source_context
from agent.token_utils import RequestTokenCounter
from storage import history_index
from storage import kb_runtime
from search import kb_task
from search import kb_read_queue
from search import kb_workspace
from search.kb_coverage import covered_chars
from api import final_answer, model_call, kb_compaction
from agent.memory import context_selection, config as C
from agent.memory import document_analysis

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
    attempt=0,
    history_budget=None,
    final_system_parts=None,
    document_enabled=False,
    document_continuing=False,
) -> Generator[dict, None, None]:
    """
    Agentic loop: 循环调用 LLM 直到不再请求工具（对齐 Anthropic）。

    Args:
        model: 模型名
        messages: 初始对话历史
        tools: 可用工具列表 [WEB_SEARCH_TOOL, KB_SEARCH_TOOL]
        max_rounds: 最大轮数（默认用全局配置）
        chat_id: 用于日志
        stream: 前端响应模式；内部流式接收，完整决策后才执行工具。

    Yields:
        {"type": "enhancement_step", "step": {...}}  # 工具调用进度
        {"type": "final", "content": "...", "messages": [...]}  # 最终回答
        {"type": "error", "content": "..."}  # 错误
    """
    explicit_rounds = max_rounds
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
    context_retry_used = False
    decision_retry_used = False
    if deadline is None and tools:
        deadline = time.monotonic() + max(1, CHAT_LLM_TIMEOUT - 10)
    counter = counter or RequestTokenCounter(model, deadline=deadline,
                                             chat_template_kwargs=C.chat_reasoning_kwargs(model))
    history_budget = history_budget or (TurnHistoryBudget(chat_id, history_upto_seq, counter=counter)
                                       if chat_id and history_upto_seq is not None else None)
    archived = history_index.tool_runs(chat_id, request_id) if request_id and attempt else []
    kb_enabled = bool(bound_kb_id and any(t['function']['name'] in KB_TOOL_NAMES for t in tools))
    separate_kb_answer = kb_enabled and bool(C.kb_answer_settings(model))
    kb_budget = TurnKBBudget(counter, archived, kb_runtime.load(chat_id, request_id)
                             if kb_enabled and request_id and attempt else None)
    def persist_kb():
        if kb_enabled and request_id and attempt:
            kb_runtime.checkpoint(chat_id, request_id, attempt, kb_budget.state)
    def model_permit(phase):
        if not kb_enabled:
            return True
        if not kb_budget.call_allowed(phase):
            kb_budget.state['stop_reason'] = 'model_call_limit'
            persist_kb()
            return False
        kb_budget.state['model_calls'] += 1
        persist_kb()
        return True
    if kb_enabled:
        tools = copy.deepcopy(tools)
        if kb_budget.task_mode:
            tools.append(copy.deepcopy(kb_task.TOOL))
        for tool in tools:
            if tool['function']['name'] == 'kb_read_document':
                tool['function']['parameters']['properties']['targets']['maxItems'] = kb_budget.settings['READ_MAX_TARGETS']
            if kb_budget.task_mode and tool['function']['name'] in ('kb_search','kb_read_document','kb_get_document_outline'):
                tool['function']['parameters']['properties']['need_ids'] = copy.deepcopy(kb_task.NEED_IDS)
        persist_kb()
    document_available = document_enabled and not any(r['name'] == document_analysis.NAME and r['result'] for r in archived)
    evidence = []
    for record in archived:
        if not record['result']:
            continue
        step = json.loads(record['step_json'])
        all_steps.append(step)
        if record['status'] == 'reused':
            continue
        if record['name'] in KB_TOOL_NAMES:
            try:
                kb_budget.revalidate(record)
            except (ValueError, LookupError):
                record = {**record, 'arguments': '{}',
                          'result': '{"outcome":"unavailable","notice":"原证据已不可访问，不能复用"}'}
                step['sources'] = []
                if record['name'] == kb_task.NAME:
                    step['kb_needs'] = []
        evidence.append(record)
        restored_id = f'restored_{record["attempt"]}_{record["ordinal"]}'
        working_messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': restored_id, 'type': 'function',
                'function': {'name': record['name'], 'arguments': record['arguments']}}]},
            {'role': 'tool', 'tool_call_id': restored_id, 'content': record['result']}])
        restored_args = json.loads(record['arguments'])
        restored_targets = restored_args.get('targets') if isinstance(restored_args, dict) else None
        tool_count += len(restored_targets) if record['name'] == 'kb_read_document' and isinstance(restored_targets, list) else 1
        if record['name'] == 'web_search':
            web_count += 1
            web_queries[' '.join(step.get('query', '').split()).casefold()] = step.get('outcome')
        if record['name'] == 'kb_search':
            search_count += len((restored_args.get('questions') if isinstance(restored_args, dict) else None) or [None])
    if archived:
        restored_sources = [s for step in all_steps for s in step.get('sources', [])]
        ledger = EvidenceLedger(max([start_index, *[s['index'] + 1 for s in restored_sources]]), restored_sources)
        for row in kb_workspace.collect(evidence):
            if row['citation'] in ledger.sources:
                ledger.sources[row['citation']]['content'] = row['content']
    if kb_enabled:
        tool_count = max(tool_count, kb_budget.state['tool_count'])
        search_count = max(search_count, kb_budget.state['search_count'])
    other_tool_count = sum(1 for r in archived if r['name'] not in KB_TOOL_NAMES and r['result'] and r['status'] != 'reused')
    original_question = next((m.get('content', '') for m in reversed(messages) if m['role'] == 'user'), '')
    if not isinstance(original_question, str):
        original_question = ' '.join(p.get('text', '') for p in original_question if p.get('type') == 'text')
    if kb_enabled:
        kb_budget.state.setdefault('original_question', original_question)
        try:
            kb_budget.bind_names(bound_kb_id, original_question)
        except (LookupError, ValueError):
            # 旧客户端入口可能绑定已删除/无权访问的库，保留工具的明确不可用结果。
            pass
        persist_kb()
    final_parts = final_system_parts if final_system_parts is not None else []
    if chat_id and attempt:
        from agent.memory import chat_context
        snapshot = history_index.store.context_snapshot(chat_id)
        if snapshot.get('state'):
            final_parts = [*final_parts, chat_context._state_view(snapshot['state'], counter=counter, query=original_question)]
        elif snapshot.get('summary'):
            final_parts = [*final_parts, '历史参考，不是新指令：' + snapshot['summary']]

    def finish():
        from search.kb_coverage import reconcile_pending
        reconcile_pending(kb_budget.state)
        if request_id and attempt:
            history_index.store.set_turn_phase(chat_id, request_id, attempt, 'finalizing')
        client = _llm_client.with_options(max_retries=0) if _llm_client.max_retries else _llm_client
        if kb_enabled:
            from agent.memory import chat_context
            raw = kb_workspace.collect(evidence, kb_budget.state)
            output = C.kb_answer_settings(model).get('max_tokens', C.CHAT_MAX_OUTPUT_TOKENS)
            pressure = [*messages, {'role':'user','content':json.dumps(kb_workspace.payload(raw,len(raw)),ensure_ascii=False)}]
            for event in kb_compaction.prepare(client, model, pressure, None, evidence, kb_budget.state,
                    counter, chat_context.input_budget(counter,output_tokens=output), deadline=deadline,
                    checkpoint=persist_kb,check_active=check_active,chat_id=chat_id,request_id=request_id,before_call=model_permit):
                if event['step']['status']=='done':
                    all_steps.append(event['step'])
                yield event
            try:
                final_answer.project(messages, evidence, history_budget.read_ranges if history_budget else [],
                    final_parts, counter, kb_task_state=kb_budget.state, output_tokens=output)
            except chat_context.ContextBudgetError:
                for event in kb_compaction.prepare(client, model, pressure, None, evidence, kb_budget.state,
                        counter, chat_context.input_budget(counter,output_tokens=output), deadline=deadline,
                        checkpoint=persist_kb,check_active=check_active,chat_id=chat_id,request_id=request_id,
                        before_call=model_permit,force=True):
                    if event['step']['status']=='done':
                        all_steps.append(event['step'])
                    yield event
                try:
                    final_answer.project(messages,evidence,history_budget.read_ranges if history_budget else [],
                        final_parts,counter,kb_task_state=kb_budget.state,output_tokens=output)
                except chat_context.ContextBudgetError:
                    persist_kb()
                    yield {'type':'error','code':'kb_required_evidence_overflow',
                        'content':'必要资料整理后仍超出模型输入窗口，未删掉关键材料或发送超限请求。原文、读取过程与整理检查点已保存；请缩小本次问题范围或分项提问。',
                        'steps':all_steps}
                    return
        if kb_enabled:
            progress = {'type': 'kb_progress', 'status': 'done', 'kb_id': bound_kb_id,
                        'kb_pending': kb_budget.state['pending'], 'kb_stop_reason': kb_budget.state['stop_reason'],
                        'kb_model_calls': kb_budget.state['model_calls'],
                        'kb_needs': kb_task.public(kb_budget.state),
                        'kb_read_coverage': kb_read_queue.report(kb_budget.state),
                        'kb_execution_mode': 'task' if kb_budget.task_mode else 'bounded',
                        'kb_workspace':kb_runtime.workspace_progress(kb_budget.state),
                        'kb_remaining_tokens': None if kb_budget.task_mode else kb_budget.remaining}
            from search.kb_coverage import public_goals
            progress['kb_goals'] = public_goals(kb_budget.state)
            all_steps.append(progress)
            yield {'type': 'enhancement_step', 'step': progress}
            persist_kb()
        for event in final_answer.generate(client, model, messages, evidence,
            history_budget.read_ranges if history_budget else [], final_parts, counter, deadline, all_steps,
            check_active=check_active, prepare_model_messages=prepare_model_messages, allow_partial=allow_partial,
            clock=time.monotonic, chat_id=chat_id, request_id=request_id, before_call=model_permit,
            kb_goals=public_goals(kb_budget.state) if kb_enabled else (),
            kb_task_state=kb_budget.state if kb_enabled and kb_budget.task_mode else None, kb_answer=kb_enabled):
            if kb_enabled:
                progress['kb_model_calls'] = kb_budget.state['model_calls']
                if event['type'] == 'error' and not kb_budget.call_allowed('final'):
                    event = {**event, 'code': 'kb_model_call_limit',
                             'content': '本请求模型调用额度已用尽，已保留工具过程。重试不会重置额度；如需继续检索，请发起新请求。'}
            if event['type'] == 'final' and kb_enabled:
                if kb_budget.task_mode:
                    kb_budget.state['visible_citations'] = kb_task.visible_citations(event['messages'])
                    persist_kb()
                warning = final_answer.citation_warning(event['content'], event['messages'])
                if warning:
                    all_steps.append(warning)
                    yield {'type': 'enhancement_step', 'step': warning}
            yield event
    runtime_guidance = ''
    base_system = working_messages[0]['content'] if working_messages and working_messages[0]['role'] == 'system' else None
    normal_rounds = min(kb_budget.settings['DECISION_MAX_ROUNDS'], max(0, explicit_rounds - 1)) if explicit_rounds is not None else kb_budget.settings['DECISION_MAX_ROUNDS']
    loop_rounds = normal_rounds + kb_budget.settings['RECOVERY_MAX_ROUNDS'] if kb_enabled else max_rounds
    task_mode = kb_enabled and kb_budget.task_mode
    for round_idx in (itertools.count() if task_mode else range(loop_rounds)):
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
            working_messages = _clear_old_tool_results(working_messages, keep=AGENTIC_KEEP_RECENT_TOOLS,
                protected=KB_TOOL_NAMES if kb_enabled else ())
            _chat_log.info("agentic_tool_cleared", session_id=chat_id, data={
                "round": round_idx + 1,
                "old_count": old_count,
                "new_count": len(working_messages),
                "tokens_before": tokens_est
            })

        # 达到限制后切换独立正文阶段，不继续携带工具运行指令。
        recovery_round = kb_enabled and not task_mode and (kb_budget.state['decisions'] >= normal_rounds or kb_budget.state.get('needs_recovery'))
        recovery_allowed = (kb_budget.needs_recovery and
                            kb_budget.state['recoveries'] < kb_budget.settings['RECOVERY_MAX_ROUNDS'] and
                            kb_budget.state['stop_reason'] != 'no_progress')
        is_last_round = ((recovery_round and not recovery_allowed) or not kb_budget.call_allowed('decision')) if kb_enabled else round_idx >= max_rounds - 1
        if task_mode:
            needs = kb_budget.state.get('needs', [])
            assessed = bool(needs) and all(n['status'] in ('ready','unresolved') for n in needs) and not kb_read_queue.pending(kb_budget.state)
            is_last_round = assessed or kb_budget.state.get('no_progress_rounds', 0) >= kb_budget.settings['NO_PROGRESS_ROUNDS']
            if is_last_round:
                kb_budget.state['stop_reason'] = 'needs_assessed' if assessed else kb_budget.state.get('last_stall_reason', 'no_progress')
        final_reserve = min(final_answer.RESERVE_SECONDS, CHAT_LLM_TIMEOUT) if tools else 0
        finishing = deadline is not None and deadline - time.monotonic() <= final_reserve
        call_tools = None if is_last_round or (not task_mode and tool_count >= 12) or finishing else [
            t for t in tools if (t['function']['name'] != 'web_search' or web_count < 3)
            and (not recovery_round or t['function']['name'] in KB_TOOL_NAMES)
            and (t['function']['name'] != 'kb_search' or task_mode or search_count < 8)
            and (not task_mode or t['function']['name'] in KB_TOOL_NAMES or other_tool_count < 12)
            and (t['function']['name'] not in KB_TOOL_NAMES or kb_budget.remaining >= 80)
            and (t['function']['name'] != document_analysis.NAME or document_available)
            and (t['function']['name'] not in HISTORY_TOOL_NAMES or (history_budget and history_budget.available))]
        force_search = require_web_search and web_count == 0
        force_plan = task_mode and not kb_budget.state.get('needs') and not force_search
        if force_plan:
            call_tools = [t for t in (call_tools or []) if t['function']['name'] == kb_task.NAME]
        elif task_mode and kb_budget.state.get('needs'):
            for tool in call_tools or []:
                if tool['function']['name'] == kb_task.NAME:
                    item = tool['function']['parameters']['properties']['needs']['items']
                    item['required'] = list(dict.fromkeys([*(k for k in item['required'] if k not in ('question','evidence_kind')), 'id', 'answer_points']))
                    item['properties']['id']['enum'] = [n['id'] for n in kb_budget.state['needs']]
        if force_search:
            call_tools = [t for t in (call_tools or []) if t['function']['name'] == 'web_search']
            if not call_tools:
                yield {'type': 'error', 'code': 'required_search_not_completed',
                       'content': '本轮要求联网，但搜索尚未执行且工具预算已用尽。', 'steps': all_steps}
                return
        call_tools = call_tools or None
        if not call_tools:
            if kb_enabled:
                kb_budget.state['stop_reason'] = kb_budget.state['stop_reason'] or (
                    'time_reserve' if finishing else 'tool_limit' if tool_count >= 12 else
                    'token_budget' if kb_budget.remaining < 80 else 'decision_limit')
            yield from finish()
            return
        allowed_tools = {tool["function"]["name"] for tool in (call_tools or [])}
        notes = []
        if kb_enabled:
            if task_mode:
                kb_budget.state['visible_citations'] = kb_task.visible_citations(working_messages)
                kb_budget.state['visible_outline_refs'] = kb_task.visible_outline_refs(working_messages)
            notes.append(kb_budget.guidance())
            if separate_kb_answer and task_mode:
                notes.append('本阶段只选择和执行工具，不直接撰写最终正文。材料足够时调用 kb_update_task 记录回答要点并评估需求；'
                             '无法补齐时标为 unresolved 并如实说明缺项。随后服务端进入独立回答阶段；不要为进入该阶段伪造 ready。')
            if force_plan:
                notes.append('当前尚未读取知识库。请先调用 kb_update_task，仅依据当前问题列出最少必要的独立需求，状态均为 open；'
                             '保持用户要求的回答深度，不额外增加逐字公式、独立验算、完整证明或通读背景等目标。'
                             '查找位置、读取正文是行动，不单独建成回答需求。不要现在回答或捏造来源。')
            if recovery_round:
                notes.append('本次仅用于补读或修复上述客观缺口；不要扩展新任务。')
        if history_budget and not history_budget.available:
            notes.append('本轮历史回查额度已用尽，不再调用历史工具；其他工具是否可用以本次 tools 为准。')
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

        # 完整接收决策后才执行工具；单次超时与整轮截止时间分开计算。
        automatic_read = (kb_read_queue.next_read(kb_budget.state, bound_kb_id, kb_budget.settings['READ_MAX_TARGETS'])
                          if task_mode and 'kb_read_document' in allowed_tools else None)
        scheduled_read = bool(automatic_read)
        started = time.monotonic()
        progress_before = (covered_chars(kb_budget.state['ranges']), len(kb_budget.state['pending']), len(kb_budget.state.get('navigation', [])))
        task_progress_before = kb_task.progress_snapshot(kb_budget.state)
        argument_errors = False
        if kb_enabled and not scheduled_read:
            kb_budget.state['recoveries' if recovery_round else 'decisions'] += 1
        try:
            extra = {}
            if force_search:
                extra['tool_choice'] = {'type': 'function', 'function': {'name': 'web_search'}}
            elif separate_kb_answer and task_mode:
                extra['tool_choice'] = 'required'
            if enforce_budget or bound_kb_id or web_enabled:
                from agent.memory import chat_context, config
                if kb_enabled:
                    compact_client = _llm_client.with_options(max_retries=0) if _llm_client.max_retries else _llm_client
                    for event in kb_compaction.prepare(compact_client, model, working_messages, call_tools,
                            evidence, kb_budget.state, counter, chat_context.input_budget(counter),deadline=deadline,
                            checkpoint=persist_kb,check_active=check_active,chat_id=chat_id,request_id=request_id,before_call=model_permit):
                        if event['step']['status']=='done':
                            all_steps.append(event['step'])
                        yield event
                    working_messages, workspace_event = kb_workspace.decision_view(
                        working_messages, call_tools, evidence, kb_budget.state, counter,
                        chat_context.input_budget(counter))
                    if workspace_event:
                        kb_budget.state['visible_citations'] = kb_task.visible_citations(working_messages)
                        kb_budget.state['visible_outline_refs'] = kb_task.visible_outline_refs(working_messages)
                        notes[0] = kb_budget.guidance()
                        runtime_guidance = '\n'.join(notes)
                        working_messages[0] = {**working_messages[0], 'content': (base_system+'\n\n' if base_system else '')+runtime_guidance}
                        persist_kb()
                        _chat_log.info('kb_workspace_selected', session_id=chat_id,
                            data={'request_id':request_id, **workspace_event})
                tokens, removed = fit_request(working_messages, call_tools, web_contexts, counter=counter)
                _chat_log.info('agentic_context_budget', session_id=chat_id, data={
                    'request_id': request_id, 'round': round_idx + 1, 'input_tokens': tokens,
                    'input_limit': chat_context.input_budget(counter), **counter.last,
                    'removed_sources': removed, 'web_used': reader_budget.used, 'web_remaining': reader_budget.remaining})
                extra['max_tokens'] = config.CHAT_MAX_OUTPUT_TOKENS
            client = _llm_client.with_options(max_retries=0) if deadline is not None and _llm_client.max_retries else _llm_client
            for transport_trial in range(2):
                if automatic_read:
                    break
                try:
                    if check_active:
                        check_active()
                    sent = prepare_model_messages(working_messages) if prepare_model_messages else working_messages
                    measured = counter(sent, call_tools)
                    if enforce_budget and measured > chat_context.input_budget(counter):
                        raise ContextBudgetError('context_budget_exceeded')
                    if not model_permit('decision'):
                        yield from finish()
                        return
                    resp = model_call.complete(client,
                        model=model, messages=sent, tools=call_tools,
                        timeout=C.CHAT_LLM_FIRST_RESPONSE_TIMEOUT, idle_timeout=C.CHAT_LLM_IDLE_TIMEOUT,
                        deadline=deadline - final_reserve if deadline is not None else None,
                        clock=time.monotonic, check_active=check_active,
                        phase='decision_retry' if transport_trial else 'decision', chat_id=chat_id,
                        request_id=request_id, input_tokens=measured, count_meta=dict(counter.last),
                        **extra,
                    )
                    break
                except APITimeoutError as exc:
                    if getattr(exc, 'received_progress', False) or transport_trial or decision_retry_used or (deadline is not None
                            and deadline - time.monotonic() < final_reserve + min(60, CHAT_LLM_TIMEOUT)):
                        raise
                    last_user = max(i for i, m in enumerate(working_messages) if m['role'] == 'user')
                    if any(m.get('tool_calls') or m['role'] == 'tool' for m in working_messages[:last_user]):
                        raise
                    recent = context_selection.select_recent(working_messages[1:last_user], counter,
                        total=C.CHAT_RECENT_TOKENS // 4, per_message=C.CHAT_RECENT_MESSAGE_TOKENS // 4,
                        query=original_question)
                    candidate = [working_messages[0], *recent, *working_messages[last_user:]]
                    if counter(candidate, call_tools) >= counter(working_messages, call_tools):
                        raise
                    working_messages = candidate
                    decision_retry_used = True
                except APIStatusError as exc:
                    if (transport_trial or context_retry_used or not web_contexts or not is_context_rejection(exc)
                            or (deadline is not None and deadline - time.monotonic() <= final_reserve)):
                        raise
                    before = counter(working_messages, call_tools)
                    fit_request(working_messages, call_tools, web_contexts, target=max(0, int(before * .8)), counter=counter)
                    context_retry_used = True
                    _chat_log.warn('agentic_context_reduced_retry', session_id=chat_id,
                                   data={'request_id': request_id, 'round': round_idx + 1, **counter.last})
        except Exception as exc:
            incomplete = isinstance(exc, history_index.store.SessionError) and exc.code == 'model_response_incomplete'
            if isinstance(exc, history_index.store.SessionError) and not incomplete:
                raise
            _chat_log.error("agentic_llm_failed", session_id=chat_id, data={
                "round": round_idx + 1, "error_type": type(exc).__name__,
            })
            if (isinstance(exc, APITimeoutError) or incomplete) and (deadline is None or deadline > time.monotonic()):
                yield from finish()
                return
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
        if automatic_read:
            msg = SimpleNamespace(content='', tool_calls=[])
            finish_reason = 'tool_calls'
            _chat_log.info('kb_queued_read_dispatched', session_id=chat_id, data={
                'request_id':request_id, 'targets':len(automatic_read['targets']), 'origin':'selected_read_queue'})
        else:
            msg = resp.choices[0].message
            finish_reason = resp.choices[0].finish_reason
            _chat_log.info('agentic_generation_finished', session_id=chat_id, data={
                'request_id': request_id,
                'round': round_idx + 1, 'finish_reason': finish_reason,
                'max_output_tokens': extra.get('max_tokens'), 'output_chars': len(msg.content or ''),
                'elapsed_s': round(time.monotonic() - started, 3),
                'usage': {k: getattr(resp.usage, k, None) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')} if resp.usage else None})
        if finish_reason == "length":
            _chat_log.warn("agentic_output_truncated", session_id=chat_id,
                           data={"round": round_idx + 1})
            if not force_search and not msg.tool_calls and not final_answer.usable_content(msg):
                yield from finish()
                return
            if separate_kb_answer and not force_search and not msg.tool_calls:
                yield from finish()
                return
            if allow_partial and not force_search and not msg.tool_calls and (msg.content or '').strip():
                yield {'type': 'final', 'content': msg.content, 'finish_reason': 'length',
                       'messages': working_messages, 'steps': all_steps}
            else:
                yield {"type": "error", "code": "model_output_truncated",
                       "content": "模型输出已截断，回答未完成", "steps": all_steps}
            return
        pending_calls = [tc.model_dump() for tc in (msg.tool_calls or [])]
        if automatic_read:
            pending_calls = [{'id':f'kb_read_queue_{round_idx}', 'type':'function',
                              'function':{'name':'kb_read_document','arguments':json.dumps(automatic_read,ensure_ascii=False)}}]
        if force_search and not any(tc['function']['name'] == 'web_search' for tc in pending_calls):
            yield {'type': 'error', 'code': 'required_search_not_called',
                   'content': '模型未执行本轮要求的联网搜索，未将未核实回答作为搜索结果输出。', 'steps': all_steps}
            return

        # 模型停止只是候选答案；已选择的完整读取不能因停止或 ready 丢失。
        if not pending_calls and task_mode and final_answer.usable_content(msg):
            queued = kb_read_queue.next_read(kb_budget.state, bound_kb_id, kb_budget.settings['READ_MAX_TARGETS'])
            if queued:
                kb_budget.state['answer_draft'] = msg.content
                persist_kb()
                if deadline is not None and deadline - time.monotonic() <= final_reserve:
                    kb_budget.state['stop_reason'] = 'time_reserve_with_unread'
                    yield from finish()
                    return
                scheduled_read = True
                pending_calls = [{'id':f'kb_read_queue_{round_idx}', 'type':'function',
                    'function':{'name':'kb_read_document','arguments':json.dumps(queued,ensure_ascii=False)}}]
                _chat_log.info('kb_completion_deferred', session_id=chat_id, data={
                    'request_id':request_id,'missing_scope_count':len(kb_read_queue.pending(kb_budget.state)),
                    'targets':len(queued['targets']), 'origin':'selected_read_queue'})

        # 4. 无 tool_call → 最终回答，结束循环
        if not pending_calls:
            final_content = msg.content or ""
            if separate_kb_answer:
                yield from finish()
                return
            if not final_answer.usable_content(msg):
                yield from finish()
                return
            if kb_enabled and not task_mode and kb_budget.needs_recovery and recovery_allowed and kb_budget.remaining >= 80:
                if recovery_round:
                    kb_budget.state['stop_reason'] = 'no_progress'
                else:
                    kb_budget.state['needs_recovery'] = True
                persist_kb()
                if not recovery_round:
                    continue
            if kb_enabled:
                progress = {'type':'kb_progress','status':'done','kb_id':bound_kb_id,
                            'kb_read_coverage':kb_read_queue.report(kb_budget.state),
                            'kb_needs':kb_task.public(kb_budget.state),
                            'kb_stop_reason':kb_budget.state['stop_reason']}
                all_steps.append(progress)
                yield {'type':'enhancement_step','step':progress}
                warning = final_answer.citation_warning(final_content, sent)
                if warning:
                    all_steps.append(warning)
                    yield {'type': 'enhancement_step', 'step': warning}
            yield {"type": "final", "content": final_content, "finish_reason": finish_reason,
                   "messages": working_messages, "steps": all_steps}
            return

        # 5. 有 tool_call → 执行所有工具
        # 追加 LLM 决策（assistant 消息）
        working_messages.append({
            "role": "assistant",
            "content": '服务端补读已选择但未交付完的范围。' if scheduled_read else msg.content or "",
            "tool_calls": pending_calls
        })

        for tc in pending_calls:
            tool_name = tc["function"]["name"]
            tool_args_str = tc["function"]["arguments"]
            tool_id = tc["id"]
            ordinal = len(archived) + len(all_steps)

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
                tool_cost = len(args_dict.get('targets', args_dict.get('citations', []))) if tool_name == 'kb_read_document' else 1
                if ((not task_mode and (tool_count + tool_cost > 12 or search_count + cost > 8))
                        or (task_mode and tool_name not in KB_TOOL_NAMES and other_tool_count + tool_cost > 12)):
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
                if tool_name in KB_TOOL_NAMES:
                    if tool_cost > kb_budget.settings['READ_MAX_TARGETS'] and tool_name == 'kb_read_document':
                        raise ValueError(f"targets 最多 {kb_budget.settings['READ_MAX_TARGETS']} 项")
                    if not bound_kb_id or args_dict["kb_id"] != bound_kb_id:
                        error_code = "kb_scope_mismatch"
                        raise ValueError("只能查询当前请求绑定的知识库，请使用绑定的 kb_id")
                    if kb_budget.remaining < 80:
                        raise ValueError('本轮知识库预算已用尽')
                if tool_name in HISTORY_TOOL_NAMES and (not history_budget or not history_budget.available):
                    error_code = 'tool_budget_exceeded'
                    raise ValueError('本轮历史回查不可用或预算已用尽')
            except ValueError as exc:
                argument_errors = True
                error_code = getattr(exc, 'code', error_code)
                error_text = json.dumps({'error': error_code, 'message': str(exc)}, ensure_ascii=False)
                error_meta = {}
                if kb_enabled and tool_name in KB_TOOL_NAMES:
                    kb_budget.record_error(tool_name, tool_args_str, str(exc))
                    from agent.memory import chat_context
                    error_text, error_meta = kb_budget.charge_error(error_code, str(exc),
                        chat_context.input_budget(counter) - counter(working_messages, call_tools) - 256)
                error_step = {
                    "type": tool_name, "tool_call_id": tool_id, "status": "error",
                    "query": "", "kb_id": "", "result_count": 0, "sources": [],
                    "error_code": error_code, "error": str(exc), **error_meta,
                }
                all_steps.append(error_step)
                yield {"type": "enhancement_step", "step": error_step}
                working_messages.append({
                    "role": "tool", "tool_call_id": tool_id,
                    "content": error_text,
                })
                if attempt:
                    history_index.save_tool(chat_id, request_id, attempt, ordinal, tool_id, tool_name,
                        tool_args_str, working_messages[-1]['content'], 'error', error_step,
                        kb_state=kb_budget.state if kb_enabled else None)
                evidence.append({'name': tool_name, 'result': working_messages[-1]['content']})
                continue

            cached = next((r for r in archived if r['name'] == tool_name and r['result']
                           and r['arguments'] == json.dumps(args_dict, ensure_ascii=False)), None)
            if cached and tool_name not in KB_TOOL_NAMES:
                working_messages.append({'role': 'tool', 'tool_call_id': tool_id, 'content': cached['result']})
                cached_step = {**json.loads(cached['step_json']), 'tool_call_id': tool_id, 'reused': True}
                all_steps.append(cached_step)
                if attempt:
                    history_index.save_tool(chat_id, request_id, attempt, ordinal, tool_id, tool_name,
                        json.dumps(args_dict, ensure_ascii=False), cached['result'], 'reused', cached_step)
                yield {'type': 'enhancement_step', 'step': cached_step}
                continue

            query = args_dict.get("query", "")
            kb_id_arg = args_dict.get("kb_id", "")
            tool_count += tool_cost
            if tool_name not in KB_TOOL_NAMES:
                other_tool_count += tool_cost
            search_count += cost
            if kb_enabled:
                kb_budget.state.update(tool_count=tool_count, search_count=search_count)

            # 发送 enhancement_step 事件（running）
            yield {
                "type": "enhancement_step",
                "step": {
                    "type": tool_name,
                    "tool_call_id": tool_id,
                    "status": "running",
                    "query": query,
                    "kb_id": kb_id_arg,
                    "origin": 'selected_read_queue' if scheduled_read else 'model'
                }
            }

            # 执行工具
            if check_active:
                check_active()
            if attempt:
                history_index.save_tool(chat_id, request_id, attempt, ordinal, tool_id, tool_name,
                    json.dumps(args_dict, ensure_ascii=False), step={'type': tool_name, 'tool_call_id': tool_id,
                        'status': 'running', 'query': query,
                        'origin':'selected_read_queue' if scheduled_read else 'model'}, kb_state=kb_budget.state if kb_enabled else None)
                if tool_name in HISTORY_TOOL_NAMES or tool_name == 'kb_read_document':
                    history_index.store.set_turn_phase(chat_id, request_id, attempt, 'reading')
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
            if tool_name in KB_TOOL_NAMES:
                from agent.memory import chat_context
                space = max(0, chat_context.input_budget(counter) - counter(working_messages, call_tools) - 256)
                result_text, sources_meta, result_meta = kb_budget.run(tool_name, args_dict, ledger, space)
                argument_errors |= result_meta.get('error_code', '').startswith(('invalid_tool_arguments', 'kb_citations_'))
                if task_mode and result_meta.get('outcome') == 'budget_exhausted' and space < 80:
                    kb_budget.state['stop_reason'] = 'context_space_exhausted'
                    kb_budget.state['no_progress_rounds'] = kb_budget.settings['NO_PROGRESS_ROUNDS']
                search_results = []
            elif tool_name == document_analysis.NAME:
                from agent.memory import chat_context
                result_cap = max(0, min(2000, chat_context.input_budget(counter) - counter(working_messages, call_tools) - 256))
                if result_cap < 512:
                    result_text, search_results, result_meta = '当前上下文空间不足，未启动全文分析。', [], {'outcome': 'error', 'error_code': 'document_result_budget_exceeded'}
                else:
                    client = _llm_client.with_options(max_retries=0) if _llm_client.max_retries else _llm_client
                    try:
                        result_text, search_results, result_meta = yield from document_analysis.run(args_dict,
                            chat_id=chat_id, request_id=request_id, attempt=attempt, upto_seq=history_upto_seq,
                            client=client, model=model, counter=counter, deadline=deadline - final_reserve,
                            max_result_tokens=result_cap, continuing=document_continuing)
                    except history_index.store.SessionError as exc:
                        if check_active:
                            check_active()
                        result_text, search_results, result_meta = json.dumps({'error': exc.code}), [], {'outcome': 'error', 'error_code': exc.code}
                document_available = False
            else:
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
            if tool_name not in KB_TOOL_NAMES:
                numbered, sources_meta = ledger.register(tool_name, kb_id_arg, search_results or [], query)
            if tool_name == "web_search" and sources_meta:
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
                "result_count": result_meta.get('provided_count', len(search_results) if search_results else 0),
                "sources": sources_meta,
                "origin": 'selected_read_queue' if scheduled_read else 'model',
                **result_meta,
            }
            all_steps.append(done_step)
            evidence.append({'name': tool_name, 'result': result_text})
            if attempt:
                history_index.save_tool(chat_id, request_id, attempt, ordinal, tool_id, tool_name,
                    json.dumps(args_dict, ensure_ascii=False), result_text, done_step['status'], done_step,
                    budget=history_budget if tool_name in HISTORY_TOOL_NAMES else None,
                    kb_state=kb_budget.state if kb_enabled else None)
                history_index.store.set_turn_phase(chat_id, request_id, attempt, 'generating')
            sync_source_context(all_steps, web_contexts)
            yield {"type": "enhancement_step", "step": done_step}

            # 追加工具结果
            working_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": result_text
            })

        # 6. 继续下一轮（循环回到顶部）
        if recovery_round and progress_before == (covered_chars(kb_budget.state['ranges']), len(kb_budget.state['pending']), len(kb_budget.state.get('navigation', []))):
            kb_budget.state['stop_reason'] = 'no_progress'
        if task_mode:
            kb_task.record_progress(kb_budget.state, task_progress_before, argument_errors=argument_errors)
        persist_kb()

    yield from finish()


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


def _clear_old_tool_results(messages: list[dict], keep: int = 3, protected=()) -> list[dict]:
    """
    Tool Clearing（对齐 Anthropic）：清除旧 tool 消息内容，保留最近 keep 个。
    纯字符串操作，零 LLM 调用。

    Args:
        messages: 当前对话历史
        keep: 保留最近 N 个工具结果

    Returns:
        压缩后的 messages
    """
    names = {c['id']:c['function']['name'] for m in messages for c in m.get('tool_calls', [])}
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool" and names.get(m.get('tool_call_id')) not in protected]
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
