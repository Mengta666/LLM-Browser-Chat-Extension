"""知识库对外统一接口 + 后端工厂。

业务层只调用本模块的函数，不直接接触具体后端实现。
切换后端时只需改 .env 的 KNOWLEDGE_BACKEND，业务代码零改动。
"""

import os
import time
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from knowledge.backend import KnowledgeBackend
from knowledge.models import OperationRecord

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "records"
KNOWLEDGE_BACKEND = os.getenv("KNOWLEDGE_BACKEND", "json")
KNOWLEDGE_MIN_SCORE = float(os.getenv("KNOWLEDGE_MIN_SCORE", "0.65"))
KNOWLEDGE_TOP_K = int(os.getenv("KNOWLEDGE_TOP_K", "1"))


def _create_backend() -> KnowledgeBackend:
    if KNOWLEDGE_BACKEND == "json":
        from knowledge.backends.json_backend import JsonBackend
        return JsonBackend(KNOWLEDGE_DIR / "operations.json")
    if KNOWLEDGE_BACKEND == "qdrant_mongo":
        from knowledge.backends.qdrant_mongo_backend import QdrantMongoBackend
        return QdrantMongoBackend()
    raise ValueError(f"未知知识库后端: {KNOWLEDGE_BACKEND}")


_backend = _create_backend()


def new_record_id() -> str:
    return f"rec_{uuid.uuid4().hex[:16]}"


def save_record(record: OperationRecord, vector: list[float]) -> str:
    """保存记录（业务层入口）。"""
    if not record.created_at:
        record.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    return _backend.save(record, vector)


def query_records(
    task_vector: list[float],
    fingerprint: dict,
    top_k: int = None,
    min_score: float = None,
) -> list[tuple[float, OperationRecord]]:
    """查询相似记录（业务层入口）。"""
    return _backend.query(
        task_vector,
        fingerprint,
        top_k if top_k is not None else KNOWLEDGE_TOP_K,
        min_score if min_score is not None else KNOWLEDGE_MIN_SCORE,
    )


def report_usage(record_id: str, success: bool) -> None:
    """回报引用结果（业务层入口）。"""
    _backend.update_usage(record_id, success)


def get_record(record_id: str) -> Optional[OperationRecord]:
    return _backend.get(record_id)


def list_records(limit: int = 100) -> list[OperationRecord]:
    return _backend.list_all(limit)


def delete_record(record_id: str) -> bool:
    return _backend.delete(record_id)
