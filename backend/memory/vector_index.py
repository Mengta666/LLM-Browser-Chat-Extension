"""长期记忆的 Qdrant 向量索引。

SQLite 仍是事实来源；本模块只维护 active 记忆行的语义检索索引。
"""
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from rag.embedder import embed_text

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
QDRANT_MEMORY_COLLECTION = os.getenv("QDRANT_MEMORY_COLLECTION", "browser_agent_memories")
QDRANT_VECTOR_SIZE = int(os.getenv("QDRANT_VECTOR_SIZE", "1024"))
QDRANT_DISTANCE = os.getenv("QDRANT_DISTANCE", "Cosine")

client = QdrantClient(
    url=QDRANT_URL,
    api_key=QDRANT_API_KEY,
)


def _resolve_distance() -> models.Distance:
    """把环境变量里的距离名称转换为 Qdrant SDK 枚举。"""
    try:
        return models.Distance(QDRANT_DISTANCE)
    except ValueError as exc:
        raise ValueError(f"Unsupported QDRANT_DISTANCE: {QDRANT_DISTANCE}") from exc


def _point_id(memory_id: str) -> str:
    """把业务 memory_id 映射成稳定的 Qdrant point id。"""
    return str(uuid5(NAMESPACE_URL, f"browser-agent-memory:{memory_id}"))


def build_memory_embedding_text(memory_item: dict[str, Any]) -> str:
    """构造写入记忆向量库的 embedding 文本。"""
    return "\n".join([
        f"记忆内容：{memory_item.get('content', '')}",
        f"类型：{memory_item.get('memory_type', '')}",
        f"计划：{memory_item.get('plan_id', '')}",
        f"任务状态：{memory_item.get('task_status', '')}",
        f"适用模式：{', '.join(memory_item.get('mode_affinity', []) or [])}",
        f"标签：{', '.join(memory_item.get('tags', []) or [])}",
    ]).strip()


def ensure_memory_collection(vector_size: int | None = None) -> None:
    """确保记忆 collection 存在，并校验向量维度和距离配置。"""
    resolved_size = vector_size or QDRANT_VECTOR_SIZE
    collections = client.get_collections().collections
    collection_names = [collection.name for collection in collections]
    if QDRANT_MEMORY_COLLECTION not in collection_names:
        client.create_collection(
            collection_name=QDRANT_MEMORY_COLLECTION,
            vectors_config=models.VectorParams(
                size=resolved_size,
                distance=_resolve_distance(),
            ),
        )
        return

    collection_info = client.get_collection(collection_name=QDRANT_MEMORY_COLLECTION)
    vectors_config = collection_info.config.params.vectors
    if isinstance(vectors_config, dict):
        raise RuntimeError("Named vectors are not supported for memory collection")
    if vectors_config.size != resolved_size:
        raise RuntimeError(
            f"Collection {QDRANT_MEMORY_COLLECTION} vector size mismatch: "
            f"expected {resolved_size}, got {vectors_config.size}"
        )
    if vectors_config.distance != _resolve_distance():
        raise RuntimeError(
            f"Collection {QDRANT_MEMORY_COLLECTION} distance mismatch: "
            f"expected {_resolve_distance()}, got {vectors_config.distance}"
        )


def upsert_memory(memory_item: dict[str, Any]) -> None:
    """把一条 active 记忆写入或覆盖到 Qdrant。"""
    memory_id = str(memory_item.get("memory_id") or "").strip()
    if not memory_id:
        raise ValueError("memory_id is required")

    vector_text = build_memory_embedding_text(memory_item)
    vector = embed_text(vector_text)
    ensure_memory_collection(len(vector))
    if len(vector) != QDRANT_VECTOR_SIZE:
        raise ValueError(
            f"memory vector size mismatch: expected {QDRANT_VECTOR_SIZE}, got {len(vector)}"
        )

    payload = {
        "memory_id": memory_id,
        "user_id": memory_item.get("user_id", "local"),
        "memory_type": memory_item.get("memory_type", ""),
        "scope": memory_item.get("scope", "user"),
        "scope_chat_id": memory_item.get("scope_chat_id", ""),
        "plan_id": memory_item.get("plan_id", ""),
        "status": memory_item.get("status", "active"),
        "task_status": memory_item.get("task_status", ""),
        "task_updated_by": memory_item.get("task_updated_by", ""),
        "mode_affinity": memory_item.get("mode_affinity", []) or [],
        "importance": float(memory_item.get("importance", 0.5) or 0.5),
        "confidence": float(memory_item.get("confidence", 0.5) or 0.5),
        "stability": float(memory_item.get("stability", 0.5) or 0.5),
        "classification_reason": memory_item.get("classification_reason", ""),
        "policy_version": memory_item.get("policy_version", ""),
        "updated_at": memory_item.get("updated_at", ""),
    }
    client.upsert(
        collection_name=QDRANT_MEMORY_COLLECTION,
        points=[
            models.PointStruct(
                id=_point_id(memory_id),
                vector=vector,
                payload=payload,
            )
        ],
        wait=True,
    )


def _build_search_filter(filters: dict[str, Any]) -> models.Filter:
    """把业务过滤条件转换成 Qdrant payload filter。"""
    must: list[models.FieldCondition] = []
    user_id = str(filters.get("user_id") or "local")
    status = str(filters.get("status") or "active")
    memory_types = [item for item in filters.get("memory_types", []) if item]
    mode_affinity = [item for item in filters.get("mode_affinity", []) if item]
    scope = str(filters.get("scope") or "").strip()
    scope_chat_id = str(filters.get("scope_chat_id") or "").strip()
    task_statuses = [item for item in filters.get("task_statuses", []) if item]

    must.append(models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)))
    must.append(models.FieldCondition(key="status", match=models.MatchValue(value=status)))
    if memory_types:
        must.append(models.FieldCondition(key="memory_type", match=models.MatchAny(any=memory_types)))
    if mode_affinity:
        must.append(models.FieldCondition(key="mode_affinity", match=models.MatchAny(any=mode_affinity)))
    if scope:
        must.append(models.FieldCondition(key="scope", match=models.MatchValue(value=scope)))
    if scope_chat_id:
        must.append(models.FieldCondition(key="scope_chat_id", match=models.MatchValue(value=scope_chat_id)))
    if task_statuses:
        must.append(models.FieldCondition(key="task_status", match=models.MatchAny(any=task_statuses)))
    return models.Filter(must=must)


def search_memories(query_text: str, filters: dict[str, Any], top_k: int = 5) -> list[dict[str, Any]]:
    """按语义相似度搜索记忆，collection 不存在时返回空结果。"""
    query = str(query_text or "").strip()
    if not query:
        return []

    query_vector = embed_text(query)
    try:
        ensure_memory_collection(len(query_vector))
        search_results = client.query_points(
            collection_name=QDRANT_MEMORY_COLLECTION,
            query=query_vector,
            query_filter=_build_search_filter(filters),
            limit=max(1, int(top_k)),
            with_payload=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return []
        raise

    hits = search_results.points if hasattr(search_results, "points") else []
    return [
        {
            "point_id": hit.id,
            "score": hit.score,
            "payload": hit.payload or {},
            "memory_id": (hit.payload or {}).get("memory_id", ""),
        }
        for hit in hits
    ]


def delete_memory_vector(memory_id: str) -> None:
    """删除单条记忆对应的 Qdrant point。"""
    normalized_id = str(memory_id or "").strip()
    if not normalized_id:
        return

    try:
        ensure_memory_collection()
        client.delete(
            collection_name=QDRANT_MEMORY_COLLECTION,
            points_selector=models.PointIdsList(points=[_point_id(normalized_id)]),
            wait=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return
        raise


def build_memory_embedding_text(memory_item: dict[str, Any]) -> str:
    """构造写入记忆向量库的 embedding 文本。"""
    return "\n".join([
        f"记忆内容：{memory_item.get('content', '')}",
        f"类型：{memory_item.get('memory_type', '')}",
        f"分类原因：{memory_item.get('classification_reason', '')}",
        f"适用模式：{', '.join(memory_item.get('mode_affinity', []) or [])}",
        f"标签：{', '.join(memory_item.get('tags', []) or [])}",
    ]).strip()
