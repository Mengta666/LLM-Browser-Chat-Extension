"""Agent 自动化 API 端点。

提供 /v1/agent/execute（启动会话）、/v1/agent/step（继续执行）、/v1/agent/cancel（取消）。
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent.loop import create_session, get_session, run_step, cancel_session
from agent.state import PageState, ActionResult


router = APIRouter(prefix="/v1/agent", tags=["Agent 自动化"])


class AgentExecuteRequest(BaseModel):
    task: str
    page_state: dict[str, Any]
    session_id: str
    model: str = "gpt-4o"
    require_confirmation: list[str] = []


class AgentStepRequest(BaseModel):
    session_id: str
    action_result: dict[str, Any]
    page_state: dict[str, Any]
    user_confirmed: bool = False


class AgentCancelRequest(BaseModel):
    session_id: str


@router.post("/execute")
def agent_execute(item: AgentExecuteRequest) -> dict[str, Any]:
    """启动新的 Agent 自动化会话，返回第一个动作。"""
    if not item.task.strip():
        raise HTTPException(400, "task 不能为空")
    if not item.session_id.strip():
        raise HTTPException(400, "session_id 不能为空")

    existing = get_session(item.session_id)
    if existing:
        raise HTTPException(409, f"会话 {item.session_id} 已存在")

    session = create_session(
        session_id=item.session_id,
        task=item.task,
        model=item.model,
        require_confirmation=item.require_confirmation,
    )

    page_state = PageState(**item.page_state)

    try:
        return run_step(session, page_state)
    except Exception as exc:
        raise HTTPException(502, f"Agent 执行出错: {exc}") from exc


@router.post("/step")
def agent_step(item: AgentStepRequest) -> dict[str, Any]:
    """继续已有的 Agent 会话，传入上一步执行结果和新的页面状态。"""
    session = get_session(item.session_id)
    if not session:
        raise HTTPException(404, f"会话 {item.session_id} 不存在")

    raw_result = item.action_result or {}
    action_result = ActionResult(
        success=raw_result.get("success", False),
        action_type=raw_result.get("action_type", "unknown"),
        details=raw_result.get("details", ""),
        error=raw_result.get("error"),
        timestamp=raw_result.get("timestamp", 0),
        state_changes=raw_result.get("state_changes"),
    )
    page_state = PageState(**item.page_state)

    try:
        return run_step(session, page_state, action_result)
    except Exception as exc:
        raise HTTPException(502, f"Agent 执行出错: {exc}") from exc


@router.post("/cancel")
def agent_cancel(item: AgentCancelRequest) -> dict[str, Any]:
    """取消正在运行的 Agent 会话。"""
    success = cancel_session(item.session_id)
    if not success:
        raise HTTPException(404, f"会话 {item.session_id} 不存在")
    return {"session_id": item.session_id, "status": "cancelled"}
