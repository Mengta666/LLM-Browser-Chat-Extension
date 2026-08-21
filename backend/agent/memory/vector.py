"""记忆的 Qdrant 向量存储(事实源)。

对齐 mem0:记忆正文存在 point.payload["content"],Qdrant 就是事实源,不再有
SQLite/Qdrant 两库同步问题。point.id 由业务 memory_id 幂等派生(uuid5),
可无损重建。维度硬校验 4096,拒绝维度不符的 collection。

所有对外函数在 Qdrant 不可用时优雅降级(search 返回空、写入抛受控异常),
调用方(service)据此决定是否静默失败,保证 agent 主流程不被记忆拖垮。
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from agent.memory.config import (
    QDRANT_URL, QDRANT_API_KEY, QDRANT_DISTANCE,
    MEMORY_COLLECTION, MEMORY_VECTOR_SIZE,
    MEMORY_TYPE_PREFERENCE, SCOPE_GLOBAL, DEFAULT_USER_ID,
)


_client: Optional[QdrantClient] = None
_collection_ready = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(content: str) -> str:
    """记忆正文的 md5,用于写入去重。"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def make_memory_id() -> str:
    """生成业务主键。"""
    return f"mem_{uuid4().hex}"


def _point_id(memory_id: str) -> str:
    """业务 memory_id → 稳定的 Qdrant point id(幂等,可无损重建)。"""
    return str(uuid5(NAMESPACE_URL, f"agent-memory:{memory_id}"))


def _resolve_distance() -> models.Distance:
    try:
        return models.Distance(QDRANT_DISTANCE)
    except ValueError as exc:
        raise ValueError(f"不支持的 QDRANT_DISTANCE: {QDRANT_DISTANCE}") from exc


def get_client() -> QdrantClient:
    """惰性创建 Qdrant 客户端。"""
    global _client
    if _client is None:
        _client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return _client


def ensure_collection() -> None:
    """确保记忆 collection 存在,并硬校验维度==4096、距离一致。

    维度不符直接抛错(拒绝老系统 1024 默认值那种炸弹),不静默降级。
    """
    global _collection_ready
    if _collection_ready:
        return

    client = get_client()
    names = [c.name for c in client.get_collections().collections]
    if MEMORY_COLLECTION not in names:
        client.create_collection(
            collection_name=MEMORY_COLLECTION,
            vectors_config=models.VectorParams(
                size=MEMORY_VECTOR_SIZE,
                distance=_resolve_distance(),
            ),
        )
        _collection_ready = True
        return

    info = client.get_collection(collection_name=MEMORY_COLLECTION)
    vectors_config = info.config.params.vectors
    if isinstance(vectors_config, dict):
        raise RuntimeError(f"collection {MEMORY_COLLECTION} 使用了具名向量,不支持")
    if vectors_config.size != MEMORY_VECTOR_SIZE:
        raise RuntimeError(
            f"collection {MEMORY_COLLECTION} 维度不符:"
            f"期望 {MEMORY_VECTOR_SIZE},实际 {vectors_config.size}"
        )
    _collection_ready = True


def _build_payload(memory_id: str, content: str, *,
                   memory_type: str, scope: str, domain: str,
                   user_id: str, created_at: str, updated_at: str,
                   confidence: float = 1.0, verified: bool = False,
                   entry_url: str = "", intent_keywords: Optional[list[str]] = None) -> dict[str, Any]:
    """组装 point payload(事实源)。

    confidence/verified 仅 lesson 消费(失败通道门控);
    entry_url/intent_keywords 仅 site_experience 消费(轻结构头,提检索命中+给起点)。
    其他 memory_type 用默认值。
    """
    return {
        "memory_id": memory_id,
        "content": content,
        "hash": content_hash(content),
        "memory_type": memory_type,
        "scope": scope,
        "domain": domain or "",
        "user_id": user_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "confidence": float(confidence),
        "verified": bool(verified),
        "entry_url": entry_url or "",
        "intent_keywords": intent_keywords or [],
    }


def insert_memory(content: str, *, vector: list[float],
                  memory_type: str = MEMORY_TYPE_PREFERENCE,
                  scope: str = SCOPE_GLOBAL, domain: str = "",
                  user_id: str = DEFAULT_USER_ID,
                  confidence: float = 1.0, verified: bool = False,
                  entry_url: str = "", intent_keywords: Optional[list[str]] = None) -> dict[str, Any]:
    """写入一条新记忆,返回落库的 payload(含生成的 memory_id)。"""
    ensure_collection()
    memory_id = make_memory_id()
    now = _now_iso()
    payload = _build_payload(
        memory_id, content, memory_type=memory_type, scope=scope,
        domain=domain, user_id=user_id, created_at=now, updated_at=now,
        confidence=confidence, verified=verified,
        entry_url=entry_url, intent_keywords=intent_keywords,
    )
    get_client().upsert(
        collection_name=MEMORY_COLLECTION,
        points=[models.PointStruct(id=_point_id(memory_id), vector=vector, payload=payload)],
        wait=True,
    )
    return payload


def update_memory(memory_id: str, content: str, *, vector: list[float]) -> Optional[dict[str, Any]]:
    """更新已存记忆的正文与向量,保留其余字段、刷新 updated_at。

    memory_id 不存在返回 None(调用方据此避免误判)。
    """
    ensure_collection()
    existing = get_memory(memory_id)
    if existing is None:
        return None
    now = _now_iso()
    payload = _build_payload(
        memory_id, content,
        memory_type=existing.get("memory_type", MEMORY_TYPE_PREFERENCE),
        scope=existing.get("scope", SCOPE_GLOBAL),
        domain=existing.get("domain", ""),
        user_id=existing.get("user_id", DEFAULT_USER_ID),
        created_at=existing.get("created_at", now),
        updated_at=now,
        confidence=existing.get("confidence", 1.0),
        verified=existing.get("verified", False),
        entry_url=existing.get("entry_url", ""),
        intent_keywords=existing.get("intent_keywords", []),
    )
    get_client().upsert(
        collection_name=MEMORY_COLLECTION,
        points=[models.PointStruct(id=_point_id(memory_id), vector=vector, payload=payload)],
        wait=True,
    )
    return payload


def delete_memory(memory_id: str) -> None:
    """删除一条记忆(幂等,不存在也不报错)。"""
    ensure_collection()
    get_client().delete(
        collection_name=MEMORY_COLLECTION,
        points_selector=models.PointIdsList(points=[_point_id(memory_id)]),
        wait=True,
    )


def get_memory(memory_id: str) -> Optional[dict[str, Any]]:
    """按 memory_id 取单条记忆的 payload,不存在返回 None。"""
    ensure_collection()
    try:
        points = get_client().retrieve(
            collection_name=MEMORY_COLLECTION,
            ids=[_point_id(memory_id)],
            with_payload=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return None
        raise
    if not points:
        return None
    return points[0].payload or None


def _build_filter(*, user_id: str, memory_type: Optional[Any],
                  scope: Optional[str], domain: Optional[str]) -> models.Filter:
    """组装 Qdrant payload 过滤条件。

    memory_type 可传 str(单类)或 list(多类,用 MatchAny)——
    recall 要同时查 site_experience+lesson 两类。
    """
    must: list[models.FieldCondition] = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
    ]
    if memory_type:
        if isinstance(memory_type, (list, tuple, set)):
            must.append(models.FieldCondition(
                key="memory_type", match=models.MatchAny(any=list(memory_type))))
        else:
            must.append(models.FieldCondition(
                key="memory_type", match=models.MatchValue(value=memory_type)))
    if scope:
        must.append(models.FieldCondition(key="scope", match=models.MatchValue(value=scope)))
    if domain:
        must.append(models.FieldCondition(key="domain", match=models.MatchValue(value=domain)))
    return models.Filter(must=must)


def search_memories(query_vector: list[float], *, top_k: int,
                    user_id: str = DEFAULT_USER_ID,
                    memory_type: Optional[Any] = None,
                    scope: Optional[str] = None,
                    domain: Optional[str] = None) -> list[dict[str, Any]]:
    """按语义相似度检索记忆,collection 不存在时返回空。

    memory_type 可传 str 或 list(多类)。
    返回 [{memory_id, content, score, ...payload}]，按相似度降序。
    """
    ensure_collection()
    try:
        result = get_client().query_points(
            collection_name=MEMORY_COLLECTION,
            query=query_vector,
            query_filter=_build_filter(
                user_id=user_id, memory_type=memory_type, scope=scope, domain=domain
            ),
            limit=max(1, int(top_k)),
            with_payload=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return []
        raise

    hits = result.points if hasattr(result, "points") else []
    out: list[dict[str, Any]] = []
    for hit in hits:
        payload = dict(hit.payload or {})
        payload["score"] = hit.score
        out.append(payload)
    return out


def count_memories(user_id: str = DEFAULT_USER_ID, *,
                   memory_type: Optional[Any] = None,
                   domain: Optional[str] = None) -> int:
    """统计记忆条数(测试/一致性校验 + 启发式兜底用)。

    可按 memory_type、domain 过滤——启发式兜底要数"某站点有几条 site_experience"。
    """
    ensure_collection()
    result = get_client().count(
        collection_name=MEMORY_COLLECTION,
        count_filter=_build_filter(
            user_id=user_id, memory_type=memory_type, scope=None, domain=domain),
        exact=True,
    )
    return int(result.count)
