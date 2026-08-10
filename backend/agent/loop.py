"""Agent 主循环模块。

支持两种 LLM 调用模式：
1. tool_calls 模式 —— 适用于支持 function calling 的模型（GPT-4o、Claude 等）
2. text_parse 模式 —— 适用于不支持 tool calling 的推理模型（DeepSeek 等），
   通过 prompt 引导模型输出 JSON，再从文本中解析动作
"""

import json
import os
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, RateLimitError, APIConnectionError, APIStatusError

from agent.state import (
    AgentSession,
    AgentStatus,
    PageAction,
    PageState,
    ActionResult,
    ElementLocator,
    FailedAttempt,
    FailedPath,
)
from agent.context_builder import (
    build_initial_messages,
    build_initial_messages_text_mode,
    build_step_planning_messages,
    build_execution_goal_context,
    build_reflection_prompt,
    append_step_messages,
)
from agent.router import should_confirm_action
from tools.tool_registry import ACTION_SCHEMAS, ALLOWED_ACTION_TYPES
from observability.logger import get_logger

_agent_log = get_logger("agent")


__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AGENT_MODE = os.getenv("AGENT_MODE", "auto")  # "tool_calls" | "text_parse" | "auto"

_llm_client = OpenAI(base_url=MODEL_BASE_URL, api_key=OPENAI_API_KEY)

import threading
_sessions: dict[str, AgentSession] = {}
_sessions_lock = threading.Lock()
_SESSION_TTL_SECONDS = 600


def _cleanup_expired_sessions():
    """清理过期会话。终态会话 10 分钟过期，非终态会话 30 分钟强制过期。"""
    now = time.time()
    _SESSION_ABSOLUTE_TTL = 1800  # 30 分钟绝对超时（防 RUNNING 状态泄漏）
    expired = [
        sid for sid, s in _sessions.items()
        if (now - s.created_at > _SESSION_TTL_SECONDS
            and s.status in (AgentStatus.COMPLETED, AgentStatus.ERROR, AgentStatus.CANCELLED))
        or (now - s.created_at > _SESSION_ABSOLUTE_TTL)
    ]
    for sid in expired:
        del _sessions[sid]


class CallMode(str, Enum):
    TOOL_CALLS = "tool_calls"
    TEXT_PARSE = "text_parse"


def get_session(session_id: str) -> Optional[AgentSession]:
    with _sessions_lock:
        _cleanup_expired_sessions()
        return _sessions.get(session_id)


def create_session(
    session_id: str,
    task: str,
    model: str,
    require_confirmation: Optional[list[str]] = None,
) -> AgentSession:
    with _sessions_lock:
        _cleanup_expired_sessions()
        session = AgentSession(
            session_id=session_id,
            task=task,
            model=model,
            require_confirmation=require_confirmation or [],
        )
        _sessions[session_id] = session
    _agent_log.info("session_create", session_id=session_id,
                    data={"task": task, "model": model})
    return session


def cancel_session(session_id: str) -> bool:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if not session:
            return False
        session.status = AgentStatus.CANCELLED
    _agent_log.info("session_cancel", session_id=session_id,
                    data={"step": session.current_step})
    return True


def run_plan(
    session: AgentSession,
    page_state: PageState,
    action_result: Optional[ActionResult] = None,
) -> dict[str, Any]:
    """第一阶段：记录结果 + 调规划 LLM。返回 plan 信息。"""

    # 安全阀：步数超限 → 调评判 LLM 归因 → 重试
    if session.goal_step_count >= session.max_steps_per_goal:
        session.goal_retry_count += 1
        if session.goal_retry_count >= session.max_retries_per_goal:
            session.status = AgentStatus.ERROR
            session.error = f"目标「{session.current_goal}」重试{session.max_retries_per_goal}次仍未完成"
            _agent_log.error("goal_retry_exhausted", session_id=session.session_id,
                             data={"goal": session.current_goal, "retries": session.goal_retry_count})
            return _build_response(session)

        judgment = _call_judgment_llm(session, page_state)
        session.failed_paths.append(judgment)
        _agent_log.warn("goal_step_limit", session_id=session.session_id,
                        data={"goal": session.current_goal, "retry": session.goal_retry_count,
                              "judgment": {"reason": judgment.failure_reason, "avoid": judgment.avoid}})

        session.goal_step_count = 0
        session.messages = []
        session.failed_attempts = []
        session.blacklisted_approaches = []
        session._retrying = True

    # 1. 记录上一步结果
    if action_result and session.pending_action:
        _track_attempt_result(session, session.pending_action, action_result)
        append_step_messages(
            session.messages, session.pending_action, action_result, page_state
        )
        if session.step_history:
            session.step_history[-1]["result"] = {
                "success": action_result.success,
                "details": action_result.details[:50] if action_result.details else "",
                "url_after": page_state.url,
                "title_after": page_state.title[:30] if page_state.title else "",
                "state_changes": action_result.state_changes or {},
            }
        session.pending_action = None

    # 2. 规划 LLM
    t0 = time.time()
    plan = _call_planning_llm(session, page_state)
    plan_ms = int((time.time() - t0) * 1000)
    _agent_log.info("planning_result", session_id=session.session_id,
                    data=plan or {"error": "parse_failed"}, duration_ms=plan_ms)

    if plan and plan.get("task_done"):
        session.status = AgentStatus.COMPLETED
        session.summary = plan.get("summary", "任务完成")
        session.completed_goals = plan.get("completed_goals", session.completed_goals)
        if session.current_goal and session.current_goal not in session.completed_goals:
            session.completed_goals.append(session.current_goal)
        session.current_goal = ""
        session.remaining_goal = ""
        _agent_log.info("session_complete", session_id=session.session_id,
                        data={"summary": session.summary, "total_steps": session.current_step})
        return _build_response(session)

    # 更新规划状态
    if plan:
        old_goal = session.current_goal
        session.current_goal = plan.get("current_goal", session.current_goal)
        session.next_action_hint = plan.get("next_action_hint", "")
        session.completed_goals = plan.get("completed_goals", session.completed_goals)
        session.remaining_goal = plan.get("remaining", "")

        if session.current_goal != old_goal:
            session.goal_step_count = 0
            # 超限重试不清 retry 计数，否则 max_retries_per_goal 永远触发不了
            if not session._retrying:
                session.goal_retry_count = 0
        session._retrying = False

    session.status = AgentStatus.ACTION_REQUIRED
    return _build_response(session)


def run_action(
    session: AgentSession,
    page_state: PageState,
) -> dict[str, Any]:
    """第二阶段：调执行 LLM 返回 action。"""

    mode = _resolve_call_mode(session)
    goal_ctx = build_execution_goal_context(session)

    if not session.messages:
        if mode == CallMode.TOOL_CALLS:
            session.messages = build_initial_messages(session.task, page_state, goal_ctx)
        else:
            session.messages = build_initial_messages_text_mode(session.task, page_state, goal_ctx)
    else:
        from agent.context_builder import SYSTEM_PROMPT, TEXT_MODE_FORMAT_APPENDIX
        if session.messages and session.messages[0].get("role") == "system":
            if mode == CallMode.TOOL_CALLS:
                session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + goal_ctx}
            else:
                session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + goal_ctx + TEXT_MODE_FORMAT_APPENDIX}

    reflection = build_reflection_prompt(session)
    if reflection:
        # 剔除历史反思消息（首行哨兵），只保留最新一条，避免累积撑爆上下文
        from agent.context_builder import REFLECT_SENTINEL
        session.messages = [
            m for m in session.messages
            if not (m.get("role") == "user"
                    and str(m.get("content", "")).startswith(REFLECT_SENTINEL))
        ]
        session.messages.append({"role": "user", "content": reflection})

    client = _llm_client

    if mode == CallMode.TOOL_CALLS:
        func_name, func_args, thought = _call_with_tools(client, session)
    else:
        func_name, func_args, thought = _call_text_parse(client, session)

    if func_name is None:
        return _build_response(session, thought=thought)

    if func_name == "task_complete":
        session.status = AgentStatus.COMPLETED
        session.summary = func_args.get("summary", "任务完成")
        if session.current_goal and session.current_goal not in session.completed_goals:
            session.completed_goals.append(session.current_goal)
        session.current_goal = ""
        session.remaining_goal = ""
        return _build_response(session, thought=thought)

    if func_name not in ALLOWED_ACTION_TYPES:
        session.status = AgentStatus.ERROR
        session.error = f"未知的操作类型: {func_name}"
        return _build_response(session, thought=thought)

    action = _parse_action(func_name, func_args)
    session.pending_action = action
    session.current_step += 1
    session.goal_step_count += 1
    _agent_log.info("execution_result", session_id=session.session_id,
                    data={"action_type": func_name, "target": action.locator.value if action.locator else "",
                          "thought": thought[:100], "step": session.current_step})

    session.step_history.append(
        {"step": session.current_step, "thought": thought, "action": action.model_dump()}
    )

    if should_confirm_action(action, session.require_confirmation):
        session.status = AgentStatus.CONFIRM_REQUIRED
    else:
        session.status = AgentStatus.ACTION_REQUIRED

    return _build_response(session, thought=thought)


def run_step(
    session: AgentSession,
    page_state: PageState,
    action_result: Optional[ActionResult] = None,
) -> dict[str, Any]:
    """兼容接口：规划+执行合一调用。"""
    plan_resp = run_plan(session, page_state, action_result)
    if session.status in (AgentStatus.COMPLETED, AgentStatus.ERROR):
        return plan_resp
    return run_action(session, page_state)


# ═══════════════════════════════════════════════════════════════════════════════
# 模式 1：tool_calls（标准 function calling）
# ═══════════════════════════════════════════════════════════════════════════════

def _call_with_tools(
    client: OpenAI, session: AgentSession
) -> tuple[Optional[str], dict, str]:
    """使用 tools 参数调用 LLM，解析 tool_calls 返回。"""
    response = _llm_call_with_retry(
        client, session,
        tools=ACTION_SCHEMAS,
        tool_choice="auto",
    )
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
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": "已收到，等待执行结果。",
            }
        )
        return func_name, func_args, thought

    session.messages.append({"role": "assistant", "content": raw_content})
    clean_content = _strip_think_tags(raw_content)
    parsed = _try_parse_action_from_text(clean_content)
    if parsed:
        return parsed[0], parsed[1], thought

    if AGENT_MODE == "auto":
        session.call_mode = CallMode.TEXT_PARSE.value
        # 保留上下文：将 messages 中的 system prompt 替换为 text_parse 版本，其余保留
        if session.messages and session.messages[0].get("role") == "system":
            from agent.context_builder import SYSTEM_PROMPT_TEXT_MODE
            session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT_TEXT_MODE}
        session.current_step -= 1
        return _call_text_parse(client, session)

    session.status = AgentStatus.ERROR
    session.error = f"LLM 未返回 tool_calls。回复: {clean_content[:200]}"
    return None, {}, thought


# ═══════════════════════════════════════════════════════════════════════════════
# 模式 2：text_parse（文本解析，适用于不支持 function calling 的模型）
# ═══════════════════════════════════════════════════════════════════════════════

def _call_text_parse(
    client: OpenAI, session: AgentSession
) -> tuple[Optional[str], dict, str]:
    """不使用 tools 参数，通过 prompt 引导模型输出 JSON 动作。"""
    if not session.messages:
        session.messages = build_initial_messages_text_mode(session.task, PageState(
            url="", title="", elements=[]
        ))

    response = _llm_call_with_retry(client, session)
    if response is None:
        return None, {}, ""

    message = response.choices[0].message
    raw_content = message.content or ""
    thought = _extract_thought(raw_content)
    clean_content = _strip_think_tags(raw_content)

    session.messages.append({"role": "assistant", "content": raw_content})

    parsed = _try_parse_action_from_text(clean_content)
    if parsed:
        return parsed[0], parsed[1], thought

    session.status = AgentStatus.ERROR
    session.error = f"无法从模型回复中解析动作。回复: {clean_content[:300]}"
    return None, {}, thought


# ═══════════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

MAX_LLM_RETRIES = 3
RETRY_DELAYS = [0.5, 1, 2]


def _create_with_retry(
    client: OpenAI, model: str, messages: list,
    tools=None, tool_choice=None, session: Optional[AgentSession] = None,
):
    """带重试的底层 LLM 调用。区分可恢复/不可恢复错误。

    session 非空时：401/404 等致命错误直接置会话为 ERROR 并返回 None；
    session 为空时（规划/评判等辅助调用）：所有错误重试耗尽后返回 None，不改会话状态。
    """
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
            last_error = f"API 限流 (429): {e.message if hasattr(e, 'message') else str(e)}"
        except APITimeoutError:
            last_error = "API 请求超时"
        except APIConnectionError as e:
            last_error = f"网络连接失败: {str(e)[:100]}"
        except APIStatusError as e:
            if e.status_code == 401:
                if session:
                    session.status = AgentStatus.ERROR
                    session.error = "API 认证失败 (401): 请检查 OPENAI_API_KEY 配置"
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
        session.error = f"LLM 调用失败（重试{MAX_LLM_RETRIES}次后放弃）: {last_error}"
    return None


def _llm_call_with_retry(
    client: OpenAI, session: AgentSession,
    tools=None, tool_choice=None,
):
    """带重试的执行 LLM 调用（使用 session.messages）。"""
    return _create_with_retry(
        client, session.model, session.messages,
        tools=tools, tool_choice=tool_choice, session=session,
    )

def _resolve_call_mode(session: AgentSession) -> CallMode:
    """根据配置和会话状态决定调用模式。"""
    if hasattr(session, "call_mode") and session.call_mode:
        return CallMode(session.call_mode)
    if AGENT_MODE == "tool_calls":
        return CallMode.TOOL_CALLS
    if AGENT_MODE == "text_parse":
        return CallMode.TEXT_PARSE
    return CallMode.TOOL_CALLS


def _extract_thought(text: str) -> str:
    """从 <think> 标签中提取推理内容。"""
    match = re.search(r'<think>(.*?)</think>', text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _strip_think_tags(text: str) -> str:
    """移除 <think>...</think> 标签，返回剩余文本。"""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _try_parse_action_from_text(text: str) -> Optional[tuple[str, dict[str, Any]]]:
    """从文本中解析 JSON 动作。

    支持格式:
    1. ```json { ... } ``` 代码块
    2. 裸 JSON 对象 { ... }
    3. function_call: {"name": "click", "arguments": {...}}
    """
    candidates = []

    for block in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL):
        candidates.append(block.group(1))

    for obj in re.finditer(r'\{[^{}]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}[^{}]*)*\}', text, re.DOTALL):
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
            func_name = data.get("type") or data.get("name") or data.get("action")
            func_args = {k: v for k, v in data.items() if k not in ("type", "name", "action")}

        if func_name and (func_name in ALLOWED_ACTION_TYPES or func_name == "task_complete"):
            return func_name, func_args

    return None


def _parse_action(func_name: str, args: dict[str, Any]) -> PageAction:
    """从参数解析出 PageAction。"""
    locator = None
    params = {}

    if "locator" in args:
        loc_data = args["locator"]
        fallback = None
        if "fallback" in loc_data:
            fb = loc_data["fallback"]
            fallback = ElementLocator(method=fb["method"], value=str(fb["value"]))
        locator = ElementLocator(
            method=loc_data["method"],
            value=str(loc_data["value"]),
            fallback=fallback,
        )

    if func_name == "type":
        params["text"] = args.get("text", "")
        params["clear"] = args.get("clear", True)
    elif func_name == "select":
        if "value" in args:
            params["value"] = args["value"]
        if "option_text" in args:
            params["option_text"] = args["option_text"]
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
    elif func_name == "wait_for_element":
        params["selector"] = args.get("locator", {}).get("value", "") if "locator" in args else ""
        params["timeout"] = min(args.get("timeout", 5000), 10000)

    return PageAction(type=func_name, locator=locator, params=params)


def _track_attempt_result(
    session: AgentSession, action: PageAction, result: ActionResult
) -> None:
    """追踪操作结果，检测重复失败并更新黑名单。"""
    target = action.locator.value if action.locator else ""

    if result.success:
        # 成功时清除与当前目标相关的黑名单
        session.blacklisted_approaches = [
            a for a in session.blacklisted_approaches
            if target not in a
        ]
        return

    # 记录失败
    session.failed_attempts.append(FailedAttempt(
        action_type=action.type,
        target=target,
        error=result.error or result.details or "",
        step=session.current_step,
    ))

    # 检测最近 5 步中相同 (action_type + 相似 target) 出现 ≥ 2 次
    recent = session.failed_attempts[-5:]
    similar = [
        a for a in recent
        if a.action_type == action.type and _is_similar_target(a.target, target)
    ]
    if len(similar) >= 2:
        approach_desc = f"{action.type} → {target}"
        if approach_desc not in session.blacklisted_approaches:
            session.blacklisted_approaches.append(approach_desc)


def _is_similar_target(a: str, b: str) -> bool:
    """判断两个 locator value 是否指向同一目标。"""
    if not a or not b:
        return a == b
    if a == b:
        return True
    # 纯数字（annotation_id）必须精确相等，避免 "1" 命中 "10"
    if a.isdigit() or b.isdigit():
        return a == b
    # 子串包含仅在较短串足够长（≥4）时才算相似，避免短 css 片段互相误判
    if (a in b or b in a) and min(len(a), len(b)) >= 4:
        return True
    return False


def _call_judgment_llm(session: AgentSession, page_state: PageState) -> FailedPath:
    """调用评判 LLM 对本轮失败进行归因。"""
    from agent.context_builder import JUDGMENT_PROMPT

    # 构建轨迹摘要
    trajectory_lines = []
    for s in session.step_history[-10:]:
        action = s.get("action", {})
        result = s.get("result", {})
        a_type = action.get("type", "?")
        target = (action.get("locator") or {}).get("value", "")[:20]
        status = "✓" if result.get("success", True) else "✗"
        url = result.get("url_after", "")[-40:] if result.get("url_after") else ""
        line = f"{a_type}"
        if target:
            line += f" → {target}"
        line += f" | {status}"
        if url:
            line += f" | {url}"
        trajectory_lines.append(line)

    user_content = (
        f"## 用户任务\n{session.task}\n\n"
        f"## 当前目标\n{session.current_goal}\n\n"
        f"## 这一轮的操作轨迹\n" + "\n".join(trajectory_lines) + "\n\n"
        f"## 最终页面\nURL: {page_state.url}\n标题: {page_state.title}"
    )

    messages = [
        {"role": "system", "content": JUDGMENT_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        response = _create_with_retry(_llm_client, session.model, messages)
    except Exception as e:
        _agent_log.error("judgment_call_error", session_id=session.session_id,
                         data={"error": f"{type(e).__name__}: {str(e)[:200]}"})
        return FailedPath(failure_reason="评判调用失败", avoid="")

    if not response or not response.choices:
        return FailedPath(failure_reason="评判无响应", avoid="")

    raw = response.choices[0].message.content or ""
    clean = _strip_think_tags(raw)

    # 解析 JSON
    for block in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', clean, re.DOTALL):
        try:
            data = json.loads(block.group(1))
            return FailedPath(
                failure_reason=data.get("failure_reason", "未知原因"),
                avoid=data.get("avoid", ""),
            )
        except json.JSONDecodeError:
            continue
    for obj in re.finditer(r'\{[^{}]*\}', clean, re.DOTALL):
        try:
            data = json.loads(obj.group(0))
            if "failure_reason" in data:
                return FailedPath(
                    failure_reason=data.get("failure_reason", "未知原因"),
                    avoid=data.get("avoid", ""),
                )
        except json.JSONDecodeError:
            continue

    return FailedPath(failure_reason=clean[:200], avoid="")


def _call_planning_llm(session: AgentSession, page_state: PageState) -> Optional[dict]:
    """调用规划 LLM，返回解析后的 plan dict。失败返回 None。"""
    messages = build_step_planning_messages(session, page_state)
    # 埋点：记录规划 prompt 是否包含参考经验，以及首屏元素文本
    user_msg = next((m["content"] for m in messages if m.get("role") == "user"), "")
    has_kb = "📖 参考经验" in user_msg
    _agent_log.info("planning_prompt_debug", session_id=session.session_id,
                    data={"step": session.current_step, "has_kb_hint": has_kb,
                          "prompt_len": len(user_msg)})
    try:
        response = _create_with_retry(_llm_client, session.model, messages)
    except Exception as e:
        _agent_log.error("planning_call_error", session_id=session.session_id,
                         data={"error": f"{type(e).__name__}: {str(e)[:200]}"})
        return None

    if not response or not response.choices:
        return None

    raw = response.choices[0].message.content or ""
    clean = _strip_think_tags(raw)

    # 解析 JSON
    for block in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', clean, re.DOTALL):
        try:
            return json.loads(block.group(1))
        except json.JSONDecodeError:
            continue
    for obj in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', clean, re.DOTALL):
        try:
            data = json.loads(obj.group(0))
            if "current_goal" in data or "task_done" in data:
                return data
        except json.JSONDecodeError:
            continue
    return None


def _build_response(
    session: AgentSession, thought: str = "", plan: Optional[dict] = None
) -> dict[str, Any]:
    """构造返回给前端的标准响应。"""
    # 构建 plan 信息
    plan_info = plan or {
        "current_goal": session.current_goal,
        "completed_goals": session.completed_goals,
        "remaining": session.remaining_goal,
        "task_done": session.status == AgentStatus.COMPLETED,
        "referenced_id": getattr(session, "_kb_referenced_id", ""),
    } if session.current_goal or session.completed_goals else None

    resp: dict[str, Any] = {
        "session_id": session.session_id,
        "status": session.status.value,
        "step": session.current_step,
        "thought": thought,
        "action": None,
        "summary": session.summary,
        "error": session.error,
        "plan": plan_info,
    }

    if session.pending_action:
        resp["action"] = session.pending_action.model_dump()

    return resp
