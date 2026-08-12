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
    AgentSession, AgentStatus, PageAction, PageState, ActionResult, FailedAttempt,
)
from agent.context_builder import (
    SYSTEM_PROMPT, SYSTEM_PROMPT_TEXT_MODE, REFLECT_SENTINEL,
    build_initial_messages, build_reflection_prompt, append_step_messages,
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
            _track_attempt_result(session, session.pending_action, action_result)
            append_step_messages(session.messages, session.pending_action, action_result, page_state)
            if session.step_history:
                session.step_history[-1]["result"] = {
                    "success": action_result.success,
                    "details": (action_result.details or "")[:50],
                    "url_after": page_state.url,
                }
        session.pending_action = None

    # 2. 反思注入（去重，只留最新一条）
    reflection = build_reflection_prompt(session)
    if reflection:
        session.messages = [
            m for m in session.messages
            if not (m.get("role") == "user" and str(m.get("content", "")).startswith(REFLECT_SENTINEL))
        ]
        session.messages.append({"role": "user", "content": reflection})

    # 3. 步数上限
    if session.current_step >= session.max_steps:
        session.status = AgentStatus.ERROR
        session.error = f"超过最大步数 {session.max_steps}，任务未完成"
        _agent_log.error("max_steps_exceeded", session_id=session.session_id,
                         data={"step": session.current_step})
        return _build_response(session)

    # 4. 刷新 system prompt（模式可能降级）
    if session.messages and session.messages[0].get("role") == "system":
        session.messages[0] = {"role": "system",
            "content": SYSTEM_PROMPT_TEXT_MODE if mode == CallMode.TEXT_PARSE else SYSTEM_PROMPT}

    # 5. 调 LLM
    _log_tokens(session, mode)
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
                              "total_steps": session.current_step})
        return _build_response(session, thought=thought)

    if func_name not in ALLOWED_ACTION_TYPES:
        session.status = AgentStatus.ERROR
        session.error = f"未知的操作类型: {func_name}"
        return _build_response(session, thought=thought)

    action = _parse_action(func_name, func_args)
    session.pending_action = action
    session.current_step += 1
    session.progress = thought[:80] if thought else session.progress
    _agent_log.info("execution_result", session_id=session.session_id,
                    data={"action_type": func_name, "index": action.index,
                          "thought": thought[:100], "step": session.current_step})
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


def _track_attempt_result(session: AgentSession, action: PageAction, result: ActionResult) -> None:
    target = str(action.index) if action.index is not None else action.type
    if result.success:
        session.blacklisted_approaches = [a for a in session.blacklisted_approaches if target not in a]
        return
    session.failed_attempts.append(FailedAttempt(
        action_type=action.type, target=target,
        error=result.error or result.details or "", step=session.current_step))
    recent = session.failed_attempts[-5:]
    similar = [a for a in recent if a.action_type == action.type and a.target == target]
    if len(similar) >= 2:
        desc = f"{action.type} → [{target}]"
        if desc not in session.blacklisted_approaches:
            session.blacklisted_approaches.append(desc)


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


def _log_tokens(session: AgentSession, mode: CallMode) -> None:
    pass  # 占位，实际在 _log_exec_tokens


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
