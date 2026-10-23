"""Agent 主循环模块（结构化输出 verify-first 架构，对齐 browser-use）。

每一步：观察（编号元素列表 + 上一步结果）→ 单次 LLM 调用返回一个结构化 JSON：
  { evaluation_previous_goal, memory, next_goal, plan?, current_plan_item?, action }
LLM 必须先自评上一步是否成功（对照观察），再决定下一个动作。
定位契约：索引直连——action.index 对应 data-agent-id，前端直取节点。
编号失效（stale）→ 重新观察再决策，不计失败。
"""

import json
import copy
import os
import re
import time
import threading
from uuid import uuid4
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, RateLimitError, APIConnectionError, APIStatusError

from agent.state import (
    AgentSession, AgentStatus, PageAction, PageState, ActionResult, HistoryItem,
)
from agent.context_builder import (
    SYSTEM_PROMPT, build_messages, build_plan_block, MAX_HISTORY_ITEMS,
)
from agent.router import should_confirm_action
from tools.tool_registry import ALLOWED_ACTION_TYPES
from observability.logger import get_logger

_agent_log = get_logger("agent")

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# 调试：置 AGENT_DEBUG_SCREENSHOT=1 时，把每步观察的截图（含 SoM 编号框）落盘到
# backend/logs/screenshots/ 供人工查看。默认关，零开销（不落盘不解码）。logs/ 已 gitignore。
_DEBUG_SCREENSHOT = os.getenv("AGENT_DEBUG_SCREENSHOT", "").strip() in ("1", "true", "True", "yes")
_SHOT_DIR = Path(__file__).resolve().parents[1] / "logs" / "screenshots"

_llm_client = OpenAI(base_url=MODEL_BASE_URL, api_key=OPENAI_API_KEY, max_retries=0)

_sessions: dict[str, AgentSession] = {}
_sessions_lock = threading.RLock()
# 空闲 TTL：基于 last_activity（每步 /step 刷新）。活跃任务不断刷新→永不误清；
# 只回收真正卡死/被遗弃（很久无 /step）的会话。取代旧的"绝对创建时间 TTL"（会误杀长任务）。
_SESSION_IDLE_TTL = 1800         # 30min 无任何活动 = 死会话，回收（须 ≥ 最慢单步耗时；配合 max_steps=200 长任务，留足单步余量）
_SESSION_DONE_IDLE_TTL = 120     # 已收尾（完成/错误/取消）会话空闲 2min 即回收（更快腾内存）
_SESSION_JANITOR_INTERVAL = 60   # 后台清理线程轮询间隔
MAX_SESSIONS = 200               # dict 容量上限：防高并发/刷 /execute 撑爆内存

MAX_LLM_RETRIES = 3
RETRY_DELAYS = [0.5, 1, 2]
MAX_PARSE_RETRIES = 3   # 结构化输出解析失败（多为网关输出截断）时，重试整次 LLM 调用的上限
MAX_STALE_RETRIES = 3   # 连续编号失效重观察上限，防打转
LLM_CALL_TIMEOUT = 90   # 单次 LLM 调用超时秒数（对齐 browser-use llm_timeout 75-90s，防单点卡死）
COMPACT_TRIGGER_STEPS = MAX_HISTORY_ITEMS
COMPACT_KEEP_RECENT = 8      # 摘要后保留的最近步数（首项 + <摘要> + 最近 N 项）

try:
    import tiktoken
    _tok_enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _tok_enc = None

from agent.token_utils import estimate_tokens as _estimate_tokens_shared


def _cleanup_expired_sessions():
    """回收空闲会话。基于 last_activity（非创建时间）：活跃任务不断刷新，永不被误清。

    两档：已收尾会话空闲 120s 即清（快腾内存）；其余（含 RUNNING）空闲 900s 清（真卡死才回收）。
    调用方必须已持有 _sessions_lock。
    """
    now = time.time()
    done_states = (AgentStatus.COMPLETED, AgentStatus.ERROR, AgentStatus.CANCELLED)
    expired = [
        sid for sid, s in _sessions.items()
        if not s.in_flight and ((now - s.last_activity > _SESSION_DONE_IDLE_TTL and s.status in done_states)
        or (now - s.last_activity > _SESSION_IDLE_TTL))
    ]
    for sid in expired:
        del _sessions[sid]
    return expired


def _evict_if_full():
    """容量到顶时腾位：优先淘汰"空闲最久的已收尾会话"；没有可淘汰的收尾会话则不动。

    调用方必须已持有 _sessions_lock。返回被淘汰的 sid（无则 None）。
    """
    if len(_sessions) < MAX_SESSIONS:
        return None
    done_states = (AgentStatus.COMPLETED, AgentStatus.ERROR, AgentStatus.CANCELLED)
    victims = [(s.last_activity, sid) for sid, s in _sessions.items() if s.status in done_states and not s.in_flight]
    if not victims:
        return None                        # 全在跑：不牺牲活跃会话，交由 create_session 决定拒绝
    victims.sort()                         # 最久未活动的排前
    sid = victims[0][1]
    del _sessions[sid]
    return sid


def _janitor_loop():
    """后台守护线程：定时主动清理，不依赖请求触发（否则无新请求时过期会话一直躺内存）。"""
    while True:
        time.sleep(_SESSION_JANITOR_INTERVAL)
        try:
            with _sessions_lock:
                removed = _cleanup_expired_sessions()
            if removed:
                _agent_log.info("sessions_janitor_cleaned",
                                data={"removed": len(removed), "remaining": len(_sessions)})
        except Exception as e:
            _agent_log.warn("sessions_janitor_error", data={"error": str(e)[:120]})


_janitor_thread = threading.Thread(target=_janitor_loop, daemon=True, name="sessions-janitor")
_janitor_thread.start()


def get_session(session_id: str) -> Optional[AgentSession]:
    """清理过期会话后，按会话标识返回仍保留的任务状态。"""
    with _sessions_lock:
        _cleanup_expired_sessions()
        return _sessions.get(session_id)


def create_session(session_id: str, task: str, model: str,
                   require_confirmation: Optional[list[str]] = None,
                   task_image: str = "",
                   llm_params: Optional[dict[str, Any]] = None, *, kb_id: str = '') -> AgentSession:
    """检查容量并初始化自动化会话、任务工作区和绑定知识库。"""
    with _sessions_lock:
        _cleanup_expired_sessions()
        if session_id in _sessions:
            raise RuntimeError("会话已存在")
        _evict_if_full()
        # 容量仍到顶（全是活跃会话，无可淘汰）→ 拒绝新建，避免无上限撑爆内存
        if len(_sessions) >= MAX_SESSIONS:
            raise RuntimeError(
                f"活跃会话数已达上限 {MAX_SESSIONS}，暂时无法新建，请稍后重试")
        session = AgentSession(
            session_id=session_id, task=task, model=model,
            require_confirmation=require_confirmation or [],
            task_image=task_image or "",
            llm_params=llm_params or {},
            kb_id=kb_id,
        )
        from agent.materials import initial_state
        session.material_state = initial_state()
        _sessions[session_id] = session
    _agent_log.info("session_create", session_id=session_id,
                    data={"task": task, "model": model, "has_image": bool(task_image),
                          "llm_params": llm_params or {},   # 便于排查"关思考"是否真配上
                          "active_sessions": len(_sessions)})
    return session


def cancel_session(session_id: str) -> bool:
    """设置会话取消终态；会话尚未建立时也保存取消记录，阻止迟到启动。"""
    with _sessions_lock:
        session = _sessions.get(session_id)
        if not session:
            session = create_session(session_id, "", "")
        if session.status in (AgentStatus.COMPLETED, AgentStatus.ERROR):
            session.last_activity = time.time()
            return True
        session.cancel_event.set()
        session.status = AgentStatus.CANCELLED
        session.pending_action = None
        session.success = False
        session.last_activity = time.time()
    _agent_log.info("session_cancel", session_id=session_id, data={"step": session.current_step})
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# 主循环：单步（观察 → 结构化 LLM → 一个动作）
# ═══════════════════════════════════════════════════════════════════════════════

def run_step(session: AgentSession, page_state: PageState,
             action_result: Optional[ActionResult] = None,
             force_done: bool = False) -> dict[str, Any]:
    # 模型调用不持锁；只在发布结果时检查取消，防止迟到结果改写终态。
    """在工作副本上推进决策，再受锁保护地发布结果，避免取消被迟到结果覆盖。"""
    with _sessions_lock:
        if session.cancel_event.is_set() or session.status in (AgentStatus.COMPLETED, AgentStatus.ERROR):
            return _build_response(session)
        work = copy.copy(session)
        for name, value in vars(session).items():
            if isinstance(value, (dict, list)) and name not in ('request_digests', 'request_results'):
                setattr(work, name, copy.deepcopy(value))
    _run_step(work, page_state, action_result, force_done)
    with _sessions_lock:
        if not session.cancel_event.is_set():
            if session.decision_deadline and time.monotonic() >= session.decision_deadline:
                work.status = AgentStatus.ERROR
                work.pending_action = None
                work.success = False
                work.error = '模型决策超过时间预算，未发布迟到动作'
            for name, value in vars(work).items():
                if name not in ('cancel_event', 'in_flight', 'active_request_id', 'request_digests', 'request_results'):
                    setattr(session, name, value)
        return _build_response(session)


def _run_step(session: AgentSession, page_state: PageState,
              action_result: Optional[ActionResult] = None,
              force_done: bool = False) -> dict[str, Any]:
    """记录上一步结果 + 调 LLM 出下一个动作。stale 由前端处理（重新观察后再调本函数）。

    force_done=True（前端整轮超时触发）：强制本步只接受 task_complete，给用户一个交代。
    """
    session.last_activity = time.time()   # 刷新活动时间：活跃任务不会被空闲 TTL 误清
    from agent.materials import revalidate
    revalidate(session)

    # 1. 记录上一步结果为一条结构化 HistoryItem（不累积 messages；messages 每步重建）
    if action_result and session.pending_action:
        from agent.interaction import evaluate
        action_result.verification = evaluate(session.pending_action.expectation, page_state)
        from agent.recovery import record as record_recovery
        record_recovery(session, session.pending_action, action_result, page_state)
        from agent.field_reads import record as record_field_read
        record_field_read(session, session.pending_action, action_result)
        from agent.task_workspace import record_receipt
        record_receipt(session, session.pending_action, action_result)
        from agent.materials import record_result
        record_result(session, session.pending_action, action_result)
        if action_result.stale:
            session.stale_retries += 1
            _agent_log.info("step_result", session_id=session.session_id,
                            data={"step": session.current_step, "outcome": "stale",
                                  "stale_retries": session.stale_retries, "url": page_state.url})
            stale_note = ("页面或字段频繁变化，请基于最新观察核实目标和原值"
                          if session.stale_retries >= MAX_STALE_RETRIES
                          else "请基于新观察核实目标和完整原值；不要重放旧动作，已有资料保留")
            if session.stale_retries >= MAX_STALE_RETRIES:
                session.stale_retries = 0
            session.history_items.append(HistoryItem(
                step=session.current_step,
                action=_action_summary(session.pending_action, page_state),
                result=f"↻ {(action_result.error or action_result.details or '观察已失效')[:160]}；{stale_note}"))
        else:
            session.stale_retries = 0
            changes = action_result.state_changes or {}
            _agent_log.info("step_result", session_id=session.session_id,
                            data={"step": session.current_step,
                                  "outcome": "ok" if action_result.success else "fail",
                                  "details": (action_result.details or "")[:80],
                                  "error": (action_result.error or "")[:80],
                                  "url": page_state.url, "changes": changes,
                                  "action_id": action_result.action_id, "observation_id": page_state.observation_id,
                                  "error_code": action_result.error_code,
                                  "click_effects": action_result.click_effects,
                                  "click_diagnostic": action_result.click_diagnostic,
                                  "control_verification": action_result.control_verification,
                                  "verification": action_result.verification})
            session.history_items.append(HistoryItem(
                step=session.current_step,
                evaluation=session.last_evaluation,
                memory=session.last_memory,
                next_goal=session.progress,
                action=_action_summary(session.pending_action, page_state),
                result=_result_summary(action_result, page_state)))
            if session.step_history:
                session.step_history[-1]["result"] = {
                    "success": action_result.success,
                    "details": (action_result.details or "")[:50],
                    "url_after": page_state.url,
                }
        session.pending_action = None

    # 2. 收尾控制（对齐 browser-use _force_done_after_last_step）
    #    硬兜底：真到 max_steps 仍没 done → ERROR。
    if session.current_step >= session.max_steps:
        session.status = AgentStatus.ERROR
        session.error = f"超过最大步数 {session.max_steps}，任务未完成"
        _agent_log.error("max_steps_exceeded", session_id=session.session_id,
                         data={"step": session.current_step,
                               "url": page_state.url, "title": (page_state.title or "")[:60]})
        return _build_response(session)
    #    最后一步（步数到上限前一步）或前端超时触发 → 强制只接受 task_complete。
    session.force_done = force_done or (session.current_step >= session.max_steps - 1)

    from agent.field_review import process_pending
    if not session.force_done and process_pending(session, page_state, _llm_client):
        if session.cancel_event.is_set():
            return _build_response(session)
        session.current_step += 1
        session.pending_action = None
        session.material_continuation = uuid4().hex
        session.material_observation_id = page_state.observation_id
        session.status = AgentStatus.OBSERVATION_REQUIRED
        session.progress = '字段语义审核已返回；仅检查内容，未执行填写'
        return _build_response(session)

    # 3~5 决策循环：一轮出一个动作即返回（页面动作下发前端 / task_complete 收尾）。
    material_repairs = 0
    while True:
        # 3. compaction（对齐 browser-use maybe_compact_messages）：步数很多时把中间段 LLM 总结成一条摘要，
        #    保留 首项 + <摘要> + 最近若干项。超长任务防止滑动窗口丢失中间进展。
        _maybe_compact_history(session)
        if session.cancel_event.is_set():
            return _build_response(session)

        # 4. 每步重建 messages（system + 任务 + 历史块 + 计划 + 当前观察）
        session.messages = build_messages(session, page_state)
        from agent import knowledge
        counter = knowledge.counter_for(session)
        try:
            def compact(prompt, tokens, timeout):
                """按压缩专用输出额度和剩余时间调用模型整理证据。"""
                compact_session = copy.copy(session)
                compact_session.decision_deadline = min(session.decision_deadline or float('inf'), time.monotonic() + timeout)
                return _create_with_retry(_llm_client, session.model, prompt, session=compact_session,
                    overrides={'max_tokens':tokens, 'response_format':{'type':'json_object'}})
            session.messages = knowledge.assemble(session, session.messages, counter,
                                                   compact=None if session.force_done else compact)
            if session.messages is None:
                session.messages = []
                session.pending_action = None
                session.material_continuation = uuid4().hex
                session.material_observation_id = page_state.observation_id
                session.current_step += 1
                session.status = AgentStatus.OBSERVATION_REQUIRED
                session.progress = '正在整理已取得资料，原文仍可回读；重新观察后继续'
                return _build_response(session)
        except ValueError as exc:
            session.status = AgentStatus.ERROR
            session.success = False
            session.error = str(exc)
            return _build_response(session)

        # 5. 调 LLM（结构化 JSON）
        _log_observation(session, page_state)
        _dump_screenshot(session, page_state)   # 调试通道：AGENT_DEBUG_SCREENSHOT=1 时落盘截图
        _log_exec_tokens(session)
        parsed = _call_llm(session)
        if parsed is None or session.cancel_event.is_set():
            return _build_response(session)

        # 5. 消化结构化输出：自评/记忆/意图/计划
        session.last_evaluation = str(parsed.get("evaluation_previous_goal", ""))[:300]
        session.last_memory = str(parsed.get("memory", ""))[:300]
        session.progress = str(parsed.get("next_goal", ""))[:120] or session.progress
        if isinstance(parsed.get("plan"), list) and parsed["plan"]:
            session.plan_items = _sanitize_plan(parsed["plan"])
        if parsed.get("current_plan_item") is not None:
            try:
                session.current_plan_item = int(parsed["current_plan_item"])
            except (ValueError, TypeError):
                pass

        action_obj = parsed.get("action") or {}
        func_name = action_obj.get("type") or action_obj.get("action")

        _agent_log.info("llm_step", session_id=session.session_id,
                        data={"step": session.current_step,
                              "eval": session.last_evaluation[:100],
                              "memory": session.last_memory[:100],
                              "next_goal": session.progress[:80],
                              "action": func_name,
                              "plan_items": len(session.plan_items),
                              "current_plan": session.current_plan_item})

        if not func_name:
            session.status = AgentStatus.ERROR
            session.error = f"LLM 输出缺少 action。回复片段: {json.dumps(parsed, ensure_ascii=False)[:200]}"
            return _build_response(session)

        if func_name in knowledge.TOOLS and not session.force_done:
            try:
                knowledge.run(session, func_name, action_obj.get('arguments', {}), counter,
                              max(0, knowledge.input_limit(session, counter) - counter(build_messages(session, page_state))), page=page_state)
            except (ValueError, TypeError, KeyError, TimeoutError) as exc:
                feedback = knowledge.record_error(session, func_name, action_obj.get('arguments', {}), exc, page=page_state)
                _agent_log.warn('task_tool_failed', session_id=session.session_id,
                    data={'step': session.current_step, 'tool': func_name, 'code': feedback['code'],
                          'part': feedback.get('part'), 'citation': feedback.get('citation'),
                          'same_arguments': feedback.get('same_arguments', False)})
            if session.cancel_event.is_set():
                return _build_response(session)
            session.pending_action = None
            session.material_continuation = uuid4().hex
            session.material_observation_id = page_state.observation_id
            session.current_step += 1
            session.history_items.append(HistoryItem(step=session.current_step, action=func_name,
                result=session.task_state.get('error') or '资料或成果已存入任务工作区；操作结果仍需页面验证',
                next_goal=session.progress))
            session.status = AgentStatus.OBSERVATION_REQUIRED
            session.progress = ('工具未完成，按错误提示修正，已取得资料保留' if session.task_state.get('error')
                                else '任务工作区已更新，重新观察后继续决策')
            return _build_response(session)

        if func_name == "task_complete":
            if action_obj.get('success', True) and not session.force_done and material_repairs < 2:
                from agent.materials import incomplete
                if incomplete(session):
                    material_repairs += 1
                    session.material_state['error'] = ('字段尚未核实。若当前值正确，使用 verify_field 绑定有效成果及原转换规则，'
                        '只读回查后再完成；有 pending_content_repairs 时先按提示整理待填稿，blocked 字段报告未完成。'
                        '若没有足够资料或约束冲突，报告 success=false，不重复输入或猜值。')
                    continue
            session.status = AgentStatus.COMPLETED
            session.summary = action_obj.get("summary", "任务完成")
            session.success = bool(action_obj.get("success", True))
            if (session.material_state.get('fields') or session.task_state.get('field_length_repairs')
                    or session.task_state.get('field_content_repairs') or session.task_state.get('field_review_jobs')):
                from agent.materials import incomplete
                remaining_fields = incomplete(session)
                if remaining_fields:
                    session.success = False
                    session.summary = '物料字段未全部核实：' + '、'.join(remaining_fields) + '。\n' + str(session.summary)
            _agent_log.info("session_complete", session_id=session.session_id,
                            data={"summary": session.summary, "success": session.success,
                                  "total_steps": session.current_step,
                                  "url": page_state.url, "title": (page_state.title or "")[:60]})
            return _build_response(session)

        # 强制收尾时 LLM 仍出页面动作（不听话）→ 代码用其记忆替它体面收尾（对齐 browser-use force-done）
        if session.force_done:
            session.status = AgentStatus.COMPLETED
            session.success = False
            session.summary = (f"已达步数/时间上限，未完全完成任务。最后进展：{session.last_memory[:180]}"
                               if session.last_memory else "已达步数/时间上限，任务未完成。")
            _agent_log.warn("forced_done_at_limit", session_id=session.session_id,
                            data={"step": session.current_step, "wanted_action": func_name,
                                  "url": page_state.url})
            return _build_response(session)

        if func_name not in ALLOWED_ACTION_TYPES:
            session.status = AgentStatus.ERROR
            session.error = f"未知的操作类型: {func_name}"
            return _build_response(session)

        action = _parse_action(func_name, action_obj)
        from agent.interaction import prepare
        try:
            prepare(action, action_obj, page_state)
        except ValueError as exc:
            action.expectation = {'kind': 'invalid', 'reason': str(exc)}
        if (func_name == 'read_field' or session.kb_id or session.material_state.get('fields') or action_obj.get('artifact_id')
                or session.task_state['artifacts'] or session.task_state.get('field_content_repairs')
                or session.task_state.get('field_reads') or session.task_state.get('artifact_errors')):
            from agent.materials import bind_action, FieldSnapshotRequired, FieldLengthExceeded
            from agent.field_content import FieldContentRequired, record_error as record_content_error
            from agent.field_review import ReviewRequired
            try:
                bind_action(session, page_state, action, action_obj)
                session.material_state['error'] = ''
            except ReviewRequired as exc:
                session.material_state['error'] = str(exc)
                session.pending_action = None
                session.material_continuation = uuid4().hex
                session.material_observation_id = page_state.observation_id
                session.current_step += 1
                session.status = AgentStatus.OBSERVATION_REQUIRED
                session.progress = str(exc)
                return _build_response(session)
            except FieldContentRequired as exc:
                feedback = record_content_error(session, exc.feedback)
                session.material_state['error'] = feedback['error']
                _agent_log.warn('field_content_required', session_id=session.session_id,
                    data={k: feedback[k] for k in ('content_repair_id', 'artifact_id', 'index', 'attempts', 'status')})
                session.task_state['records'].append({'name': 'prepare_field_content',
                    'arguments': {'artifact_id': feedback['artifact_id']}, 'result': json.dumps(feedback, ensure_ascii=False),
                    'outcome': feedback['code'], 'step': session.current_step, 'status': 'failed'})
                session.pending_action = None
                session.material_continuation = uuid4().hex
                session.material_observation_id = page_state.observation_id
                session.current_step += 1
                session.history_items.append(HistoryItem(step=session.current_step, action='prepare_field_content',
                    result=feedback['error'] + '；' + feedback['hint']))
                session.status = AgentStatus.OBSERVATION_REQUIRED
                session.progress = ('此字段修稿未通过，保留未完成状态，可继续其他已授权字段' if feedback['status'] == 'blocked'
                                    else '正在按目标字段整理待填内容、检查重复；原资料保留，未执行写入')
                return _build_response(session)
            except FieldLengthExceeded as exc:
                feedback = knowledge.record_length_error(session, exc)
                session.material_state['error'] = feedback['error']
                _agent_log.warn('field_length_exceeded', session_id=session.session_id,
                    data={k: feedback[k] for k in ('artifact_id', 'index', 'max_length', 'actual_length', 'attempts', 'length_repair_id')})
                if feedback['attempts'] >= 3:
                    session.status = AgentStatus.ERROR
                    session.success = False
                    session.error = feedback['error'] + '；模型重复请求同一超长成果，未完成精简，已停止写入。原成果保留。'
                    return _build_response(session)
                session.pending_action = None
                session.material_continuation = uuid4().hex
                session.material_observation_id = page_state.observation_id
                session.current_step += 1
                session.history_items.append(HistoryItem(step=session.current_step, action='prepare_field_value',
                    result=feedback['error'] + '；先生成限长派生成果，再填写；没有执行页面写入。'))
                session.status = AgentStatus.OBSERVATION_REQUIRED
                session.progress = '待填正文超出字段上限，正在按限制精简；原稿和资料保留'
                return _build_response(session)
            except FieldSnapshotRequired as exc:
                key = f'{page_state.document_epoch}:{action.index}'
                attempts = session.task_state.setdefault('snapshot_refreshes', {})
                attempts[key] = attempts.get(key, 0) + 1
                session.material_state['error'] = str(exc)
                if attempts[key] > 1:
                    session.status = AgentStatus.ERROR
                    session.success = False
                    session.error = str(exc)
                    return _build_response(session)
                session.pending_action = None
                session.material_continuation = uuid4().hex
                session.material_observation_id = page_state.observation_id
                session.current_step += 1
                session.status = AgentStatus.OBSERVATION_REQUIRED
                session.progress = '重新获取目标字段完整值快照，已取得资料与成果保留'
                return _build_response(session)
            except ValueError as exc:
                session.material_state['error'] = str(exc)
                material_repairs += 1
                if material_repairs <= 2:
                    continue
                session.status = AgentStatus.ERROR
                session.success = False
                session.error = '物料动作未通过校验：' + str(exc)
                return _build_response(session)
        break  # 页面动作：跳出循环，走下发前端流程

    action.action_id = uuid4().hex
    action.observation_id = page_state.observation_id
    if action.params.get('artifact_id'):
        session.task_state['bindings'][action.action_id] = {
            'action_id': action.action_id, 'artifact_id': action.params['artifact_id'],
            'observation_id': page_state.observation_id, 'document_epoch': page_state.document_epoch,
            'index': action.index, 'status': 'pending'}
    session.pending_action = action
    action.target_label = _describe_target(action.index, page_state)
    from agent.recovery import describe
    session.recovery_state['pending'] = describe(action, page_state)
    session.current_step += 1

    target = _describe_target(action.index, page_state)
    log_data = {
        "request_id": session.active_request_id, "action_id": action.action_id,
        "observation_id": action.observation_id,
        "step": session.current_step, "action": action.type, "index": action.index,
        "target": target, "url": page_state.url, "title": (page_state.title or "")[:60],
        "elements": len(page_state.interactive_elements or []),
    }
    selected = next((el for el in page_state.interactive_elements if el.get('id') == action.index), None)
    if selected:
        log_data['target_evidence'] = {key: selected.get(key) for key in (
            'tag', 'role', 'label_source', 'interaction_source', 'bounding_box', 'target_hint')}
    if action.params.get('material_id'):
        log_data['material_id'] = action.params['material_id']
    elif func_name == "type":
        log_data["text_length"] = len(action.params.get("text", "") or "")
    elif func_name == "select":
        log_data["option"] = action.params.get("option_text", "")
    elif func_name == "press_key":
        log_data["key"] = action.params.get("key", "")
    elif func_name == "scroll":
        log_data["direction"] = action.params.get("direction", "")
    elif func_name == "navigate":
        log_data["nav_url"] = action.params.get("url", "")
    _agent_log.info("execution_result", session_id=session.session_id, data=log_data)
    session.step_history.append(
        {"step": session.current_step, "thought": session.progress, "action": action.model_dump()})

    if action.params.get('confirm_overwrite') or should_confirm_action(action, session.require_confirmation):
        session.status = AgentStatus.CONFIRM_REQUIRED
    else:
        session.status = AgentStatus.ACTION_REQUIRED
    return _build_response(session)


# ═══════════════════════════════════════════════════════════════════════════════
# 历史 compaction（对齐 browser-use maybe_compact_messages）
# ═══════════════════════════════════════════════════════════════════════════════

def _maybe_compact_history(session: AgentSession) -> None:
    """步数很多时，把中间段历史 LLM 总结成一条 compacted_memory，保留 首项 + 摘要 + 最近 N 项。

    仅超长任务触发（COMPACT_TRIGGER_STEPS）。摘要失败则不动历史（滑动窗口兜底）。
    在窗口将省略原文前更新摘要，已有摘要固定保留。
    """
    from agent.task_workspace import history_for_context
    items = history_for_context(session)
    if len(items) <= COMPACT_TRIGGER_STEPS:
        return
    if len(items) > 1 and items[1].step == -1:
        head, mid, recent = items[:1], items[2:-COMPACT_KEEP_RECENT], items[-COMPACT_KEEP_RECENT:]
        prev_summary = items[1].memory
    else:
        head, mid, recent = items[:1], items[1:-COMPACT_KEEP_RECENT], items[-COMPACT_KEEP_RECENT:]
        prev_summary = ""
    if not mid:
        return

    mid_text = "\n".join(it.to_string() for it in mid)
    prompt = (
        "把以下浏览器自动化的中间步骤压缩成一段简短进度摘要（3-5句），"
        "保留：已完成什么、试过哪些无效路径、发现的关键信息（数字/名称/状态）。丢弃冗余细节。\n"
    )
    if prev_summary:
        prompt += f"\n【已有摘要，需合并】\n{prev_summary}\n"
    prompt += f"\n【要压缩的步骤】\n{mid_text}\n\n只输出摘要文本，不要其他。"

    try:
        compact_session = copy.copy(session)
        compact_session.llm_params = {}
        resp = _create_with_retry(
            _llm_client, session.model,
            [{"role": "user", "content": prompt}], session=compact_session)
        summary = (resp.choices[0].message.content or "").strip() if resp else ""
        summary = _strip_think_tags(summary)
    except Exception:
        summary = ""
    if not summary:
        return  # 摘要失败 → 保持原历史，滑动窗口兜底

    compacted = HistoryItem(step=-1, memory=summary[:800],
                            evaluation="", next_goal="", action="", result="（以上为前序步骤摘要）")
    session.history_items = head + [compacted] + recent
    session.task_state['history_summary_after_invalidation'] = True
    _agent_log.info("history_compacted", session_id=session.session_id,
                    data={"compacted_steps": len(mid), "kept_recent": len(recent),
                          "summary_len": len(summary)})


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 调用（结构化 JSON）
# ═══════════════════════════════════════════════════════════════════════════════

def _call_llm(session: AgentSession) -> Optional[dict]:
    """调 LLM，解析出结构化 JSON。失败置 session.error 并返回 None。

    解析层重试：LLM 偶发输出截断（网关流式中断返回半截 JSON）会导致解析失败。
    这类是可自愈的抖动，不该让整个多步任务前功尽弃——重试整次调用，最多 MAX_PARSE_RETRIES 次。
    （网络层 429/超时/5xx 已由 _create_with_retry 内部重试；这里补的是"HTTP 200 但内容非法"。）
    """
    last_raw = ""
    for attempt in range(MAX_PARSE_RETRIES):
        if session.cancel_event.is_set():
            return None
        response = _create_with_retry(_llm_client, session.model, session.messages, session=session)
        if response is None or session.cancel_event.is_set():
            return None            # 网络层重试已耗尽 / 认证等硬错误：session.error 已置
        raw = response.choices[0].message.content or ""
        last_raw = raw
        # 不把原始回复存回 messages——messages 每步由 build_messages 重建，历史只走 history_items
        parsed = _parse_structured(raw)
        if parsed is not None and parsed.get("action"):
            return parsed
        # 解析失败（多为输出截断）→ 记一条 JSONL 便于排查，然后重试整次调用
        _agent_log.warn("llm_parse_failed", session_id=session.session_id,
                        data={"step": session.current_step, "attempt": attempt + 1,
                              "max": MAX_PARSE_RETRIES, "raw_len": len(raw),
                              "raw_tail": _strip_think_tags(raw)[-160:]})

    session.status = AgentStatus.ERROR
    session.error = f"结构化输出连续 {MAX_PARSE_RETRIES} 次解析失败（疑似输出截断）。最后回复: {_strip_think_tags(last_raw)[:300]}"
    _agent_log.error("llm_parse_failed_final", session_id=session.session_id,
                     data={"step": session.current_step, "retries": MAX_PARSE_RETRIES,
                           "raw_len": len(last_raw)})
    return None


def _build_create_kwargs(model: str, messages: list, llm_params: dict) -> dict:
    """把用户在设置面板配的自定义 LLM 参数合并进 chat.completions.create 的调用参数。

    约定:llm_params 里 'extra_body' 键的值合并进 SDK 的 extra_body(供应商私有参数如
    Claude thinking/Qwen enable_thinking/Gemini thinking_config);其余键作为顶层参数
    展开(如 OpenAI reasoning_effort)。保存时前端已做黑名单校验(禁 model/messages 等),
    这里再兜一层防跨版本被绕过。
    """
    FORBIDDEN = {'model', 'messages', 'stream', 'api_key', 'timeout', 'n'}
    kwargs: dict[str, Any] = {'model': model, 'messages': messages, 'timeout': LLM_CALL_TIMEOUT}
    if not llm_params or not isinstance(llm_params, dict):
        return kwargs
    extra = llm_params.get('extra_body') or {}
    if isinstance(extra, dict) and extra:
        kwargs['extra_body'] = dict(extra)
    for k, v in llm_params.items():
        if k == 'extra_body' or k in FORBIDDEN:
            continue
        kwargs[k] = v
    return kwargs


def _create_with_retry(client: OpenAI, model: str, messages: list, session: Optional[AgentSession] = None, *, overrides=None):
    """调用模型并处理可重试失败及参数兼容问题，重试仍受取消信号和决策期限约束。"""
    llm_params = (session.llm_params if session else None) or {}
    create_kwargs = _build_create_kwargs(model, messages, llm_params)
    if session and session.kb_id and not any(k in create_kwargs for k in ('max_tokens', 'max_completion_tokens')):
        from agent.materials import OUTPUT_TOKENS
        create_kwargs['max_tokens'] = OUTPUT_TOKENS
    if overrides:
        if 'max_tokens' in overrides:
            create_kwargs.pop('max_completion_tokens', None)
        create_kwargs.update(overrides)
    last_error = None
    for attempt in range(MAX_LLM_RETRIES):
        if session and session.cancel_event.is_set():
            return None
        if session and session.decision_deadline:
            remaining = session.decision_deadline - time.monotonic()
            if remaining <= 0:
                session.status = AgentStatus.ERROR
                session.error = '模型决策超过时间预算'
                return None
            create_kwargs['timeout'] = min(LLM_CALL_TIMEOUT, remaining)
        try:
            return client.chat.completions.create(**create_kwargs)
        except RateLimitError as e:
            last_error = f"API 限流 (429): {getattr(e, 'message', str(e))}"
        except APITimeoutError:
            last_error = "API 请求超时"
        except APIConnectionError as e:
            last_error = f"网络连接失败: {str(e)[:100]}"
        except APIStatusError as e:
            if e.status_code == 401:
                if session:
                    session.status = AgentStatus.ERROR
                    session.error = "API 认证失败 (401): 请检查 OPENAI_API_KEY"
                return None
            if e.status_code == 404:
                if session:
                    session.status = AgentStatus.ERROR
                    session.error = f"模型不存在 (404): {model}"
                return None
            if e.status_code >= 500:
                last_error = f"API 服务端错误 ({e.status_code})"
            else:
                if session:
                    session.status = AgentStatus.ERROR
                    session.error = f"API 错误 ({e.status_code}): {str(e)[:150]}"
                    return None
                last_error = f"API 错误 ({e.status_code})"
        except Exception as e:
            last_error = f"未知错误: {type(e).__name__}: {str(e)[:100]}"
        if attempt < MAX_LLM_RETRIES - 1:
            if session:
                if session.cancel_event.wait(RETRY_DELAYS[attempt]):
                    return None
            else:
                time.sleep(RETRY_DELAYS[attempt])
    if session:
        session.status = AgentStatus.ERROR
        session.error = f"LLM 调用失败（重试{MAX_LLM_RETRIES}次）: {last_error}"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 解析
# ═══════════════════════════════════════════════════════════════════════════════

def _strip_think_tags(text: str) -> str:
    """移除回复中的思考标签内容，留下可解析的结构化输出。"""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _parse_structured(text: str) -> Optional[dict]:
    """从 LLM 回复中解析结构化 JSON（含 evaluation/memory/next_goal/action 等）。

    容错：优先 ```json 代码块；否则找含 "action" 的最外层 JSON 对象。
    """
    clean = _strip_think_tags(text)
    candidates = []
    for block in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', clean, re.DOTALL):
        candidates.append(block.group(1))
    # 最外层大对象（贪婪）——结构化输出是单个大 JSON
    m = re.search(r'\{.*\}', clean, re.DOTALL)
    if m:
        candidates.append(m.group(0))

    for raw in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        # 兼容：action 可能是对象，也可能被平铺（旧格式 {"action":"click","index":7}）
        if "action" in data:
            if isinstance(data["action"], str):
                # 平铺格式 → 收拢成 action 对象
                atype = data["action"]
                aobj = {k: v for k, v in data.items()
                        if k not in ("action", "evaluation_previous_goal", "memory",
                                     "next_goal", "plan", "current_plan_item", "thought")}
                aobj["type"] = atype
                data["action"] = aobj
            return data
        # 无 action 键但像动作（极端兜底）
        from agent.knowledge import TOOLS
        if data.get("type") in ALLOWED_ACTION_TYPES | TOOLS | {'task_complete'}:
            return {"action": data}
    return None


def _parse_action(func_name: str, args: dict[str, Any]) -> PageAction:
    """将模型动作名称和参数转换为统一的页面动作对象。"""
    index = args.get("index")
    if index is not None:
        try:
            index = int(index)
        except (ValueError, TypeError):
            index = None
    params = {}
    if func_name == "type":
        params["text"] = args.get("text", "")
        params["clear"] = args.get("clear", True)
    elif func_name == "select":
        params["option_text"] = args.get("option_text", "")
    elif func_name == "read_field":
        params = {k: args[k] for k in ('offset', 'limit', 'field_ref') if k in args}
    elif func_name == "scroll":
        params["direction"] = args.get("direction", "down")
        params["amount"] = args.get("amount", 300)
    elif func_name == "press_key":
        params["key"] = args.get("key", "Enter")
        params["modifiers"] = args.get("modifiers", [])
    elif func_name == "wait":
        params["ms"] = min(args.get("ms", 1000), 5000)
    elif func_name == "navigate":
        params["url"] = args.get("url", "")
    return PageAction(type=func_name, index=index, params=params)


def _sanitize_plan(items) -> list[dict]:
    """校验/规整 LLM 的计划：限 ≤10 条，content 截断，status 非法归 pending。"""
    if not isinstance(items, list):
        return []
    out = []
    valid = {"pending", "current", "done", "skipped"}
    for it in items[:10]:
        if isinstance(it, str):
            it = {"content": it, "status": "pending"}
        if not isinstance(it, dict):
            continue
        content = str(it.get("content", "")).strip()[:80]
        if not content:
            continue
        status = it.get("status", "pending")
        if status not in valid:
            status = "pending"
        out.append({"content": content, "status": status})
    return out


def _describe_target(index: Optional[int], page_state: PageState) -> str:
    """把动作的 index 解析成人类可读的目标描述：tag "文本" role。日志诊断用。"""
    if index is None:
        return ""
    for el in (page_state.interactive_elements or []):
        if el.get("id") == index:
            tag = el.get("tag", "?")
            text = (el.get("text") or el.get("aria_label") or el.get("placeholder") or "").strip()[:40]
            role = el.get("role", "")
            parts = [f"<{tag}>"]
            if text:
                parts.append(f'"{text}"')
            if role:
                parts.append(f"role={role}")
            if el.get("occluded"):
                parts.append("[被遮挡]")
            return " ".join(parts)
    return f"(编号{index}不在当前观察中)"


def _action_summary(action: PageAction, page_state: PageState) -> str:
    """把已执行动作压成一行摘要，进 HistoryItem（如 'click [7] 提交'）。"""
    if action is None:
        return ""
    parts = [action.type]
    if action.index is not None:
        parts.append(f"[{action.index}]")
    tgt = action.target_label or _describe_target(action.index, page_state)
    if tgt and not tgt.startswith("(编号"):
        parts.append(tgt.replace("<", "").replace(">", ""))
    p = action.params or {}
    if p.get("text"):
        parts.append(f'输入长度={len(str(p["text"]))}')
    if p.get("option_text"):
        parts.append(f'选"{p["option_text"]}"')
    if p.get("key"):
        parts.append(p["key"])
    if p.get("url"):
        parts.append(str(p["url"])[:50])
    return " ".join(parts)[:120]


def _result_summary(result: ActionResult, page_state: PageState) -> str:
    """把动作结果压成一行摘要，进 HistoryItem。"""
    unconfirmed = (result.verification or {}).get('status') in ('unknown', 'not_satisfied') or (
        result.target_probe is not None and result.target_probe.get('clear') is not True)
    tag = ("⚠" if unconfirmed else "✓") if result.success else "✗"
    bits = [tag]
    if result.details:
        bits.append(str(result.details)[:50])
    if result.error:
        bits.append(f"错误:{str(result.error)[:40]}")
    if result.error_code:
        bits.append(f"原因={result.error_code}")
    if result.click_effects is not None:
        bits.append('点击已发出' if result.click_effects.get('press_sent') else '未发出点击（可能已滚动/悬停）')
    if result.control_verification:
        verification = result.control_verification
        bits.append(f"回读={verification.get('status')}:{verification.get('before')}→{verification.get('after')}")
    if result.verification and result.verification.get('status') != 'not_requested':
        bits.append(f"预期核实={result.verification.get('kind')}:{result.verification.get('status')}")
        if result.verification.get('reason') == 'invalid_expectation':
            bits.append(f"附加验证无效:{result.verification.get('details', '')}")
    if result.target_probe is not None:
        bits.append(f"只读命中检查={result.target_probe.get('clear')}（未点击）")
        if result.target_probe.get('reason'):
            bits.append(f"命中原因={result.target_probe['reason']}")
    ch = result.state_changes or {}
    flags = [k for k in ("url_changed", "popup_appeared", "popup_disappeared") if ch.get(k)]
    if flags:
        bits.append("+".join(flags))
    return " ".join(bits)[:320]


# ═══════════════════════════════════════════════════════════════════════════════
# 埋点
# ═══════════════════════════════════════════════════════════════════════════════

def _estimate_tokens(messages: list) -> dict:
    """调用共享估算器，返回当前消息的文本和图片 token 用量。"""
    return _estimate_tokens_shared(messages)


def _log_observation(session: AgentSession, page_state: PageState) -> None:
    """记录页面观察的规模和主要状态，供排查模型当时看到的内容。"""
    txt = page_state.text_content_summary or ""
    els = page_state.interactive_elements or []
    popup = page_state.active_popup or {}
    _agent_log.info("observation", session_id=session.session_id,
                    data={"step": session.current_step, "url": page_state.url,
                          "request_id": session.active_request_id,
                          "observation_id": page_state.observation_id, "tab_id": page_state.tab_id,
                          "title": (page_state.title or "")[:60], "elements": len(els),
                          "popup": popup.get("type", "") if popup else "",
                          "text_len": len(txt), "text_head": txt[:160],
                          "text_tail": txt[-160:] if len(txt) > 320 else ""})


def _dump_screenshot(session: AgentSession, page_state: PageState) -> None:
    """调试通道：把当前观察截图（data URL）落盘到 logs/screenshots/。仅 _DEBUG_SCREENSHOT 开启时执行。

    文件名：{session_id}_s{step}.{ext}，便于按会话/步数对照 JSONL 日志排查。失败静默（调试功能不阻断主流程）。
    """
    if not _DEBUG_SCREENSHOT:
        return
    shot = getattr(page_state, "screenshot", "") or ""
    if not shot.startswith("data:"):
        return
    try:
        import base64
        header, _, b64 = shot.partition(",")
        if not b64:
            return
        ext = "png" if "image/png" in header else "jpg"
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{session.session_id}_s{session.current_step}.{ext}"
        (_SHOT_DIR / fname).write_bytes(base64.b64decode(b64))
        _agent_log.info("screenshot_dumped", session_id=session.session_id,
                        data={"step": session.current_step, "path": str(_SHOT_DIR / fname)})
    except Exception as e:
        _agent_log.warn("screenshot_dump_failed", session_id=session.session_id,
                        data={"step": session.current_step, "error": str(e)[:120]})


def _log_exec_tokens(session: AgentSession) -> None:
    """记录当前执行上下文的 token 估算结果。"""
    t = _estimate_tokens(session.messages)
    _agent_log.info("step_prompt_tokens", session_id=session.session_id,
                    data={"step": session.current_step, "total": t["total"],
                          "text": t["text"], "images": t["images"], "method": t["method"]})


def _build_response(session: AgentSession, thought: str = "") -> dict[str, Any]:
    """把会话状态整理为前端协议响应，包含待执行动作、成果和恢复反馈。"""
    from agent.task_workspace import public_state as workspace_state
    resp: dict[str, Any] = {
        "protocol_version": 2,
        "session_id": session.session_id,
        "status": session.status.value,
        "step": session.current_step,
        "thought": session.last_evaluation,   # 前端展示：上一步自评
        "action": None,
        "summary": session.summary,
        "success": session.success,            # 完成时是否真正成功（force_done/失败为 False）
        "error": session.error,
        "progress": session.progress,          # 前端展示：next_goal
        "task_workspace": workspace_state(session),
        "recovery": {"feedback": session.recovery_state.get('feedback', ''),
                     "last_result": session.recovery_state.get('last'),
                     "last_verification": (session.recovery_state.get('last') or {}).get('verification')},
    }
    if session.pending_action:
        resp["action"] = session.pending_action.model_dump()
    if session.kb_id or session.task_state['artifacts']:
        from agent.materials import public_state
        resp['materials_version'] = 1
        resp['materials'] = public_state(session)
    resp['continuation_id'] = session.material_continuation
    resp['task_workspace_version'] = 2
    return resp
