"""知识库存储后端抽象接口。

JsonBackend（当前）和 QdrantMongoBackend（未来）都实现此接口。
业务层只依赖此抽象，切换后端时业务代码零改动。
"""

from abc import ABC, abstractmethod
from typing import Optional

from knowledge.models import OperationRecord


class KnowledgeBackend(ABC):
    """知识库存储后端。"""

    @abstractmethod
    def save(self, record: OperationRecord, vector: list[float]) -> str:
        """保存一条记录及其向量，返回 record id。"""
        ...

    @abstractmethod
    def query(
        self,
        vector: list[float],
        fingerprint: dict,
        top_k: int,
        min_score: float,
        vec_min: float = 0.0,
    ) -> list[tuple[float, OperationRecord]]:
        """向量召回 + 指纹加权，返回 [(score, record), ...] 按分降序。

        vec_min: 向量相似度独立下限。综合分含指纹/域名地板分，
        同域同页时仅靠地板分即可越过 min_score，故对向量另设下限避免误召回。
        """
        ...

    @abstractmethod
    def update_usage(self, record_id: str, success: bool) -> None:
        """回报引用结果，更新 used_count / success_after_use。"""
        ...

    @abstractmethod
    def get(self, record_id: str) -> Optional[OperationRecord]:
        """按 id 获取记录。"""
        ...

    @abstractmethod
    def list_all(self, limit: int = 100) -> list[OperationRecord]:
        """列出所有记录。"""
        ...

    @abstractmethod
    def delete(self, record_id: str) -> bool:
        """删除记录，返回是否成功。"""
        ...
