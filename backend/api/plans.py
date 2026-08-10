"""计划模式 API。

负责创建、修订、批准、取消和完成计划，并把批准后的计划同步成 task_state 记忆。
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field

from api.chat import (
    BASE_SYSTEM_PROMPT,
    CurrentPage,
    build_page_context_messages,
    build_web_context_messages,
    json_dumps,
    make_id,
    strip_source_citations,
)
from memory.store import create_manual_memory, patch_memory, retrieve_memory_context
from storage.db import db

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PLAN_USER_ID = os.getenv("MEMORY_USER_ID", "local")

router = APIRouter(prefix="/api", tags=["plans"])

ACTIVE_PLAN_STATUSES = {"draft", "needs_revision", "executing"}
PLAN_APPROVABLE_STATUSES = {"draft", "needs_revision"}
PLAN_REVISABLE_STATUSES = {"draft", "needs_revision", "executing"}


class PlanContextOptions(BaseModel):
    """生成计划时可选的上下文来源配置。"""

    use_current_page: bool = False
    use_web_search: bool = False
    force_refresh_page: bool = False
    web_search_query: str = ""


class PlanCreateRequest(BaseModel):
    """创建计划的请求体。"""

    model: str = ""
    objective: str
    context_options: PlanContextOptions = Field(default_factory=PlanContextOptions)
    current_page: CurrentPage | None = None


class PlanReviseRequest(BaseModel):
    """修订计划时用户提交的反馈。"""

    model: str = ""
    feedback: str


def _safe_json_loads(value: Any, default: Any) -> Any:
    """宽松解析 SQLite 文本 JSON 字段。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _extract_first_json_object(text: str) -> dict[str, Any]:
    """从模型输出中提取计划 JSON 对象。"""
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(value)
    if not isinstance(parsed, dict):
        raise ValueError("plan model output must be a JSON object")
    return parsed


def _normalize_text_list(value: Any) -> list[str]:
    """把模型输出的字符串数组清洗成非空文本列表。"""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _normalize_checklist(value: Any) -> list[dict[str, str]]:
    """把 checklist 标准化为包含 title/detail 的对象数组。"""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            detail = str(item.get("detail") or "").strip()
        else:
            title = str(item or "").strip()
            detail = ""
        if title:
            result.append({"title": title, "detail": detail})
    return result


def _normalize_plan_output(raw: dict[str, Any], fallback_objective: str, change_summary: str) -> dict[str, Any]:
    """校验并补齐模型返回的计划结构。"""
    title = str(raw.get("title") or fallback_objective).strip()
    objective = str(raw.get("objective") or fallback_objective).strip()
    plan_markdown = str(raw.get("plan_markdown") or "").strip()
    checklist = _normalize_checklist(raw.get("checklist"))
    risks = _normalize_text_list(raw.get("risks"))
    assumptions = _normalize_text_list(raw.get("assumptions"))
    acceptance_criteria = _normalize_text_list(raw.get("acceptance_criteria"))
    open_questions = _normalize_text_list(raw.get("open_questions"))
    resolved_change_summary = str(raw.get("change_summary") or change_summary).strip()

    if not objective or not plan_markdown or not checklist:
        raise ValueError("plan JSON requires objective, plan_markdown and non-empty checklist")
    if not risks:
        raise ValueError("plan JSON requires non-empty risks")
    if not acceptance_criteria:
        raise ValueError("plan JSON requires non-empty acceptance_criteria")

    return {
        "title": title[:80],
        "objective": objective,
        "plan_markdown": plan_markdown,
        "checklist": checklist,
        "risks": risks,
        "assumptions": assumptions,
        "acceptance_criteria": acceptance_criteria,
        "open_questions": open_questions,
        "change_summary": resolved_change_summary,
    }


def _call_plan_model(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    """调用计划模型并返回解析后的 JSON。"""
    if not OPENAI_API_KEY or not MODEL_BASE_URL:
        raise RuntimeError("MODEL_BASE_URL or OPENAI_API_KEY is not configured")
    resolved_model = model.strip()
    if not resolved_model:
        raise ValueError("model is required")
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=MODEL_BASE_URL)
    response = client.chat.completions.create(
        model=resolved_model,
        messages=messages,
        temperature=0.2,
        stream=False,
    )
    content = response.choices[0].message.content or ""
    return _extract_first_json_object(content)


def _plan_system_prompt() -> str:
    """生成计划模式专用 system prompt。"""
    return (
        BASE_SYSTEM_PROMPT
        + "\n你处于计划模式。只生成可执行计划，不要声称已经完成任务。"
        + "输出必须是严格 JSON，不要 Markdown 代码块。"
        + "JSON 字段固定为 title, objective, plan_markdown, checklist, risks, "
        + "assumptions, acceptance_criteria, open_questions, change_summary。"
        + "checklist 是对象数组，每项包含 title 和 detail。"
        + "risks 和 acceptance_criteria 都必须是非空数组。"
    )


def _build_plan_messages(
        objective: str,
        previous_messages: list[dict[str, str]],
        memory_messages: list[dict[str, str]],
        context_messages: list[dict[str, str]],
        current_revision: dict[str, Any] | None = None,
        feedback: str = "",
) -> list[dict[str, str]]:
    """组装计划创建或修订时发送给模型的消息。"""
    revision_context = ""
    if current_revision:
        revision_context = (
            "当前计划版本：\n"
            + str(current_revision.get("plan_markdown") or "")
            + "\n\n当前 checklist JSON："
            + str(current_revision.get("checklist_json") or "[]")
        )
    user_lines = [
        f"计划目标：{objective}",
        "请生成或更新一份可执行计划。",
    ]
    if feedback:
        user_lines.append(f"用户修改意见：{feedback}")
    if revision_context:
        user_lines.append(revision_context)
    return [
        {"role": "system", "content": _plan_system_prompt()},
        *previous_messages,
        *memory_messages,
        *context_messages,
        {"role": "user", "content": "\n\n".join(user_lines)},
    ]


def _create_plan_turn(chat_id: str, model: str, objective: str, use_current_page: bool, use_web_search: bool) -> tuple[str, str]:
    """为计划操作创建一轮 chat turn 和对应用户消息。"""
    db.upsert_chat(chat_id)
    turn_id = make_id("turn")
    message_id = make_id("msg")
    db.create_chat_turn(
        turn_id=turn_id,
        chat_id=chat_id,
        turn_index=db.next_turn_index(chat_id),
        task_type="plan",
        query_text=objective,
        use_current_page=use_current_page,
        use_web_search=use_web_search,
    )
    db.insert_chat_message(
        message_id=message_id,
        chat_id=chat_id,
        turn_id=turn_id,
        role="user",
        content=objective,
        display_content=objective,
    )
    return turn_id, message_id


def _complete_plan_turn(chat_id: str, turn_id: str, assistant_message: str = "") -> str:
    """把计划 turn 标记完成，并按需写入助手展示消息。"""
    message_id = ""
    if assistant_message:
        message_id = make_id("msg")
        db.insert_chat_message(
            message_id=message_id,
            chat_id=chat_id,
            turn_id=turn_id,
            role="assistant",
            content=strip_source_citations(assistant_message),
            display_content=assistant_message,
        )
    db.complete_chat_turn(turn_id=turn_id, trace_json=json_dumps({"status": "ok", "task_type": "plan"}))
    return message_id


def _build_context_messages(
        model: str,
        chat_id: str,
        objective: str,
        options: PlanContextOptions,
        current_page: CurrentPage | None,
) -> list[dict[str, str]]:
    """按开关收集记忆、当前页和联网搜索上下文。"""
    messages: list[dict[str, str]] = []
    try:
        memory_messages, _ = retrieve_memory_context(
            query_text=objective,
            focus_text="",
            task_type="plan",
            use_current_page=options.use_current_page,
            use_web_search=options.use_web_search,
            chat_id=chat_id,
        )
        messages.extend(memory_messages)
    except Exception:
        pass

    if options.use_current_page and current_page:
        page_messages, page_sources, _ = build_page_context_messages(
            "plan",
            objective,
            "",
            current_page,
            chat_id,
            "",
            options.force_refresh_page,
            retrieval_query_override=objective,
        )
        messages.extend(page_messages)
        source_start_index = len(page_sources) + 1
    else:
        source_start_index = 1

    if options.use_web_search:
        web_messages, _, _ = build_web_context_messages(
            "plan",
            objective,
            "",
            True,
            options.web_search_query or objective,
            source_start_index=source_start_index,
            has_page_context=options.use_current_page,
        )
        messages.extend(web_messages)
    return messages


def _serialize_revision(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 revision 行里的 JSON 文本字段展开成前端可用结构。"""
    if not row:
        return None
    return {
        **row,
        "checklist": _safe_json_loads(row.get("checklist_json"), []),
        "risks": _safe_json_loads(row.get("risks_json"), []),
        "assumptions": _safe_json_loads(row.get("assumptions_json"), []),
        "acceptance_criteria": _safe_json_loads(row.get("acceptance_criteria_json"), []),
        "open_questions": _safe_json_loads(row.get("open_questions_json"), []),
    }


def _serialize_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """把计划主表、当前版本、步骤和事件合并为 API 返回结构。"""
    if not plan:
        return None
    revision = _serialize_revision(db.get_current_plan_revision(plan["plan_id"]))
    return {
        **plan,
        "current_revision": revision,
        "steps": db.list_plan_steps(plan["plan_id"]),
        "events": db.list_plan_events(plan["plan_id"]),
    }


def _insert_revision_from_output(
        plan_id: str,
        turn_id: str,
        revision_index: int,
        plan_output: dict[str, Any],
        user_request: str = "",
        user_feedback: str = "",
) -> str:
    """把一次模型计划输出写成新的 revision。"""
    revision_id = make_id("planrev")
    db.insert_plan_revision(
        revision_id=revision_id,
        plan_id=plan_id,
        revision_index=revision_index,
        user_request=user_request,
        user_feedback=user_feedback,
        plan_markdown=plan_output["plan_markdown"],
        checklist_json=json_dumps(plan_output["checklist"]),
        risks_json=json_dumps(plan_output["risks"]),
        assumptions_json=json_dumps(plan_output["assumptions"]),
        acceptance_criteria_json=json_dumps(plan_output["acceptance_criteria"]),
        open_questions_json=json_dumps(plan_output["open_questions"]),
        change_summary=plan_output["change_summary"],
        source_turn_id=turn_id,
    )
    return revision_id


def _summarize_checklist_for_memory(checklist: list[Any], limit: int = 5) -> str:
    """把 checklist 前几项压缩成 task_state 记忆证据。"""
    titles = []
    for index, step in enumerate(checklist[:limit], start=1):
        if isinstance(step, dict):
            title = str(step.get("title") or "").strip()
        else:
            title = str(step or "").strip()
        if title:
            titles.append(f"{index}. {title}")
    return "\n".join(titles)


def _mark_plan_task_memory_done(plan_id: str, plan: dict[str, Any]) -> None:
    """计划完成时同步关闭对应的 task_state 记忆。"""
    task_memory_id = str(plan.get("task_memory_id") or "").strip()
    if not task_memory_id:
        return
    db.update_memory_item(task_memory_id, {
        "task_status": "done",
        "task_updated_by": "assistant",
        "plan_id": plan_id,
        "evidence": "计划已完成：所有步骤已由自动执行链路输出结果，并由 complete API 标记完成。",
    })


@router.post("/chats/{chat_id}/plans")
def create_plan(chat_id: str, item: PlanCreateRequest) -> dict[str, Any]:
    """创建新的计划草稿；同一 chat 同时只允许一个活跃计划。"""
    normalized_chat_id = chat_id.strip()
    objective = item.objective.strip()
    if not normalized_chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not objective:
        raise HTTPException(status_code=400, detail="objective is required")
    if db.get_active_plan(normalized_chat_id):
        raise HTTPException(status_code=409, detail="active plan already exists")

    turn_id, _ = _create_plan_turn(
        normalized_chat_id,
        item.model,
        objective,
        item.context_options.use_current_page,
        item.context_options.use_web_search,
    )
    try:
        context_messages = _build_context_messages(
            item.model,
            normalized_chat_id,
            objective,
            item.context_options,
            item.current_page,
        )
        raw_plan = _call_plan_model(
            item.model,
            _build_plan_messages(objective, [], [], context_messages),
        )
        plan_output = _normalize_plan_output(raw_plan, objective, "首次生成计划。")
    except Exception as exc:
        db.fail_chat_turn(turn_id, "plan_model", str(exc))
        raise HTTPException(status_code=502, detail=f"plan model error: {exc}") from exc

    plan_id = make_id("plan")
    revision_id = _insert_revision_from_output(
        plan_id,
        turn_id,
        1,
        plan_output,
        user_request=objective,
    )
    db.insert_chat_plan(
        plan_id=plan_id,
        chat_id=normalized_chat_id,
        title=plan_output["title"],
        objective=plan_output["objective"],
        current_revision_id=revision_id,
        created_turn_id=turn_id,
    )
    _complete_plan_turn(normalized_chat_id, turn_id)
    db.insert_plan_event(
        event_id=make_id("planevt"),
        plan_id=plan_id,
        chat_id=normalized_chat_id,
        event_type="plan_created",
        revision_id=revision_id,
        turn_id=turn_id,
        summary=plan_output["change_summary"],
    )
    return {
        "plan": _serialize_plan(db.get_plan(plan_id)),
        "revision": _serialize_revision(db.get_plan_revision(revision_id)),
        "display_message": "计划已生成，请在计划面板查看。",
    }


@router.post("/plans/{plan_id}/revise")
def revise_plan(plan_id: str, item: PlanReviseRequest) -> dict[str, Any]:
    """根据用户反馈生成新的计划版本。"""
    plan = db.get_plan(plan_id.strip())
    feedback = item.feedback.strip()
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan["status"] not in PLAN_REVISABLE_STATUSES:
        raise HTTPException(status_code=409, detail="plan cannot be revised in current status")
    if not feedback:
        raise HTTPException(status_code=400, detail="feedback is required")

    chat_id = plan["chat_id"]
    turn_id, _ = _create_plan_turn(chat_id, item.model, feedback, False, False)
    db.update_chat_plan(plan_id, {"status": "needs_revision"})
    current_revision = db.get_current_plan_revision(plan_id)
    try:
        raw_plan = _call_plan_model(
            item.model,
            _build_plan_messages(
                plan["objective"],
                db.list_chat_messages(chat_id),
                [],
                [],
                current_revision=current_revision,
                feedback=feedback,
            ),
        )
        plan_output = _normalize_plan_output(raw_plan, plan["objective"], "根据用户反馈修订计划。")
    except Exception as exc:
        db.fail_chat_turn(turn_id, "plan_model", str(exc))
        raise HTTPException(status_code=502, detail=f"plan model error: {exc}") from exc

    revision_id = _insert_revision_from_output(
        plan_id,
        turn_id,
        db.next_plan_revision_index(plan_id),
        plan_output,
        user_feedback=feedback,
    )
    db.update_chat_plan(plan_id, {
        "title": plan_output["title"],
        "objective": plan_output["objective"],
        "status": "draft",
        "current_revision_id": revision_id,
    })
    if plan.get("task_memory_id"):
        db.update_memory_item(plan["task_memory_id"], {
            "task_status": "reopened",
            "task_updated_by": "user",
        })
    _complete_plan_turn(chat_id, turn_id)
    db.insert_plan_event(
        event_id=make_id("planevt"),
        plan_id=plan_id,
        chat_id=chat_id,
        event_type="plan_revised",
        revision_id=revision_id,
        turn_id=turn_id,
        summary=plan_output["change_summary"],
    )
    return {
        "plan": _serialize_plan(db.get_plan(plan_id)),
        "revision": _serialize_revision(db.get_plan_revision(revision_id)),
        "display_message": "计划已更新，请在计划面板查看。",
    }


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: str) -> dict[str, Any]:
    """批准当前计划版本，并创建可被聊天链路召回的 task_state 记忆。"""
    plan = db.get_plan(plan_id.strip())
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan["status"] not in PLAN_APPROVABLE_STATUSES:
        raise HTTPException(status_code=409, detail="plan cannot be approved in current status")

    revision = db.get_current_plan_revision(plan_id)
    if not revision:
        raise HTTPException(status_code=409, detail="plan has no current revision")

    chat_id = plan["chat_id"]
    turn_id, _ = _create_plan_turn(chat_id, "", "同意开始执行计划", False, False)
    checklist = _safe_json_loads(revision.get("checklist_json"), [])
    content_lines = [plan["objective"]]
    for index, step in enumerate(checklist[:5], start=1):
        content_lines.append(f"{index}. {step.get('title', '')}")
    task_content = "\n".join(line for line in content_lines if line.strip())
    checklist_summary = _summarize_checklist_for_memory(checklist)
    task_evidence = "\n".join(
        part
        for part in [
            "用户点击同意开始执行计划。",
            f"计划目标：{plan.get('objective', '')}",
            f"批准步骤：\n{checklist_summary}" if checklist_summary else "",
        ]
        if part
    )
    if plan.get("task_memory_id") and db.get_memory_item(plan["task_memory_id"]):
        memory = patch_memory(
            plan["task_memory_id"],
            content=task_content,
            evidence=task_evidence,
            tags=["todo", "next_step"],
            importance=0.8,
            confidence=1.0,
            stability=0.5,
            status="active",
            task_status="open",
            task_updated_by="user",
            plan_id=plan_id,
        )
    else:
        memory = create_manual_memory(
            content=task_content,
            memory_type="task_state",
            evidence=task_evidence,
            scope_chat_id=chat_id,
            tags=["todo", "next_step"],
            importance=0.8,
            confidence=1.0,
            stability=0.5,
            task_status="open",
            task_updated_by="user",
            plan_id=plan_id,
        )
    for index, step in enumerate(checklist, start=1):
        db.insert_plan_step(
            step_id=make_id("planstep"),
            plan_id=plan_id,
            revision_id=revision["revision_id"],
            step_index=index,
            title=str(step.get("title") or "").strip(),
            detail=str(step.get("detail") or "").strip(),
            source_turn_id=turn_id,
        )
    db.update_chat_plan(plan_id, {
        "status": "executing",
        "approved_revision_id": revision["revision_id"],
        "task_memory_id": memory["memory_id"],
        "approved_turn_id": turn_id,
    })
    assistant_message = "计划已进入执行，已创建当前任务状态。"
    _complete_plan_turn(chat_id, turn_id, assistant_message)
    db.insert_plan_event(
        event_id=make_id("planevt"),
        plan_id=plan_id,
        chat_id=chat_id,
        event_type="plan_approved",
        revision_id=revision["revision_id"],
        turn_id=turn_id,
        summary=assistant_message,
    )
    return {
        "plan": _serialize_plan(db.get_plan(plan_id)),
        "task_memory": memory,
        "display_message": assistant_message,
    }


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: str) -> dict[str, Any]:
    """取消计划，并把关联 task_state 标记为 cancelled。"""
    plan = db.get_plan(plan_id.strip())
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    now = db._now()
    db.update_chat_plan(plan_id, {"status": "cancelled", "completed_at": now})
    if plan.get("task_memory_id"):
        db.update_memory_item(plan["task_memory_id"], {
            "task_status": "cancelled",
            "task_updated_by": "user",
            "plan_id": plan_id,
            "evidence": "用户取消计划，任务状态由计划取消流程标记为 cancelled。",
        })
    db.insert_plan_event(
        event_id=make_id("planevt"),
        plan_id=plan_id,
        chat_id=plan["chat_id"],
        event_type="plan_cancelled",
        summary="用户取消计划。",
    )
    return {"plan": _serialize_plan(db.get_plan(plan_id))}


@router.post("/plans/{plan_id}/complete")
def complete_plan(plan_id: str) -> dict[str, Any]:
    """把执行中的计划和所有步骤标记为完成。"""
    plan = db.get_plan(plan_id.strip())
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    if plan["status"] == "done":
        _mark_plan_task_memory_done(plan_id, plan)
        return {"plan": _serialize_plan(plan)}
    if plan["status"] != "executing":
        raise HTTPException(status_code=409, detail="plan cannot be completed in current status")

    now = db._now()
    revision_id = plan.get("approved_revision_id") or plan.get("current_revision_id") or ""
    db.update_chat_plan(plan_id, {"status": "done", "completed_at": now})
    db.update_plan_steps_status(
        plan_id=plan_id,
        revision_id=revision_id,
        status="done",
        updated_by="assistant",
    )
    _mark_plan_task_memory_done(plan_id, plan)
    db.insert_plan_event(
        event_id=make_id("planevt"),
        plan_id=plan_id,
        chat_id=plan["chat_id"],
        revision_id=revision_id,
        event_type="plan_completed",
        summary="计划已一次性执行完成。",
    )
    return {"plan": _serialize_plan(db.get_plan(plan_id))}


@router.get("/chats/{chat_id}/plans/active")
def get_active_chat_plan(chat_id: str) -> dict[str, Any]:
    """读取某个 chat 当前活跃的计划。"""
    return {"plan": _serialize_plan(db.get_active_plan(chat_id.strip()))}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str) -> dict[str, Any]:
    """按计划 ID 读取完整计划详情。"""
    plan = _serialize_plan(db.get_plan(plan_id.strip()))
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return {"plan": plan}
