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
    Goal,
    FailedAttempt,
)
from agent.context_builder import (
    build_initial_messages,
    build_initial_messages_text_mode,
    build_initial_planning_messages,
    build_goal_planning_messages,
    build_goal_context,
    build_reflection_prompt,
    append_step_messages,
)
from agent.router import should_confirm_action
from tools.tool_registry import ACTION_SCHEMAS, ALLOWED_ACTION_TYPES


__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AGENT_MODE = os.getenv("AGENT_MODE", "auto")  # "tool_calls" | "text_parse" | "auto"

_llm_client = OpenAI(base_url=MODEL_BASE_URL, api_key=OPENAI_API_KEY)

_sessions: dict[str, AgentSession] = {}
_SESSION_TTL_SECONDS = 600  # 10分钟过期


def _cleanup_expired_sessions():
    """清理过期会话。"""
    now = time.time()
    expired = [
        sid for sid, s in _sessions.items()
        if now - getattr(s, '_created_at', now) > _SESSION_TTL_SECONDS
        and s.status in (AgentStatus.COMPLETED, AgentStatus.ERROR, AgentStatus.CANCELLED)
    ]
    for sid in expired:
        del _sessions[sid]


class CallMode(str, Enum):
    TOOL_CALLS = "tool_calls"
    TEXT_PARSE = "text_parse"


def get_session(session_id: str) -> Optional[AgentSession]:
    _cleanup_expired_sessions()
    return _sessions.get(session_id)


def create_session(
    session_id: str,
    task: str,
    model: str,
    max_steps: int = 15,
    require_confirmation: Optional[list[str]] = None,
) -> AgentSession:
    _cleanup_expired_sessions()
    session = AgentSession(
        session_id=session_id,
        task=task,
        model=model,
        max_steps=max_steps,
        require_confirmation=require_confirmation or [],
    )
    session._created_at = time.time()
    _sessions[session_id] = session
    return session


def cancel_session(session_id: str) -> bool:
    session = _sessions.get(session_id)
    if not session:
        return False
    session.status = AgentStatus.CANCELLED
    return True


def run_step(
    session: AgentSession,
    page_state: PageState,
    action_result: Optional[ActionResult] = None,
) -> dict[str, Any]:
    """执行 Agent 循环的一步。每步执行后都重新规划目标进度。"""

    if session.current_step >= session.max_steps:
        session.status = AgentStatus.ERROR
        session.error = f"已达到最大步数限制 ({session.max_steps})"
        return _build_response(session)

    # 首次规划：拆出第一个可执行目标 + 待拆解目标
    if not session.initial_planning_done:
        _initial_plan(session, page_state)
        session.initial_planning_done = True
        if session.goals:
            session.status = AgentStatus.PLAN_READY
            return _build_response(session, plan=_build_plan_info(session))

    # 每步都重新规划当前目标（结合最新页面）
    if session.goals and session.current_goal_index < len(session.goals):
        current_goal = session.goals[session.current_goal_index]
        if not current_goal.planned:
            _plan_current_goal(session, page_state)
            session.status = AgentStatus.PLAN_READY
            return _build_response(session, plan=_build_plan_info(session))
        # 步骤上限
        if current_goal.step_count >= session.max_steps_per_goal:
            current_goal.status = "completed"
            return _advance_to_next_goal(session, page_state)

    session.current_step += 1
    if session.goals and session.current_goal_index < len(session.goals):
        session.goals[session.current_goal_index].step_count += 1

    # 记录上一步结果
    if action_result and session.pending_action:
        _track_attempt_result(session, session.pending_action, action_result)
        append_step_messages(
            session.messages, session.pending_action, action_result, page_state
        )
        session.pending_action = None

    mode = _resolve_call_mode(session)

    # 每步用最新目标进度构建上下文
    goal_ctx = build_goal_context(
        session.task, session.goals, session.current_goal_index
    ) if session.goals else ""

    if not session.messages:
        if mode == CallMode.TOOL_CALLS:
            session.messages = build_initial_messages(session.task, page_state, goal_ctx)
        else:
            session.messages = build_initial_messages_text_mode(session.task, page_state, goal_ctx)
    else:
        # 每步更新 system prompt 中的目标进度
        if session.messages[0].get("role") == "system":
            from agent.context_builder import SYSTEM_PROMPT, TEXT_MODE_FORMAT_APPENDIX
            if mode == CallMode.TOOL_CALLS:
                session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + goal_ctx}
            else:
                session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + goal_ctx + TEXT_MODE_FORMAT_APPENDIX}

    # 注入反思提示
    reflection = build_reflection_prompt(session)
    if reflection:
        session.messages.append({"role": "user", "content": reflection})

    client = _llm_client

    if mode == CallMode.TOOL_CALLS:
        func_name, func_args, thought = _call_with_tools(client, session)
    else:
        func_name, func_args, thought = _call_text_parse(client, session)

    if func_name is None:
        return _build_response(session, thought=thought)

    # goal_complete / task_complete → 完成当前目标，重新规划下一个
    if func_name in ("task_complete", "goal_complete"):
        if session.goals and session.current_goal_index < len(session.goals):
            session.goals[session.current_goal_index].status = "completed"
            has_remaining = any(
                g.status == "pending" for g in session.goals[session.current_goal_index + 1:]
            )
            if has_remaining:
                return _advance_to_next_goal(session, page_state, thought=thought)
        session.status = AgentStatus.COMPLETED
        session.summary = func_args.get("summary", "任务完成")
        return _build_response(session, thought=thought)

    if func_name == "goal_retry":
        return _retry_goal(session, func_args, thought=thought)

    if func_name not in ALLOWED_ACTION_TYPES:
        session.status = AgentStatus.ERROR
        session.error = f"未知的操作类型: {func_name}"
        return _build_response(session, thought=thought)

    action = _parse_action(func_name, func_args)
    session.pending_action = action

    session.step_history.append(
        {"step": session.current_step, "thought": thought, "action": action.model_dump()}
    )

    if should_confirm_action(action, session.require_confirmation):
        session.status = AgentStatus.CONFIRM_REQUIRED
    else:
        session.status = AgentStatus.ACTION_REQUIRED

    return _build_response(session, thought=thought)


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
RETRY_DELAYS = [1, 3, 8]


def _llm_call_with_retry(
    client: OpenAI, session: AgentSession,
    tools=None, tool_choice=None,
):
    """带重试的 LLM API 调用，区分可恢复/不可恢复错误。"""
    last_error = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            kwargs = {
                "model": session.model,
                "messages": session.messages,
            }
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
                session.status = AgentStatus.ERROR
                session.error = "API 认证失败 (401): 请检查 OPENAI_API_KEY 配置"
                return None
            if e.status_code == 404:
                session.status = AgentStatus.ERROR
                session.error = f"模型不存在 (404): {session.model}"
                return None
            if e.status_code >= 500:
                last_error = f"API 服务端错误 ({e.status_code})"
            else:
                session.status = AgentStatus.ERROR
                session.error = f"API 错误 ({e.status_code}): {str(e)[:150]}"
                return None
        except Exception as e:
            last_error = f"未知错误: {type(e).__name__}: {str(e)[:100]}"

        if attempt < MAX_LLM_RETRIES - 1:
            time.sleep(RETRY_DELAYS[attempt])

    session.status = AgentStatus.ERROR
    session.error = f"LLM 调用失败（重试{MAX_LLM_RETRIES}次后放弃）: {last_error}"
    return None

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
    # 完全相同
    if a == b:
        return True
    # 一个包含另一个（如 "#btn" vs "btn"）
    if a in b or b in a:
        return True
    # annotation_id 相同
    if a.isdigit() and b.isdigit() and a == b:
        return True
    return False


def _initial_plan(session: AgentSession, page_state: PageState) -> None:
    """首次规划：拆出第一个可执行目标 + 待拆解目标列表。"""
    messages = build_initial_planning_messages(session.task, page_state)
    try:
        response = _llm_client.chat.completions.create(
            model=session.model, messages=messages,
        )
    except Exception:
        session.goals = [Goal(description=session.task, status="in_progress", planned=True,
                              sub_steps=[session.task])]
        return

    if not response or not response.choices:
        session.goals = [Goal(description=session.task, status="in_progress", planned=True,
                              sub_steps=[session.task])]
        return

    raw = response.choices[0].message.content or ""
    clean = _strip_think_tags(raw)
    data = _parse_plan_json(clean)

    if data and "current_goal" in data:
        cg = data["current_goal"]
        first_goal = Goal(
            description=cg.get("description", session.task),
            status="in_progress",
            planned=True,
            sub_steps=[str(s) for s in cg.get("sub_steps", []) if s],
        )
        session.goals = [first_goal]
        # remaining_goals: 合并为最多1个待拆解目标
        remaining = [str(d) for d in data.get("remaining_goals", []) if d]
        if remaining:
            merged_desc = "；".join(remaining) if len(remaining) <= 2 else remaining[0]
            session.goals.append(Goal(description=merged_desc))
    else:
        session.goals = [Goal(description=session.task, status="in_progress", planned=True,
                              sub_steps=[session.task])]


def _plan_current_goal(session: AgentSession, page_state: PageState) -> None:
    """对当前待拆解目标进行拆解（结合最新页面状态）。

    LLM 返回 current_goal + remaining_goals:
    - current_goal 成为当前目标的实际执行内容
    - remaining_goals 追加为新的待拆解目标（插入到当前目标后面）
    """
    goal = session.goals[session.current_goal_index]
    messages = build_goal_planning_messages(
        session.task, goal.description, session.goals, page_state,
        retry_reason=goal.retry_reason,
    )
    try:
        response = _llm_client.chat.completions.create(
            model=session.model, messages=messages,
        )
    except Exception:
        goal.sub_steps = [goal.description]
        goal.planned = True
        goal.status = "in_progress"
        session.messages = []
        return

    if response and response.choices:
        raw = response.choices[0].message.content or ""
        clean = _strip_think_tags(raw)
        data = _parse_plan_json(clean)

        if data and "current_goal" in data:
            cg = data["current_goal"]
            goal.description = cg.get("description", goal.description)
            goal.sub_steps = [str(s) for s in cg.get("sub_steps", []) if s]
            # 追加新的 remaining 目标
            remaining = [str(d) for d in data.get("remaining_goals", []) if d]
            if remaining:
                insert_pos = session.current_goal_index + 1
                # 移除旧的 pending 目标（会被新拆解替代）
                old_remaining = [
                    i for i, g in enumerate(session.goals)
                    if i >= insert_pos and g.status == "pending" and not g.planned
                ]
                for i in reversed(old_remaining):
                    session.goals.pop(i)
                # 插入新 remaining
                for desc in remaining:
                    session.goals.insert(insert_pos, Goal(description=desc))
                    insert_pos += 1
        elif data and "sub_steps" in data:
            goal.sub_steps = [str(s) for s in data["sub_steps"] if s]
        else:
            goal.sub_steps = [goal.description]
    else:
        goal.sub_steps = [goal.description]

    goal.planned = True
    goal.status = "in_progress"
    goal.current_sub_step = 0
    session.messages = []


def _advance_to_next_goal(session: AgentSession, page_state: PageState = None, thought: str = "") -> dict[str, Any]:
    """推进到下一个目标，触发重新规划。"""
    session.current_goal_index += 1
    if session.current_goal_index >= len(session.goals):
        session.status = AgentStatus.COMPLETED
        session.summary = "所有目标已完成"
        return _build_response(session, thought=thought)

    next_goal = session.goals[session.current_goal_index]
    next_goal.status = "planning"
    # 重置上下文（新目标从零开始）
    session.messages = []
    session.failed_attempts = []
    session.blacklisted_approaches = []
    session.current_step -= 1  # 目标切换不计入步数

    # 如果有 page_state，立即拆解新目标
    if page_state:
        _plan_current_goal(session, page_state)
        session.status = AgentStatus.PLAN_READY
    else:
        session.status = AgentStatus.ACTION_REQUIRED

    return _build_response(session, thought=thought, plan=_build_plan_info(session))


def _retry_goal(session: AgentSession, args: dict, thought: str = "") -> dict[str, Any]:
    """回退到指定目标，携带错误原因重新拆解。"""
    target_index = args.get("target_index", max(0, session.current_goal_index - 1))
    reason = args.get("reason", "")

    if target_index < 0 or target_index >= len(session.goals):
        session.status = AgentStatus.ERROR
        session.error = f"无效的回退目标: {target_index}"
        return _build_response(session, thought=thought)

    target_goal = session.goals[target_index]
    if target_goal.retry_count >= 2:
        target_goal.status = "skipped"
        return _advance_to_next_goal(session, thought=thought)

    # 重置当前目标
    current_goal = session.goals[session.current_goal_index]
    current_goal.status = "pending"
    current_goal.planned = False

    # 回退目标：重新拆解
    target_goal.status = "planning"
    target_goal.planned = False
    target_goal.retry_count += 1
    target_goal.retry_reason = reason
    target_goal.sub_steps = []
    target_goal.step_count = 0
    target_goal.current_sub_step = 0
    session.current_goal_index = target_index

    # 清空上下文
    session.messages = []
    session.failed_attempts = []
    session.blacklisted_approaches = []
    session.current_step -= 1
    session.status = AgentStatus.ACTION_REQUIRED
    return _build_response(session, thought=thought, plan=_build_plan_info(session))


def _parse_plan_json(text: str) -> Optional[dict]:
    """从文本中解析规划 JSON。"""
    for block in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL):
        try:
            return json.loads(block.group(1))
        except json.JSONDecodeError:
            continue
    for obj in re.finditer(r'\{[^{}]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}[^{}]*)*\}', text, re.DOTALL):
        try:
            data = json.loads(obj.group(0))
            if "current_goal" in data or "sub_steps" in data:
                return data
        except json.JSONDecodeError:
            continue
    return None


def _build_plan_info(session: AgentSession) -> Optional[dict[str, Any]]:
    """构建返回给前端的 plan 信息。"""
    if not session.goals:
        return None
    return {
        "goals": [
            {
                "description": g.description,
                "status": g.status,
                "planned": g.planned,
                "sub_steps": g.sub_steps,
                "current_sub_step": g.current_sub_step,
                "step_count": g.step_count,
            }
            for g in session.goals
        ],
        "current_index": session.current_goal_index,
    }


def _build_response(
    session: AgentSession, thought: str = "", plan: Optional[dict] = None
) -> dict[str, Any]:
    """构造返回给前端的标准响应。"""
    resp: dict[str, Any] = {
        "session_id": session.session_id,
        "status": session.status.value,
        "step": session.current_step,
        "thought": thought,
        "action": None,
        "summary": session.summary,
        "error": session.error,
        "plan": plan or _build_plan_info(session),
    }

    if session.pending_action:
        resp["action"] = session.pending_action.model_dump()

    return resp
