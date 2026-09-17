"""自动化协议 v2：步骤幂等、决策状态查询、取消终态保护。"""
import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from agent import loop
from agent.state import PageState, ActionResult, AgentStatus

router = APIRouter(prefix='/v1/agent', tags=['Agent 自动化'])


class AgentExecuteRequest(BaseModel):
    protocol_version: int = 0
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    page_state: dict[str, Any]
    budget_ms: int = Field(default=180000, gt=0, le=3600000)
    task: str
    model: str = 'gpt-4o'
    require_confirmation: list[str] = []
    task_image: str = ''
    llm_params: dict[str, Any] = {}


class AgentStepRequest(BaseModel):
    protocol_version: int = 0
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    page_state: dict[str, Any]
    budget_ms: int = Field(default=180000, gt=0, le=3600000)
    action_result: dict[str, Any]
    force_done: bool = False


class AgentCancelRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)


class AgentStatusRequest(AgentCancelRequest):
    request_id: str = Field(min_length=1, max_length=128)


def _processing(session, request_id):
    return {'protocol_version': 2, 'session_id': session.session_id,
            'request_id': request_id, 'status': 'processing', 'action': None}


def _decide(item, initial=False):
    if item.protocol_version != 2:
        raise HTTPException(409, '自动化协议不匹配，请同时更新后端和扩展')
    try:
        page = PageState(**item.page_state)
        result = None if initial else ActionResult(**item.action_result)
    except (ValidationError, TypeError) as exc:
        raise HTTPException(400, '页面观察或动作结果格式错误') from exc
    if page.tab_id is None or not page.observation_id or not page.document_epoch:
        raise HTTPException(400, '缺少有效观察版本或目标标签页')
    if initial and not item.task.strip():
        raise HTTPException(400, 'task 不能为空')
    digest = hashlib.sha256(json.dumps(item.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with loop._sessions_lock:
        session = loop.get_session(item.session_id)
        if not session:
            if not initial:
                raise HTTPException(404, '自动化会话不存在，不能续接旧动作')
            try:
                session = loop.create_session(item.session_id, item.task, item.model,
                    item.require_confirmation, item.task_image, item.llm_params)
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
            session.bound_tab_id = page.tab_id
        if session.cancel_event.is_set():
            return loop._build_response(session)
        previous = session.request_digests.get(item.request_id)
        if previous:
            if previous != digest:
                raise HTTPException(409, '同一请求标识不能用于不同内容')
            session.last_activity = time.time()
            return session.request_results.get(item.request_id) or _processing(session, item.request_id)
        if session.in_flight:
            raise HTTPException(409, '已有决策正在处理，请查询原请求状态')
        if session.status in (AgentStatus.COMPLETED, AgentStatus.ERROR):
            return loop._build_response(session)
        if session.bound_tab_id != page.tab_id:
            raise HTTPException(409, '观察不属于本任务标签页')
        if initial and session.request_digests:
            raise HTTPException(409, '会话已经启动')
        if not initial:
            if not session.pending_action or result.action_id != session.pending_action.action_id:
                raise HTTPException(409, '动作结果已过期或不属于当前步骤')
            if result.execution_state not in ('completed', 'not_dispatched', 'partial'):
                raise HTTPException(409, '旧动作尚未结束，不能推进下一步')
            if page.observation_id == session.pending_action.observation_id:
                raise HTTPException(409, '必须提交动作后的新观察')
        session.in_flight = True
        session.active_request_id = item.request_id
        session.request_digests[item.request_id] = digest
        session.decision_deadline = time.monotonic() + min(item.budget_ms / 1000, 180)
    try:
        response = loop.run_step(session, page, result, getattr(item, 'force_done', False))
    except Exception as exc:
        with loop._sessions_lock:
            if not session.cancel_event.is_set():
                session.status = AgentStatus.ERROR
                session.pending_action = None
                session.success = False
                session.error = f'自动化决策失败（{type(exc).__name__}）'
            response = loop._build_response(session)
    with loop._sessions_lock:
        if session.cancel_event.is_set():
            response = loop._build_response(session)
        session.request_results[item.request_id] = response
        session.in_flight = False
        session.last_activity = time.time()
        return response


@router.post('/execute')
def agent_execute(item: AgentExecuteRequest):
    return _decide(item, initial=True)


@router.post('/step')
def agent_step(item: AgentStepRequest):
    return _decide(item)


@router.post('/status')
def agent_status(item: AgentStatusRequest):
    with loop._sessions_lock:
        session = loop.get_session(item.session_id)
        if not session:
            raise HTTPException(404, '自动化会话不存在')
        session.last_activity = time.time()
        if session.cancel_event.is_set():
            return loop._build_response(session)
        if item.request_id in session.request_results:
            return session.request_results[item.request_id]
        if item.request_id in session.request_digests:
            return _processing(session, item.request_id)
        raise HTTPException(404, '决策请求尚未接收')


@router.post('/cancel')
def agent_cancel(item: AgentCancelRequest):
    try:
        loop.cancel_session(item.session_id)
    except RuntimeError as exc:
        raise HTTPException(503, '取消记录暂时无法保存') from exc
    with loop._sessions_lock:
        return loop._build_response(loop.get_session(item.session_id))
