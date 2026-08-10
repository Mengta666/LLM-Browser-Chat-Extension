"""Qdrant + MongoDB 存储后端（未来实现）。

向量入 Qdrant，元数据入 MongoDB。接口与 JsonBackend 完全一致。
当前为 stub，Phase 5 实现。切换方式：.env 设 KNOWLEDGE_BACKEND=qdrant_mongo。
"""

from typing import Optional

from knowledge.backend import KnowledgeBackend
from knowledge.models import OperationRecord


class QdrantMongoBackend(KnowledgeBackend):
    """向量 → Qdrant，元数据 → MongoDB。"""

    def __init__(self):
        raise NotImplementedError(
            "QdrantMongoBackend 尚未实现（Phase 5）。当前请使用 KNOWLEDGE_BACKEND=json"
        )

    def save(self, record: OperationRecord, vector: list[float]) -> str:
        raise NotImplementedError

    def query(self, vector, fingerprint, top_k, min_score, vec_min=0.0):
        raise NotImplementedError

    def update_usage(self, record_id: str, success: bool) -> None:
        raise NotImplementedError

    def get(self, record_id: str) -> Optional[OperationRecord]:
        raise NotImplementedError

    def list_all(self, limit: int = 100) -> list[OperationRecord]:
        raise NotImplementedError

    def delete(self, record_id: str) -> bool:
        raise NotImplementedError
