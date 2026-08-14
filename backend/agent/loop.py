"""Agent 主循环模块（单 LLM 反应式架构）。

每一步：观察（编号元素列表）→ 单次 LLM 调用 → 一个动作。
定位契约：索引直连——LLM 输出 index，前端直取 data-agent-id 节点。
编号失效（stale）→ 重新观察再决策，不计失败。

支持两种调用模式：
- tool_calls：支持 function calling 的模型
- text_parse：不支持的推理模型，靠 prompt 输出 JSON 再解析
"""

import json
import os
import re
import time
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, RateLimitError, APIConnectionError, APIStatusError

from agent.state import (
    AgentSession, AgentStatus, PageAction, PageState, ActionResult,
)
from agent.context_builder import (
    SYSTEM_PROMPT, SYSTEM_PROMPT_TEXT_MODE,
    build_initial_messages, append_step_messages,
)
from agent.router import should_confirm_action
from tools.tool_registry import ACTION_SCHEMAS, ALLOWED_ACTION_TYPES
from observability.logger import get_logger

_agent_log = get_logger("agent")

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AGENT_MODE = os.getenv("AGENT_MODE", "auto")  # tool_calls | text_parse | auto

_llm_client = OpenAI(base_url=MODEL_BASE_URL, api_key=OPENAI_API_KEY)

_sessions: dict[str, AgentSession] = {}
_sessions_lock = threading.Lock()
_SESSION_TTL_SECONDS = 600
_SESSION_ABSOLUTE_TTL = 1800

MAX_LLM_RETRIES = 3
RETRY_DELAYS = [0.5, 1, 2]
MAX_STALE_RETRIES = 3   # 连续编号失效重观察上限，防打转

try:
    import tiktoken
    _tok_enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _tok_enc = None

# 无效点击提示的首行哨兵（供下一步注入前去重，只留最新一条）
INEFFECTIVE_SENTINEL = "[[ineffective]]"
# element_count 变化小于此值视为"无变化"（小面板展开可能只增 1 个）
INEFFECTIVE_COUNT_DELTA = 2

# 停滞：连续停在同一页（url+title 未变）达此步数 → 给 LLM 看最近轨迹（不强制反思）
STALL_THRESHOLD = 4
TRAIL_SENTINEL = "[[trail]]"


class CallMode(str, Enum):
    TOOL_CALLS = "tool_calls"
    TEXT_PARSE = "text_parse"


def _cleanup_expired_sessions():
    now = time.time()
    expired = [
        sid for sid, s in _sessions.items()
        if (now - s.created_at > _SESSION_TTL_SECONDS
            and s.status in (AgentStatus.COMPLETED, AgentStatus.ERROR, AgentStatus.CANCELLED))
        or (now - s.created_at > _SESSION_ABSOLUTE_TTL)
    ]
    for sid in expired:
        del _sessions[sid]


def get_session(session_id: str) -> Optional[AgentSession]:
    with _sessions_lock:
        _cleanup_expired_sessions()
        return _sessions.get(session_id)


def create_session(session_id: str, task: str, model: str,
                   require_confirmation: Optional[list[str]] = None) -> AgentSession:
    with _sessions_lock:
        _cleanup_expired_sessions()
        session = AgentSession(
            session_id=session_id, task=task, model=model,
            require_confirmation=require_confirmation or [],
        )
        _sessions[session_id] = session
    _agent_log.info("session_create", session_id=session_id, data={"task": task, "model": model})
    return session


def cancel_session(session_id: str) -> bool:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if not session:
            return False
        session.status = AgentStatus.CANCELLED
    _agent_log.info("session_cancel", session_id=session_id, data={"step": session.current_step})
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# 主循环：单步（观察结果 → LLM → 下一个动作）
# ═══════════════════════════════════════════════════════════════════════════════

def run_step(session: AgentSession, page_state: PageState,
             action_result: Optional[ActionResult] = None) -> dict[str, Any]:
    """记录上一步结果 + 调 LLM 出下一个动作。stale 由前端处理（重新观察后再调本函数）。"""
    mode = _resolve_call_mode(session)

    # 1. 首步初始化 messages / 后续追加上一步结果+新观察
    if not session.messages:
        session.messages = build_initial_messages(
            session.task, page_state, session, text_mode=(mode == CallMode.TEXT_PARSE))
    elif action_result and session.pending_action:
        # stale：不追加结果（避免污染），前端已重新观察，这里只刷新观察
        if action_result.stale:
            session.stale_retries += 1
            _agent_log.info("step_result", session_id=session.session_id,
                            data={"step": session.current_step, "outcome": "stale",
                                  "stale_retries": session.stale_retries, "url": page_state.url})
            if session.stale_retries >= MAX_STALE_RETRIES:
                # 连续失效多次仍打转 → 追加一条提示，让 LLM 换策略
                session.messages.append({"role": "user", "content":
                    f"[编号多次失效] 页面在频繁变化。请基于下面最新观察重新选择编号。\n\n{_observe(page_state)}"})
                session.stale_retries = 0
            else:
                session.messages.append({"role": "user", "content":
                    f"[编号已失效，页面已更新] 请用下面最新观察里的编号。\n\n{_observe(page_state)}"})
        else:
            session.stale_retries = 0
            changes = action_result.state_changes or {}
            _agent_log.info("step_result", session_id=session.session_id,
                            data={"step": session.current_step,
                                  "outcome": "ok" if action_result.success else "fail",
                                  "details": (action_result.details or "")[:80],
                                  "error": (action_result.error or "")[:80],
                                  "url": page_state.url,
                                  "changes": changes})
            # 打转检测：click/select 成功但页面无任何变化 = 成功但无效
            _detect_ineffective(session, action_result, changes, page_state)
            # 停滞检测：连续停在同一页 → 累加计数，下面据此给 LLM 看最近轨迹
            _detect_stall(session, page_state)
            append_step_messages(session.messages, session.pending_action, action_result, page_state)
            if session.step_history:
                session.step_history[-1]["result"] = {
                    "success": action_result.success,
                    "details": (action_result.details or "")[:50],
                    "url_after": page_state.url,
                }
        session.pending_action = None

    # 1b. 无效点击提示注入（哨兵去重，只留最新一条）
    if session.last_ineffective:
        idx, tgt = session.last_ineffective
        streak = session.ineffective_clicks.get(idx, 0)
        if streak >= 2:
            warn = (f"{INEFFECTIVE_SENTINEL}⚠️ 你已连续 {streak} 次点击 [{idx}]『{tgt}』但页面毫无变化——"
                    f"这个操作无效，**禁止再点 [{idx}]**。换一种方式：换其他编号、先 hover 展开菜单、"
                    f"或 scroll 找目标，或用 navigate 直接跳转 URL。")
        else:
            warn = (f"{INEFFECTIVE_SENTINEL}⚠️ 上一步点击 [{idx}]『{tgt}』后页面无变化，该操作可能无效。"
                    f"不要重复点它，换个思路（换编号/hover 展开/scroll/navigate）。")
        session.messages = [
            m for m in session.messages
            if not (m.get("role") == "user" and str(m.get("content", "")).startswith(INEFFECTIVE_SENTINEL))
        ]
        session.messages.append({"role": "user", "content": warn})
        session.last_ineffective = None

    # 1c. 停滞（连续停在同一页 ≥ 阈值）→ 给 LLM 看最近轨迹，让它自己判断要不要换思路（不强制）
    if session.no_progress_count >= STALL_THRESHOLD:
        from agent.context_builder import build_trail_hint
        hint = build_trail_hint(session, page_state)
        session.messages = [
            m for m in session.messages
            if not (m.get("role") == "user" and str(m.get("content", "")).startswith(TRAIL_SENTINEL))
        ]
        session.messages.append({"role": "user", "content": hint})
        session.no_progress_count = 0   # 提醒一次后重置，避免每步刷屏
        _agent_log.info("stall_hint", session_id=session.session_id,
                        data={"step": session.current_step, "url": page_state.url})

    # 2. 步数上限
    if session.current_step >= session.max_steps:
        session.status = AgentStatus.ERROR
        session.error = f"超过最大步数 {session.max_steps}，任务未完成"
        _agent_log.error("max_steps_exceeded", session_id=session.session_id,
                         data={"step": session.current_step,
                               "url": page_state.url, "title": (page_state.title or "")[:60]})
        return _build_response(session)

    # 3. 刷新 system prompt（模式可能降级）
    if session.messages and session.messages[0].get("role") == "system":
        session.messages[0] = {"role": "system",
            "content": SYSTEM_PROMPT_TEXT_MODE if mode == CallMode.TEXT_PARSE else SYSTEM_PROMPT}

    # 4. 调 LLM（先注入当前计划 + 记录观察上下文）
    from agent.context_builder import build_plan_block, PLAN_SENTINEL
    plan_block = build_plan_block(session)
    if plan_block:
        session.messages = [
            m for m in session.messages
            if not (m.get("role") == "user" and str(m.get("content", "")).startswith(PLAN_SENTINEL))
        ]
        session.messages.append({"role": "user", "content": plan_block})
    _log_observation(session, page_state)
    if mode == CallMode.TOOL_CALLS:
        func_name, func_args, thought = _call_with_tools(_llm_client, session)
    else:
        func_name, func_args, thought = _call_text_parse(_llm_client, session)

    if func_name is None:
        return _build_response(session, thought=thought)

    if func_name == "task_complete":
        session.status = AgentStatus.COMPLETED
        session.summary = func_args.get("summary", "任务完成")
        _agent_log.info("session_complete", session_id=session.session_id,
                        data={"summary": session.summary, "success": func_args.get("success", True),
                              "total_steps": session.current_step,
                              "url": page_state.url, "title": (page_state.title or "")[:60]})
        return _build_response(session, thought=thought)

    # update_plan：LLM 自维护的计划，是"思考"不操作页面 → 更新后递归让它接着出真正的页面动作
    if func_name == "update_plan":
        session.plan_items = _sanitize_plan(func_args.get("items", []))
        if "current" in func_args:
            try:
                session.current_plan_item = int(func_args.get("current", -1))
            except (ValueError, TypeError):
                pass
        session.plan_calls_this_step += 1
        _agent_log.info("plan_update", session_id=session.session_id,
                        data={"step": session.current_step, "items": len(session.plan_items),
                              "current": session.current_plan_item})
        if session.plan_calls_this_step >= 2:
            # 连调防护：已记录计划，强制要求出页面动作，避免只规划不干活
            session.messages.append({"role": "user",
                "content": "计划已记录。现在请执行一个具体的页面动作（不要再调 update_plan）。"})
        return run_step(session, page_state)   # 同一观察递归，出页面动作

    if func_name not in ALLOWED_ACTION_TYPES:
        session.status = AgentStatus.ERROR
        session.error = f"未知的操作类型: {func_name}"
        return _build_response(session, thought=thought)

    action = _parse_action(func_name, func_args)
    session.pending_action = action
    session.current_step += 1
    session.plan_calls_this_step = 0   # 真正的页面动作 → 清零 update_plan 连调计数
    session.progress = thought[:80] if thought else session.progress
    # 富日志：把 index 解析成目标元素信息（tag/text/role），并带上动作参数、当前页面
    target = _describe_target(action.index, page_state)
    log_data = {
        "step": session.current_step,
        "action": func_name,
        "index": action.index,
        "target": target,                       # 点了什么（tag "文本" role）
        "thought": thought[:200],
        "url": page_state.url,
        "title": (page_state.title or "")[:60],
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
        {"step": session.current_step, "thought": thought, "action": action.model_dump()})

    if should_confirm_action(action, session.require_confirmation):
        session.status = AgentStatus.CONFIRM_REQUIRED
    else:
        session.status = AgentStatus.ACTION_REQUIRED
    return _build_response(session, thought=thought)


def _observe(page_state: PageState) -> str:
    from agent.context_builder import build_observation_message
    return build_observation_message(page_state)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 调用
# ═══════════════════════════════════════════════════════════════════════════════

def _call_with_tools(client: OpenAI, session: AgentSession) -> tuple[Optional[str], dict, str]:
    _log_exec_tokens(session, tools=ACTION_SCHEMAS, call="tool_calls")
    response = _create_with_retry(client, session.model, session.messages,
                                  tools=ACTION_SCHEMAS, tool_choice="auto", session=session)
    if response is None:
        return None, {}, ""
    message = response.choices[0].message
    raw_content = message.content or ""
    thought = _extract_thought(raw_content)

    if message.tool_calls:
        session.messages.append(message.model_dump())
        tool_call = message.tool_calls[0]
        func_name = tool_call.function.name
        try:
            func_args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            func_args = {}
        session.messages.append({"role": "tool", "tool_call_id": tool_call.id,
                                 "content": "已收到，等待执行结果。"})
        return func_name, func_args, thought

    session.messages.append({"role": "assistant", "content": raw_content})
    clean = _strip_think_tags(raw_content)
    parsed = _try_parse_action_from_text(clean)
    if parsed:
        return parsed[0], parsed[1], thought

    if AGENT_MODE == "auto":
        session.call_mode = CallMode.TEXT_PARSE.value
        if session.messages and session.messages[0].get("role") == "system":
            session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT_TEXT_MODE}
        return _call_text_parse(client, session)

    session.status = AgentStatus.ERROR
    session.error = f"LLM 未返回 tool_calls。回复: {clean[:200]}"
    return None, {}, thought


def _call_text_parse(client: OpenAI, session: AgentSession) -> tuple[Optional[str], dict, str]:
    _log_exec_tokens(session, tools=None, call="text_parse")
    response = _create_with_retry(client, session.model, session.messages, session=session)
    if response is None:
        return None, {}, ""
    message = response.choices[0].message
    raw_content = message.content or ""
    thought = _extract_thought(raw_content)
    clean = _strip_think_tags(raw_content)
    session.messages.append({"role": "assistant", "content": raw_content})
    parsed = _try_parse_action_from_text(clean)
    if parsed:
        return parsed[0], parsed[1], thought
    session.status = AgentStatus.ERROR
    session.error = f"无法从模型回复解析动作。回复: {clean[:300]}"
    return None, {}, thought


def _create_with_retry(client: OpenAI, model: str, messages: list,
                       tools=None, tool_choice=None, session: Optional[AgentSession] = None):
    last_error = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            kwargs = {"model": model, "messages": messages}
            if tools:
                kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
            return client.chat.completions.create(**kwargs)
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


def _resolve_call_mode(session: AgentSession) -> CallMode:
    if getattr(session, "call_mode", None):
        return CallMode(session.call_mode)
    if AGENT_MODE == "text_parse":
        return CallMode.TEXT_PARSE
    return CallMode.TOOL_CALLS


# ═══════════════════════════════════════════════════════════════════════════════
# 解析 / 反思 / 埋点
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_thought(text: str) -> str:
    m = re.search(r'<think>(.*?)</think>', text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _strip_think_tags(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _try_parse_action_from_text(text: str) -> Optional[tuple[str, dict[str, Any]]]:
    candidates = []
    for block in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL):
        candidates.append(block.group(1))
    for obj in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL):
        candidates.append(obj.group(0))
    for raw in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "name" in data and "arguments" in data:
            func_name = data["name"]
            func_args = data["arguments"] if isinstance(data["arguments"], dict) else {}
        else:
            func_name = data.get("action") or data.get("type") or data.get("name")
            func_args = {k: v for k, v in data.items() if k not in ("action", "type", "name", "thought")}
        if func_name and (func_name in ALLOWED_ACTION_TYPES or func_name == "task_complete"):
            return func_name, func_args
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


def _detect_ineffective(session: AgentSession, result: ActionResult,
                        changes: dict, page_state: PageState) -> None:
    """检测"成功但无效"的点击：click/select outcome=ok 但页面无任何变化。

    索引直连下的真实失败模式——点中了、成功了，但页面没反应。累加连续次数，
    供下一步注入"别再点它"的提示。任何有效变化则清零该 index。
    """
    action = session.pending_action
    if not action or action.index is None:
        return
    if action.type not in ("click", "select"):
        return
    idx = action.index
    no_change = (
        not changes.get("url_changed")
        and not changes.get("popup_appeared")
        and not changes.get("popup_disappeared")
        and abs(changes.get("element_count_delta", 0)) < INEFFECTIVE_COUNT_DELTA
    )
    if result.success and no_change:
        session.ineffective_clicks[idx] = session.ineffective_clicks.get(idx, 0) + 1
        session.last_ineffective = (idx, _describe_target(idx, page_state)[:40])
    else:
        # 有效变化 → 清零该 index 的无效计数
        session.ineffective_clicks.pop(idx, None)
        session.last_ineffective = None


def _detect_stall(session: AgentSession, page_state: PageState) -> bool:
    """停滞检测（对齐 browser-use：只判"连续停在同一页"，用最稳的 url+title）。

    换页即清零；连续 STALL_THRESHOLD 步没换页 → 返回 True（调用方给 LLM 看轨迹，不强制反思）。
    刻意不看正文指纹、不区分动作类型——那些规则补不完（打字/自动刷新/动画各种例外）。
    判断"是否在原地打转"交给 LLM，代码只保证它看得到历史。
    """
    sig = f"{page_state.url}|{page_state.title}"
    if sig != session.last_page_sig:
        session.last_page_sig = sig
        session.no_progress_count = 0
        return False
    session.no_progress_count += 1
    return session.no_progress_count >= STALL_THRESHOLD


def _sanitize_plan(items) -> list[dict]:
    """校验/规整 LLM 传来的计划：限 ≤10 条，content 截断，status 非法归 pending。"""
    if not isinstance(items, list):
        return []
    out = []
    valid = {"pending", "current", "done", "skipped"}
    for it in items[:10]:
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


def _estimate_tokens(messages: list, tools=None) -> dict:
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
        parts = []
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.append(json.dumps(c, ensure_ascii=False))
        tc = m.get("tool_calls") if isinstance(m, dict) else None
        if tc:
            parts.append(json.dumps(tc, ensure_ascii=False, default=str))
        return "\n".join(parts)

    msg_tok = sum(_count(_msg_text(m)) for m in (messages or []))
    tools_tok = _count(json.dumps(tools, ensure_ascii=False)) if tools else 0
    return {"total": msg_tok + tools_tok, "messages": msg_tok, "tools": tools_tok,
            "method": "tiktoken" if _tok_enc is not None else "heuristic"}


def _log_observation(session: AgentSession, page_state: PageState) -> None:
    """记录本步 LLM 看到的观察上下文，诊断"是否看得到目标内容/完成判断依据"。"""
    txt = page_state.text_content_summary or ""
    els = page_state.interactive_elements or []
    popup = page_state.active_popup or {}
    _agent_log.info("observation", session_id=session.session_id,
                    data={"step": session.current_step,
                          "url": page_state.url,
                          "title": (page_state.title or "")[:60],
                          "elements": len(els),
                          "popup": popup.get("type", "") if popup else "",
                          "text_len": len(txt),          # 前端采集的正文长度（注入 LLM 时会被截断）
                          "text_head": txt[:160],        # 正文开头（看是否全是导航/菜单）
                          "text_tail": txt[-160:] if len(txt) > 320 else ""})  # 正文结尾（目标内容常在此）


def _log_exec_tokens(session: AgentSession, tools, call: str) -> None:
    t = _estimate_tokens(session.messages, tools=tools)
    _agent_log.info("step_prompt_tokens", session_id=session.session_id,
                    data={"step": session.current_step, "call": call,
                          "total": t["total"], "messages": t["messages"],
                          "tools": t["tools"], "method": t["method"]})


def _build_response(session: AgentSession, thought: str = "") -> dict[str, Any]:
    resp: dict[str, Any] = {
        "session_id": session.session_id,
        "status": session.status.value,
        "step": session.current_step,
        "thought": thought,
        "action": None,
        "summary": session.summary,
        "error": session.error,
        "progress": session.progress,
    }
    if session.pending_action:
        resp["action"] = session.pending_action.model_dump()
    return resp
