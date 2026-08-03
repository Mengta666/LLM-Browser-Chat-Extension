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
    SubTask,
    FailedAttempt,
)
from agent.context_builder import (
    build_initial_messages,
    build_initial_messages_text_mode,
    build_planning_messages,
    build_sub_task_context,
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
    """执行 Agent 循环的一步：观察→思考→决策。"""

    if session.current_step >= session.max_steps:
        session.status = AgentStatus.ERROR
        session.error = f"已达到最大步数限制 ({session.max_steps})"
        return _build_response(session)

    # 首步：先做任务分解规划
    if not session.planning_done:
        plan_result = _plan_task(session, page_state)
        session.planning_done = True
        if plan_result and len(plan_result) > 1:
            session.sub_tasks = [SubTask(description=desc) for desc in plan_result]
            session.sub_tasks[0].status = "in_progress"
            session.status = AgentStatus.PLAN_READY
            # 返回带 plan 的响应，前端展示计划后继续执行第一步
            return _build_response(session, plan=_build_plan_info(session))
        elif plan_result and len(plan_result) == 1:
            session.sub_tasks = [SubTask(description=plan_result[0], status="in_progress")]

    session.current_step += 1

    mode = _resolve_call_mode(session)

    sub_task_ctx = build_sub_task_context(
        session.task, session.sub_tasks, session.current_sub_task_index
    ) if session.sub_tasks else ""

    if not session.messages:
        if mode == CallMode.TOOL_CALLS:
            session.messages = build_initial_messages(session.task, page_state, sub_task_ctx)
        else:
            session.messages = build_initial_messages_text_mode(session.task, page_state, sub_task_ctx)
    elif action_result and session.pending_action:
        # 记录失败/成功并更新反思状态
        _track_attempt_result(session, session.pending_action, action_result)
        append_step_messages(
            session.messages, session.pending_action, action_result, page_state
        )
        session.pending_action = None

    # 注入反思提示（如果有重复失败）
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

    if func_name == "task_complete":
        session.status = AgentStatus.COMPLETED
        session.summary = func_args.get("summary", "任务完成")
        if session.sub_tasks:
            session.sub_tasks[session.current_sub_task_index].status = "completed"
        return _build_response(session, thought=thought)

    if func_name == "sub_task_complete":
        if session.sub_tasks and session.current_sub_task_index < len(session.sub_tasks):
            session.sub_tasks[session.current_sub_task_index].status = "completed"
            session.current_sub_task_index += 1
            if session.current_sub_task_index >= len(session.sub_tasks):
                session.status = AgentStatus.COMPLETED
                session.summary = func_args.get("summary", "所有子任务已完成")
                return _build_response(session, thought=thought)
            else:
                session.sub_tasks[session.current_sub_task_index].status = "in_progress"
                # 更新 system prompt 中的子任务上下文
                sub_task_ctx = build_sub_task_context(
                    session.task, session.sub_tasks, session.current_sub_task_index
                )
                if session.messages and session.messages[0].get("role") == "system":
                    from agent.context_builder import SYSTEM_PROMPT, SYSTEM_PROMPT_TEXT_MODE, TEXT_MODE_FORMAT_APPENDIX
                    mode = _resolve_call_mode(session)
                    if mode == CallMode.TOOL_CALLS:
                        session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + sub_task_ctx}
                    else:
                        session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + sub_task_ctx + TEXT_MODE_FORMAT_APPENDIX}
                session.current_step -= 1  # 子任务切换不计入步数
                session.status = AgentStatus.ACTION_REQUIRED
                return _build_response(session, thought=thought, plan=_build_plan_info(session))
        # 没有子任务时当作 task_complete 处理
        session.status = AgentStatus.COMPLETED
        session.summary = func_args.get("summary", "任务完成")
        return _build_response(session, thought=thought)

    if func_name == "sub_task_retry":
        target_index = func_args.get("target_index", 0)
        reason = func_args.get("reason", "")

        if not session.sub_tasks or target_index < 0 or target_index >= len(session.sub_tasks):
            session.status = AgentStatus.ERROR
            session.error = f"无效的回退目标: {target_index}"
            return _build_response(session, thought=thought)

        target_task = session.sub_tasks[target_index]
        if target_task.retry_count >= 2:
            # 超过重试上限，跳过该子任务
            target_task.status = "skipped"
            session.sub_tasks[session.current_sub_task_index].status = "in_progress"
            session.status = AgentStatus.ACTION_REQUIRED
            return _build_response(session, thought=thought, plan=_build_plan_info(session))

        # 回退：当前子任务重置为 pending，目标子任务标记为 in_progress
        session.sub_tasks[session.current_sub_task_index].status = "pending"
        target_task.status = "in_progress"
        target_task.retry_count += 1
        target_task.retry_reason = reason
        session.current_sub_task_index = target_index

        # 更新 system prompt
        sub_task_ctx = build_sub_task_context(
            session.task, session.sub_tasks, session.current_sub_task_index
        )
        if session.messages and session.messages[0].get("role") == "system":
            from agent.context_builder import SYSTEM_PROMPT, TEXT_MODE_FORMAT_APPENDIX
            mode = _resolve_call_mode(session)
            if mode == CallMode.TOOL_CALLS:
                session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + sub_task_ctx}
            else:
                session.messages[0] = {"role": "system", "content": SYSTEM_PROMPT + sub_task_ctx + TEXT_MODE_FORMAT_APPENDIX}
        session.current_step -= 1
        session.status = AgentStatus.ACTION_REQUIRED
        return _build_response(session, thought=thought, plan=_build_plan_info(session))

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


def _plan_task(session: AgentSession, page_state: PageState) -> Optional[list[str]]:
    """调用 LLM 对任务进行分解规划，返回子任务描述列表。失败时返回 None。"""
    messages = build_planning_messages(session.task, page_state)
    client = _llm_client

    try:
        response = client.chat.completions.create(
            model=session.model,
            messages=messages,
        )
    except Exception:
        return None

    if not response or not response.choices:
        return None

    raw_content = response.choices[0].message.content or ""
    clean_content = _strip_think_tags(raw_content)

    # 解析 JSON
    for block in re.finditer(r'```(?:json)?\s*(\{.*?\})\s*```', clean_content, re.DOTALL):
        try:
            data = json.loads(block.group(1))
            if "sub_tasks" in data and isinstance(data["sub_tasks"], list):
                return [str(t) for t in data["sub_tasks"] if t]
        except json.JSONDecodeError:
            continue

    for obj in re.finditer(r'\{[^{}]*"sub_tasks"\s*:\s*\[.*?\][^{}]*\}', clean_content, re.DOTALL):
        try:
            data = json.loads(obj.group(0))
            if "sub_tasks" in data and isinstance(data["sub_tasks"], list):
                return [str(t) for t in data["sub_tasks"] if t]
        except json.JSONDecodeError:
            continue

    return None


def _build_plan_info(session: AgentSession) -> Optional[dict[str, Any]]:
    """构建返回给前端的 plan 信息。"""
    if not session.sub_tasks:
        return None
    return {
        "sub_tasks": [
            {"description": st.description, "status": st.status}
            for st in session.sub_tasks
        ],
        "current_index": session.current_sub_task_index,
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
