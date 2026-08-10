"""知识库 API 端点。

提供操作记录的保存、查询、引用回报、列表和删除。
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from knowledge.store import (
    save_record,
    query_records,
    report_usage,
    get_record,
    list_records,
    delete_record,
    new_record_id,
)
from knowledge.models import OperationRecord

router = APIRouter(prefix="/v1/knowledge", tags=["知识库"])


class RecordSaveRequest(BaseModel):
    task_description: str
    trigger_prompt: str = ""
    source: str = "confirmed"
    page_fingerprint: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    user_note: str = ""
    model: str = ""              # 用于 recorded 记录的 intent 补全


class UsageReportRequest(BaseModel):
    record_id: str
    success: bool


class QueryRequest(BaseModel):
    task_description: str
    page_fingerprint: dict[str, Any] = {}
    top_k: Optional[int] = None
    min_score: Optional[float] = None


def _embed(text: str) -> list[float]:
    """生成 embedding 向量。失败时抛异常。"""
    from rag.embedder import embed_text
    return embed_text(text)


@router.post("/record")
def create_record(item: RecordSaveRequest) -> dict[str, Any]:
    """保存操作记录（用户确认接受 / 录制完成）。"""
    if not item.task_description.strip():
        raise HTTPException(400, "task_description 不能为空")
    if not item.steps:
        raise HTTPException(400, "steps 不能为空")

    from knowledge.cleaner import clean_steps, clean_fingerprint
    cleaned_steps = clean_steps(item.steps)
    if not cleaned_steps:
        raise HTTPException(400, "清洗后无有效步骤")

    # recorded 记录缺少 intent，调 LLM 批量补全（失败不阻断）
    if item.source == "recorded" and item.model:
        from knowledge.enricher import enrich_intents
        cleaned_steps = enrich_intents(item.task_description, cleaned_steps, item.model)

    record = OperationRecord(
        id=new_record_id(),
        task_description=item.task_description,
        trigger_prompt=item.trigger_prompt,
        source=item.source,
        page_fingerprint=clean_fingerprint(item.page_fingerprint),
        steps=cleaned_steps,
        user_note=item.user_note,
    )

    try:
        vector = _embed(item.task_description)
    except Exception as exc:
        raise HTTPException(502, f"生成向量失败: {exc}") from exc

    rid = save_record(record, vector)
    return {"id": rid, "status": "saved"}


@router.post("/query")
def query(item: QueryRequest) -> list[dict[str, Any]]:
    """查询相似记录（调试用）。"""
    try:
        vector = _embed(item.task_description)
    except Exception as exc:
        raise HTTPException(502, f"生成向量失败: {exc}") from exc

    results = query_records(vector, item.page_fingerprint, item.top_k, item.min_score)
    return [
        {"score": round(score, 4), "record": rec.to_dict()}
        for score, rec in results
    ]


@router.post("/usage")
def report(item: UsageReportRequest) -> dict[str, Any]:
    """回报引用后的成功/失败，更新质量评分。"""
    report_usage(item.record_id, item.success)
    return {"status": "ok"}


@router.get("/records")
def list_all(limit: int = 100) -> list[dict[str, Any]]:
    """列出所有记录（管理用）。"""
    return [rec.to_dict() for rec in list_records(limit)]


@router.delete("/record/{record_id}")
def remove(record_id: str) -> dict[str, Any]:
    """删除记录。"""
    ok = delete_record(record_id)
    if not ok:
        raise HTTPException(404, f"记录 {record_id} 不存在")
    return {"status": "deleted"}
