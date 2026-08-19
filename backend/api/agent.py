"""Agent 自动化 API 端点（单 LLM 反应式）。

/execute 启动会话并返回首个动作；/step 传入上一步结果+新观察，返回下一个动作；
/cancel 取消。（旧的 /plan /action 双端点已合并进 /step。）
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from agent.loop import (
    create_session, get_session, run_step, cancel_session,
    acquire_session, release_session,
)
from agent.state import PageState, ActionResult


router = APIRouter(prefix="/v1/agent", tags=["Agent 自动化"])


class AgentExecuteRequest(BaseModel):
    task: str
    page_state: dict[str, Any]
    session_id: str
    model: str = "gpt-4o"
    require_confirmation: list[str] = []
    task_image: str = ""   # 可选：任务附带的视觉上下文（上传图/框选截图 data URL）


class AgentStepRequest(BaseModel):
    session_id: str
    action_result: dict[str, Any] = {}
    page_state: dict[str, Any]
    user_confirmed: bool = False
    force_done: bool = False   # 前端整轮超时触发：强制本步只出 task_complete 收尾


class AgentCancelRequest(BaseModel):
    session_id: str


def _parse_result(raw: dict[str, Any]) -> ActionResult:
    raw = raw or {}
    return ActionResult(
        success=raw.get("success", False),
        action_type=raw.get("action_type", "unknown"),
        details=raw.get("details", ""),
        error=raw.get("error"),
        timestamp=raw.get("timestamp", 0),
        stale=raw.get("stale", False),
        state_changes=raw.get("state_changes"),
    )


@router.post("/execute")
def agent_execute(item: AgentExecuteRequest) -> dict[str, Any]:
    """启动新会话，返回第一个动作。"""
    if not item.task.strip():
        raise HTTPException(400, "task 不能为空")
    if not item.session_id.strip():
        raise HTTPException(400, "session_id 不能为空")
    if get_session(item.session_id):
        raise HTTPException(409, f"会话 {item.session_id} 已存在")

    try:
        session = create_session(
            session_id=item.session_id, task=item.task, model=item.model,
            require_confirmation=item.require_confirmation,
            task_image=item.task_image,
        )
    except RuntimeError as e:                       # 活跃会话到达容量上限
        raise HTTPException(503, str(e)) from e
    try:
        page_state = PageState(**item.page_state)
    except (ValidationError, TypeError) as e:
        raise HTTPException(400, f"page_state 格式错误: {str(e)[:200]}")
    try:
        return run_step(session, page_state)
    except Exception as exc:
        raise HTTPException(502, f"Agent 执行出错: {exc}") from exc


@router.post("/step")
def agent_step(item: AgentStepRequest) -> dict[str, Any]:
    """继续会话：传入上一步执行结果 + 新页面观察，返回下一个动作。"""
    # 原子取会话 + 占用忙标志：拒绝同会话并发 /step（防 current_step 读-改-写竞态）
    session, acquired = acquire_session(item.session_id)
    if session is None:
        raise HTTPException(404, f"会话 {item.session_id} 不存在")
    if not acquired:
        raise HTTPException(409, f"会话 {item.session_id} 有请求正在处理中")
    try:
        action_result = _parse_result(item.action_result)
        try:
            page_state = PageState(**item.page_state)
        except (ValidationError, TypeError) as e:
            raise HTTPException(400, f"page_state 格式错误: {str(e)[:200]}")
        try:
            return run_step(session, page_state, action_result, force_done=item.force_done)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Agent 执行出错: {exc}") from exc
    finally:
        release_session(session)               # 无论成功/异常/校验失败都释放忙标志


@router.post("/cancel")
def agent_cancel(item: AgentCancelRequest) -> dict[str, Any]:
    success = cancel_session(item.session_id)
    if not success:
        raise HTTPException(404, f"会话 {item.session_id} 不存在")
    return {"session_id": item.session_id, "status": "cancelled"}
