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
    kb_id: str = ''
    materials_version: int = 0


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


class AgentResumeRequest(BaseModel):
    protocol_version: int = 0
    request_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    page_state: dict[str, Any]
    continuation_id: str = Field(min_length=1, max_length=128)
    budget_ms: int = Field(default=180000, gt=0, le=3600000)


class MaterialCheckRequest(BaseModel):
    session_id: str
    action_id: str


class AgentStatusRequest(AgentCancelRequest):
    request_id: str = Field(min_length=1, max_length=128)


def _processing(session, request_id):
    return {'protocol_version': 2, 'session_id': session.session_id,
            'request_id': request_id, 'status': 'processing', 'action': None}


def _decide(item, initial=False, resume=False):
    if item.protocol_version != 2:
        raise HTTPException(409, '自动化协议不匹配，请同时更新后端和扩展')
    try:
        page = PageState(**item.page_state)
        result = None if initial or resume else ActionResult(**item.action_result)
    except (ValidationError, TypeError) as exc:
        raise HTTPException(400, '页面观察或动作结果格式错误') from exc
    if page.tab_id is None or not page.observation_id or not page.document_epoch:
        raise HTTPException(400, '缺少有效观察版本或目标标签页')
    if initial and not item.task.strip():
        raise HTTPException(400, 'task 不能为空')
    if initial and item.kb_id:
        if item.materials_version != 1:
            raise HTTPException(409, '知识库物料协议不匹配，请重新加载扩展')
        from agent.materials import validate_kb
        from storage.kb_store import KBNotFound, KBConflict
        try:
            validate_kb(item.kb_id)
        except KBNotFound as exc:
            raise HTTPException(404, '绑定知识库不存在或不可访问') from exc
        except KBConflict as exc:
            raise HTTPException(409, '绑定知识库暂不可用') from exc
    digest = hashlib.sha256(json.dumps(item.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with loop._sessions_lock:
        session = loop.get_session(item.session_id)
        if not session:
            if not initial:
                raise HTTPException(404, '自动化会话不存在，不能续接旧动作')
            try:
                session = loop.create_session(item.session_id, item.task, item.model,
                    item.require_confirmation, item.task_image, item.llm_params, kb_id=item.kb_id)
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
        if resume:
            if (session.status != AgentStatus.OBSERVATION_REQUIRED or
                    item.continuation_id != session.material_continuation):
                raise HTTPException(409, '物料续接标识过期或不属于当前任务')
            if page.observation_id == session.material_observation_id:
                raise HTTPException(409, '取材后必须重新观察，不能复用旧页面编号')
            session.material_continuation = ''
        elif not initial:
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


@router.get('/capabilities')
@router.post('/capabilities')
def agent_capabilities():
    return {'protocol_version': 2, 'materials_version': 1, 'material_fill_version': 1,
            'restart_resume': False}


@router.post('/resume')
def agent_resume(item: AgentResumeRequest):
    return _decide(item, resume=True)


@router.post('/material-check')
def agent_material_check(item: MaterialCheckRequest):
    from agent.materials import revalidate
    with loop._sessions_lock:
        session = loop.get_session(item.session_id)
        if not session:
            raise HTTPException(404, '自动化会话不存在，请重新开始，不重放旧动作')
        action = session.pending_action
        if session.cancel_event.is_set() or not action or action.action_id != item.action_id:
            raise HTTPException(409, '物料动作已失效或任务已停止')
        if session.in_flight:
            raise HTTPException(409, '任务仍在决策中')
        revalidate(session)
        field = next((f for f in session.material_state.get('fields', {}).values()
                      if f.get('material_id') == action.params.get('material_id')), None)
        allowed = bool(field and field['status'] == 'available' and
                       field['value'] == action.params.get('text', action.params.get('option_text')))
        return {'allowed': allowed, 'error': '' if allowed else '物料来源已失效，未执行填写，请重新取材'}


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
