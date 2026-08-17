"""记忆相关 API 模块。

后续用于提供用户记忆的查看、保存、删除和召回接口。
当前记忆能力尚未接入主聊天链路。
"""
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from memory.policy_v2 import normalize_memory_row
from memory.store import create_manual_memory, delete_memory, patch_memory, rerun_memory_job
from storage.db import db

router = APIRouter(prefix="/api", tags=["memory"])


class MemoryCreateRequest(BaseModel):
    """手工创建记忆时前端提交的字段。"""

    memory_type: str
    content: str
    evidence: str = ""
    scope_chat_id: str = ""
    mode_affinity: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.9
    stability: float = 0.9
    task_status: str = ""
    task_updated_by: str = ""
    plan_id: str = ""


class MemoryPatchRequest(BaseModel):
    """局部更新记忆时允许修改的字段；None 表示保持原值。"""

    content: str | None = None
    evidence: str | None = None
    classification_reason: str | None = None
    scope_chat_id: str | None = None
    mode_affinity: list[str] | None = None
    tags: list[str] | None = None
    importance: float | None = None
    confidence: float | None = None
    stability: float | None = None
    status: str | None = None
    task_status: str | None = None
    task_updated_by: str | None = None
    plan_id: str | None = None


def _safe_json_loads(value: Any, default: Any) -> Any:
    """宽松解析 JSON 字段，兼容已经是 dict/list 的调用方。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _summarize_memory_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """把后台记忆抽取任务压缩成前端调试面板需要的摘要。"""
    if not job:
        return None
    output = _safe_json_loads(job.get("output_json"), {})
    return {
        "job_id": job.get("job_id", ""),
        "status": job.get("status", ""),
        "error_message": job.get("error_message", ""),
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "completed_at": job.get("completed_at", ""),
        "decisions": output.get("decisions", []),
        "applied": output.get("applied", []),
        "validation_warnings": output.get("validation_warnings", []),
    }


@router.get("/memories")
def list_memories(
        status: str = Query(default="active"),
        memory_type: str = Query(default=""),
        limit: int = Query(default=100, ge=1, le=500),
        include_debug: bool = Query(default=False),
):
    """按状态和类型列出记忆；调试模式会附带最近一次 writer job。"""
    memory_types = [memory_type] if memory_type else None
    rows = db.list_memory_items(status=status, memory_types=memory_types, limit=limit)
    latest_jobs = {}
    if include_debug:
        latest_jobs = db.list_latest_memory_jobs_by_turn_ids([
            row.get("source_turn_id", "")
            for row in rows
            if row.get("source_turn_id")
        ])
    return {
        "memories": [
            {
                **normalize_memory_row(row),
                **({
                    "debug": {
                        "latest_job": _summarize_memory_job(latest_jobs.get(row.get("source_turn_id", "")))
                    }
                } if include_debug else {}),
            }
            for row in rows
        ],
    }


@router.post("/memories")
def create_memory(item: MemoryCreateRequest):
    """创建一条手工记忆，并同步写入向量索引。"""
    try:
        memory = create_manual_memory(
            content=item.content,
            memory_type=item.memory_type,
            evidence=item.evidence,
            scope_chat_id=item.scope_chat_id,
            mode_affinity=item.mode_affinity,
            tags=item.tags,
            importance=item.importance,
            confidence=item.confidence,
            stability=item.stability,
            task_status=item.task_status,
            task_updated_by=item.task_updated_by,
            plan_id=item.plan_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory index error: {exc}") from exc
    return {"memory": memory}


@router.patch("/memories/{memory_id}")
def update_memory(memory_id: str, item: MemoryPatchRequest):
    """更新一条已有记忆，并按状态刷新或删除向量索引。"""
    try:
        memory = patch_memory(
            memory_id=memory_id,
            content=item.content,
            evidence=item.evidence,
            classification_reason=item.classification_reason,
            scope_chat_id=item.scope_chat_id,
            mode_affinity=item.mode_affinity,
            tags=item.tags,
            importance=item.importance,
            confidence=item.confidence,
            stability=item.stability,
            status=item.status,
            task_status=item.task_status,
            task_updated_by=item.task_updated_by,
            plan_id=item.plan_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory index error: {exc}") from exc
    return {"memory": memory}


@router.delete("/memories/{memory_id}")
def remove_memory(memory_id: str):
    """软删除记忆，同时尝试删除对应向量。"""
    try:
        delete_memory(memory_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory index error: {exc}") from exc
    return {"ok": True, "memory_id": memory_id}


@router.get("/memory_jobs/{job_id}")
def get_memory_job(job_id: str):
    """读取指定记忆抽取任务的完整输入输出，供调试使用。"""
    job = db.get_memory_extraction_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="memory job not found")
    return {
        "job": {
            **job,
            "input": _safe_json_loads(job.get("input_json"), {}),
            "output": _safe_json_loads(job.get("output_json"), {}),
        }
    }


@router.post("/memory_jobs/{job_id}/rerun")
def rerun_job(job_id: str):
    """重新运行一次已有 memory writer job。"""
    try:
        output = rerun_memory_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"memory writer error: {exc}") from exc
    return {"job_id": job_id, "output": output}
