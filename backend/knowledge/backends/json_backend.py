"""JSON 文件存储后端。

向量和元数据都存本地 JSON 文件，召回用 numpy 余弦相似度。
适合开发和小规模使用。生产环境切换到 QdrantMongoBackend。
"""

import json
import threading
from pathlib import Path
from typing import Optional

from knowledge.backend import KnowledgeBackend
from knowledge.models import OperationRecord
from knowledge.matcher import cosine, combined_score


class JsonBackend(KnowledgeBackend):
    """向量 + 元数据都在本地 JSON。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _flush(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def save(self, record: OperationRecord, vector: list[float]) -> str:
        with self._lock:
            self._records[record.id] = {
                "record": record.to_dict(),
                "vector": vector,
            }
            self._flush()
        return record.id

    def query(
        self,
        vector: list[float],
        fingerprint: dict,
        top_k: int,
        min_score: float,
    ) -> list[tuple[float, OperationRecord]]:
        results: list[tuple[float, OperationRecord]] = []
        with self._lock:
            entries = list(self._records.values())
        for entry in entries:
            vec = entry.get("vector", [])
            vec_sim = cosine(vector, vec)
            fp_record = entry["record"].get("page_fingerprint", {})
            score = combined_score(vec_sim, fingerprint, fp_record)
            if score >= min_score:
                rec = OperationRecord.from_dict(entry["record"])
                results.append((score, rec))
        # 综合分降序；同分时质量分高的优先
        results.sort(key=lambda x: (x[0], x[1].quality_score()), reverse=True)
        return results[:top_k]

    def update_usage(self, record_id: str, success: bool) -> None:
        with self._lock:
            entry = self._records.get(record_id)
            if not entry:
                return
            rec = entry["record"]
            rec["used_count"] = rec.get("used_count", 0) + 1
            if success:
                rec["success_after_use"] = rec.get("success_after_use", 0) + 1
            self._flush()

    def get(self, record_id: str) -> Optional[OperationRecord]:
        with self._lock:
            entry = self._records.get(record_id)
        return OperationRecord.from_dict(entry["record"]) if entry else None

    def list_all(self, limit: int = 100) -> list[OperationRecord]:
        with self._lock:
            entries = list(self._records.values())
        return [OperationRecord.from_dict(e["record"]) for e in entries[:limit]]

    def delete(self, record_id: str) -> bool:
        with self._lock:
            if record_id in self._records:
                del self._records[record_id]
                self._flush()
                return True
        return False
