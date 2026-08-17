"""Agent 主循环模块（结构化输出 verify-first 架构，对齐 browser-use）。

每一步：观察（编号元素列表 + 上一步结果）→ 单次 LLM 调用返回一个结构化 JSON：
  { evaluation_previous_goal, memory, next_goal, plan?, current_plan_item?, action }
LLM 必须先自评上一步是否成功（对照观察），再决定下一个动作。
定位契约：索引直连——action.index 对应 data-agent-id，前端直取节点。
编号失效（stale）→ 重新观察再决策，不计失败。
"""

import json
import os
import re
import time
import threading
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, RateLimitError, APIConnectionError, APIStatusError

from agent.state import (
    AgentSession, AgentStatus, PageAction, PageState, ActionResult, HistoryItem,
)
from agent.context_builder import (
    SYSTEM_PROMPT, build_messages, build_plan_block,
)
from agent.router import should_confirm_action
from tools.tool_registry import ALLOWED_ACTION_TYPES
from observability.logger import get_logger

_agent_log = get_logger("agent")

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

_llm_client = OpenAI(base_url=MODEL_BASE_URL, api_key=OPENAI_API_KEY)

_sessions: dict[str, AgentSession] = {}
_sessions_lock = threading.Lock()
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
COMPACT_TRIGGER_STEPS = 24   # history 超过此条数才触发 compaction（保守，短任务不触发）
COMPACT_KEEP_RECENT = 8      # 摘要后保留的最近步数（首项 + <摘要> + 最近 N 项）

try:
    import tiktoken
    _tok_enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _tok_enc = None


def _cleanup_expired_sessions():
    """回收空闲会话。基于 last_activity（非创建时间）：活跃任务不断刷新，永不被误清。

    两档：已收尾会话空闲 120s 即清（快腾内存）；其余（含 RUNNING）空闲 900s 清（真卡死才回收）。
    调用方必须已持有 _sessions_lock。
    """
    now = time.time()
    done_states = (AgentStatus.COMPLETED, AgentStatus.ERROR, AgentStatus.CANCELLED)
    expired = [
        sid for sid, s in _sessions.items()
        if (now - s.last_activity > _SESSION_DONE_IDLE_TTL and s.status in done_states)
        or (now - s.last_activity > _SESSION_IDLE_TTL)
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
    victims = [(s.last_activity, sid) for sid, s in _sessions.items() if s.status in done_states]
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
    with _sessions_lock:
        _cleanup_expired_sessions()
        return _sessions.get(session_id)


def create_session(session_id: str, task: str, model: str,
                   require_confirmation: Optional[list[str]] = None) -> AgentSession:
    with _sessions_lock:
        _cleanup_expired_sessions()
        _evict_if_full()
        # 容量仍到顶（全是活跃会话，无可淘汰）→ 拒绝新建，避免无上限撑爆内存
        if len(_sessions) >= MAX_SESSIONS:
            raise RuntimeError(
                f"活跃会话数已达上限 {MAX_SESSIONS}，暂时无法新建，请稍后重试")
        session = AgentSession(
            session_id=session_id, task=task, model=model,
            require_confirmation=require_confirmation or [],
        )
        _sessions[session_id] = session
    _agent_log.info("session_create", session_id=session_id,
                    data={"task": task, "model": model, "active_sessions": len(_sessions)})
    return session


def cancel_session(session_id: str) -> bool:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if not session:
            return False
        session.status = AgentStatus.CANCELLED
    _agent_log.info("session_cancel", session_id=session_id, data={"step": session.current_step})
    return True


def acquire_session(session_id: str) -> tuple[Optional[AgentSession], bool]:
    """原子地取会话并占用忙标志（check-and-set 在锁内，杜绝同会话并发 /step 竞态）。

    返回 (session, acquired)：
      (None, False)  会话不存在
      (session, False)  会话存在但正忙 → 调用方应回 409，且不要 release
      (session, True)   已成功占用 → 调用方处理完必须 release_session
    """
    with _sessions_lock:
        session = _sessions.get(session_id)
        if not session:
            return None, False
        if session.in_flight:
            return session, False
        session.in_flight = True
        return session, True


def release_session(session: AgentSession) -> None:
    session.in_flight = False


# ═══════════════════════════════════════════════════════════════════════════════
# 主循环：单步（观察 → 结构化 LLM → 一个动作）
# ═══════════════════════════════════════════════════════════════════════════════

def run_step(session: AgentSession, page_state: PageState,
             action_result: Optional[ActionResult] = None,
             force_done: bool = False) -> dict[str, Any]:
    """记录上一步结果 + 调 LLM 出下一个动作。stale 由前端处理（重新观察后再调本函数）。

    force_done=True（前端整轮超时触发）：强制本步只接受 task_complete，给用户一个交代。
    """
    session.last_activity = time.time()   # 刷新活动时间：活跃任务不会被空闲 TTL 误清

    # 1. 记录上一步结果为一条结构化 HistoryItem（不累积 messages；messages 每步重建）
    if action_result and session.pending_action:
        if action_result.stale:
            session.stale_retries += 1
            _agent_log.info("step_result", session_id=session.session_id,
                            data={"step": session.current_step, "outcome": "stale",
                                  "stale_retries": session.stale_retries, "url": page_state.url})
            stale_note = ("页面频繁变化，编号多次失效，请基于最新观察重新选择编号"
                          if session.stale_retries >= MAX_STALE_RETRIES
                          else "编号已失效，页面已更新，请用最新观察里的编号")
            if session.stale_retries >= MAX_STALE_RETRIES:
                session.stale_retries = 0
            session.history_items.append(HistoryItem(
                step=session.current_step,
                action=_action_summary(session.pending_action, page_state),
                result=f"↻ {stale_note}"))
        else:
            session.stale_retries = 0
            changes = action_result.state_changes or {}
            _agent_log.info("step_result", session_id=session.session_id,
                            data={"step": session.current_step,
                                  "outcome": "ok" if action_result.success else "fail",
                                  "details": (action_result.details or "")[:80],
                                  "error": (action_result.error or "")[:80],
                                  "url": page_state.url, "changes": changes})
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

    # 3. compaction（对齐 browser-use maybe_compact_messages）：步数很多时把中间段 LLM 总结成一条摘要，
    #    保留 首项 + <摘要> + 最近若干项。超长任务防止滑动窗口丢失中间进展。
    _maybe_compact_history(session)

    # 4. 每步重建 messages（system + 任务 + 历史块 + 计划 + 当前观察）
    session.messages = build_messages(session, page_state)

    # 5. 调 LLM（结构化 JSON）
    _log_observation(session, page_state)
    _log_exec_tokens(session)
    parsed = _call_llm(session)
    if parsed is None:
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

    if func_name == "task_complete":
        session.status = AgentStatus.COMPLETED
        session.summary = action_obj.get("summary", "任务完成")
        _agent_log.info("session_complete", session_id=session.session_id,
                        data={"summary": session.summary, "success": action_obj.get("success", True),
                              "total_steps": session.current_step,
                              "url": page_state.url, "title": (page_state.title or "")[:60]})
        return _build_response(session)

    # 强制收尾时 LLM 仍出页面动作（不听话）→ 代码用其记忆替它体面收尾（对齐 browser-use force-done）
    if session.force_done:
        session.status = AgentStatus.COMPLETED
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
    session.pending_action = action
    session.current_step += 1

    target = _describe_target(action.index, page_state)
    log_data = {
        "step": session.current_step, "action": func_name, "index": action.index,
        "target": target, "url": page_state.url, "title": (page_state.title or "")[:60],
        "elements": len(page_state.interactive_elements or []),
    }
    if func_name == "type":
        log_data["text"] = (action.params.get("text", "") or "")[:60]
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

    if should_confirm_action(action, session.require_confirmation):
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
    已压过的历史（首项后紧跟 compacted 项）不重复压，避免每步都调 LLM。
    """
    items = session.history_items
    if len(items) <= COMPACT_TRIGGER_STEPS:
        return
    # 已有摘要项（step=-1 标记）紧跟首项 → 说明刚压过，暂不重复压
    if len(items) > 1 and items[1].step == -1:
        # 只有当摘要后又堆积了足够多新步骤才再次压缩
        if len(items) - 2 <= COMPACT_TRIGGER_STEPS:
            return
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
        resp = _create_with_retry(
            _llm_client, session.model,
            [{"role": "user", "content": prompt}], session=None)
        summary = (resp.choices[0].message.content or "").strip() if resp else ""
        summary = _strip_think_tags(summary)
    except Exception:
        summary = ""
    if not summary:
        return  # 摘要失败 → 保持原历史，滑动窗口兜底

    compacted = HistoryItem(step=-1, memory=summary[:800],
                            evaluation="", next_goal="", action="", result="（以上为前序步骤摘要）")
    session.history_items = head + [compacted] + recent
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
        response = _create_with_retry(_llm_client, session.model, session.messages, session=session)
        if response is None:
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


def _create_with_retry(client: OpenAI, model: str, messages: list, session: Optional[AgentSession] = None):
    last_error = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            return client.chat.completions.create(model=model, messages=messages, timeout=LLM_CALL_TIMEOUT)
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
            time.sleep(RETRY_DELAYS[attempt])
    if session:
        session.status = AgentStatus.ERROR
        session.error = f"LLM 调用失败（重试{MAX_LLM_RETRIES}次）: {last_error}"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 解析
# ═══════════════════════════════════════════════════════════════════════════════

def _strip_think_tags(text: str) -> str:
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
        if data.get("type") in ALLOWED_ACTION_TYPES or data.get("type") == "task_complete":
            return {"action": data}
    return None


def _parse_action(func_name: str, args: dict[str, Any]) -> PageAction:
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
    tgt = _describe_target(action.index, page_state)
    if tgt and not tgt.startswith("(编号"):
        parts.append(tgt.replace("<", "").replace(">", ""))
    p = action.params or {}
    if p.get("text"):
        parts.append(f'输入"{str(p["text"])[:30]}"')
    if p.get("option_text"):
        parts.append(f'选"{p["option_text"]}"')
    if p.get("key"):
        parts.append(p["key"])
    if p.get("url"):
        parts.append(str(p["url"])[:50])
    return " ".join(parts)[:120]


def _result_summary(result: ActionResult, page_state: PageState) -> str:
    """把动作结果压成一行摘要，进 HistoryItem。"""
    tag = "✓" if result.success else "✗"
    bits = [tag]
    if result.details:
        bits.append(str(result.details)[:50])
    if result.error:
        bits.append(f"错误:{str(result.error)[:40]}")
    ch = result.state_changes or {}
    flags = [k for k in ("url_changed", "popup_appeared", "popup_disappeared") if ch.get(k)]
    if flags:
        bits.append("+".join(flags))
    return " ".join(bits)[:120]


# ═══════════════════════════════════════════════════════════════════════════════
# 埋点
# ═══════════════════════════════════════════════════════════════════════════════

def _estimate_tokens(messages: list) -> dict:
    def _count(text: str) -> int:
        if not text:
            return 0
        if _tok_enc is not None:
            try:
                return len(_tok_enc.encode(text))
            except Exception:
                pass
        zh = sum(1 for c in text if ord(c) > 127)
        return int((len(text) - zh) / 4 + zh / 1.5)

    def _msg_text(m) -> str:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            # 多模态：只计文本部分；图片按固定成本估（不把 base64 dataURL 算进去）
            texts = []
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
            return "\n".join(texts)
        return ""

    def _img_count(m) -> int:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            return sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
        return 0

    IMG_TOKEN = 1100   # 单张截图估算 token（粗略，避免 base64 撑爆计数）
    text_tok = sum(_count(_msg_text(m)) for m in (messages or []))
    n_img = sum(_img_count(m) for m in (messages or []))
    return {
        "total": text_tok + n_img * IMG_TOKEN,
        "text": text_tok,
        "images": n_img,
        "method": "tiktoken" if _tok_enc is not None else "heuristic",
    }


def _log_observation(session: AgentSession, page_state: PageState) -> None:
    txt = page_state.text_content_summary or ""
    els = page_state.interactive_elements or []
    popup = page_state.active_popup or {}
    _agent_log.info("observation", session_id=session.session_id,
                    data={"step": session.current_step, "url": page_state.url,
                          "title": (page_state.title or "")[:60], "elements": len(els),
                          "popup": popup.get("type", "") if popup else "",
                          "text_len": len(txt), "text_head": txt[:160],
                          "text_tail": txt[-160:] if len(txt) > 320 else ""})


def _log_exec_tokens(session: AgentSession) -> None:
    t = _estimate_tokens(session.messages)
    _agent_log.info("step_prompt_tokens", session_id=session.session_id,
                    data={"step": session.current_step, "total": t["total"],
                          "text": t["text"], "images": t["images"], "method": t["method"]})


def _build_response(session: AgentSession, thought: str = "") -> dict[str, Any]:
    resp: dict[str, Any] = {
        "session_id": session.session_id,
        "status": session.status.value,
        "step": session.current_step,
        "thought": session.last_evaluation,   # 前端展示：上一步自评
        "action": None,
        "summary": session.summary,
        "error": session.error,
        "progress": session.progress,          # 前端展示：next_goal
    }
    if session.pending_action:
        resp["action"] = session.pending_action.model_dump()
    return resp
