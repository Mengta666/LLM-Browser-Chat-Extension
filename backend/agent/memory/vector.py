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
    DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME,
    MEMORY_TYPE_PREFERENCE, SCOPE_GLOBAL, DEFAULT_USER_ID,
)
from agent.memory.sparse import to_sparse_vector


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
    """确保记忆 collection 存在:dense(4096/Cosine 具名)+ sparse(IDF)双向量。

    hybrid 检索需具名双向量。维度硬校验 dense==4096(拒绝维度炸弹)。
    """
    global _collection_ready
    if _collection_ready:
        return

    client = get_client()
    names = [c.name for c in client.get_collections().collections]
    if MEMORY_COLLECTION not in names:
        client.create_collection(
            collection_name=MEMORY_COLLECTION,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=MEMORY_VECTOR_SIZE,
                    distance=_resolve_distance(),
                ),
            },
            sparse_vectors_config={
                SPARSE_VECTOR_NAME: models.SparseVectorParams(
                    modifier=models.Modifier.IDF,
                ),
            },
        )
        _collection_ready = True
        return

    info = client.get_collection(collection_name=MEMORY_COLLECTION)
    vectors_config = info.config.params.vectors
    if not isinstance(vectors_config, dict) or DENSE_VECTOR_NAME not in vectors_config:
        raise RuntimeError(
            f"collection {MEMORY_COLLECTION} 不是具名向量或缺 {DENSE_VECTOR_NAME},"
            f"需删库重建为 dense+sparse 双向量"
        )
    if vectors_config[DENSE_VECTOR_NAME].size != MEMORY_VECTOR_SIZE:
        raise RuntimeError(
            f"collection {MEMORY_COLLECTION} dense 维度不符:"
            f"期望 {MEMORY_VECTOR_SIZE},实际 {vectors_config[DENSE_VECTOR_NAME].size}"
        )
    _collection_ready = True


def _build_payload(memory_id: str, content: str, *,
                   memory_type: str, scope: str, domain: str,
                   user_id: str, created_at: str, updated_at: str,
                   confidence: float = 1.0, verified: bool = False,
                   entry_url: str = "", intent_keywords: Optional[list[str]] = None,
                   keywords: Optional[list[str]] = None,
                   reinforce_count: int = 0, last_accessed_at: str = "",
                   valid: bool = True, invalid_at: str = "") -> dict[str, Any]:
    """组装 point payload(事实源)。

    confidence/verified/reinforce_count 是生命周期门控(H6:复用升权/负强化);
    entry_url/intent_keywords 仅 site_experience 消费;
    keywords 供 BM25 稀疏向量(H1)与检索;
    valid/invalid_at 是时间失效(矛盾时标记失效而非物理删除,可回溯,对齐 Zep)。
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
        "keywords": keywords or [],
        "reinforce_count": int(reinforce_count),
        "last_accessed_at": last_accessed_at or "",
        "valid": bool(valid),
        "invalid_at": invalid_at or "",
    }


def _build_vectors(dense: list[float], content: str, keywords: Optional[list[str]]) -> dict[str, Any]:
    """组装具名双向量:dense + sparse(sparse 源 = content + keywords)。"""
    sparse_src = content
    if keywords:
        sparse_src = content + " " + " ".join(str(k) for k in keywords)
    return {
        DENSE_VECTOR_NAME: dense,
        SPARSE_VECTOR_NAME: to_sparse_vector(sparse_src),
    }


def insert_memory(content: str, *, vector: list[float],
                  memory_type: str = MEMORY_TYPE_PREFERENCE,
                  scope: str = SCOPE_GLOBAL, domain: str = "",
                  user_id: str = DEFAULT_USER_ID,
                  confidence: float = 1.0, verified: bool = False,
                  entry_url: str = "", intent_keywords: Optional[list[str]] = None,
                  keywords: Optional[list[str]] = None,
                  reinforce_count: int = 0) -> dict[str, Any]:
    """写入一条新记忆(dense+sparse 双向量),返回落库的 payload(含生成的 memory_id)。"""
    ensure_collection()
    memory_id = make_memory_id()
    now = _now_iso()
    payload = _build_payload(
        memory_id, content, memory_type=memory_type, scope=scope,
        domain=domain, user_id=user_id, created_at=now, updated_at=now,
        confidence=confidence, verified=verified,
        entry_url=entry_url, intent_keywords=intent_keywords,
        keywords=keywords, reinforce_count=reinforce_count,
    )
    get_client().upsert(
        collection_name=MEMORY_COLLECTION,
        points=[models.PointStruct(
            id=_point_id(memory_id),
            vector=_build_vectors(vector, content, keywords),
            payload=payload,
        )],
        wait=True,
    )
    return payload


def update_memory(memory_id: str, content: str, *, vector: list[float]) -> Optional[dict[str, Any]]:
    """更新已存记忆的正文与双向量,保留其余字段、刷新 updated_at。

    memory_id 不存在返回 None(调用方据此避免误判)。
    """
    ensure_collection()
    existing = get_memory(memory_id)
    if existing is None:
        return None
    now = _now_iso()
    kw = existing.get("keywords", [])
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
        keywords=kw,
        reinforce_count=existing.get("reinforce_count", 0),
        last_accessed_at=existing.get("last_accessed_at", ""),
        valid=existing.get("valid", True),
        invalid_at=existing.get("invalid_at", ""),
    )
    get_client().upsert(
        collection_name=MEMORY_COLLECTION,
        points=[models.PointStruct(
            id=_point_id(memory_id),
            vector=_build_vectors(vector, content, kw),
            payload=payload,
        )],
        wait=True,
    )
    return payload


def reinforce_memory(memory_id: str, *, success: bool) -> Optional[dict[str, Any]]:
    """复用结算(H6):recall 命中的记忆在任务收尾时按成败升/降权。只改 payload,不动向量。

    - 成功:未验证记忆 +2(快速转正)、已验证 +1;置 verified=true;刷新 last_accessed_at。
    - 失败:reinforce_count-1(负强化);≤0 直接删(防失败经验固化)。
    返回更新后的 payload;记忆不存在返回 None;删除返回 {"deleted": True}。
    """
    ensure_collection()
    existing = get_memory(memory_id)
    if existing is None:
        return None
    count = int(existing.get("reinforce_count", 0))
    verified = bool(existing.get("verified", False))

    if success:
        count += 2 if not verified else 1
        patch = {"reinforce_count": count, "verified": True, "last_accessed_at": _now_iso()}
        get_client().set_payload(
            collection_name=MEMORY_COLLECTION, payload=patch,
            points=[_point_id(memory_id)], wait=True)
        return {**existing, **patch}

    count -= 1
    if count <= 0:
        delete_memory(memory_id)
        return {"deleted": True, "memory_id": memory_id}
    patch = {"reinforce_count": count, "last_accessed_at": _now_iso()}
    get_client().set_payload(
        collection_name=MEMORY_COLLECTION, payload=patch,
        points=[_point_id(memory_id)], wait=True)
    return {**existing, **patch}


def invalidate_memory(memory_id: str) -> Optional[dict[str, Any]]:
    """时间失效(对齐 Zep):矛盾时把旧记忆标记失效而非物理删除,只改 payload、不动向量。

    检索默认过滤掉 valid=False(见 _build_filter),但 include_invalid=True 可回溯。
    记忆不存在返回 None;成功返回更新后的 payload。
    """
    ensure_collection()
    existing = get_memory(memory_id)
    if existing is None:
        return None
    patch = {"valid": False, "invalid_at": _now_iso()}
    get_client().set_payload(
        collection_name=MEMORY_COLLECTION, payload=patch,
        points=[_point_id(memory_id)], wait=True)
    return {**existing, **patch}


def _prune_items(items: list[dict[str, Any]], cap: int, keep_ratio: float) -> int:
    """按 (reinforce_count 降序, last_accessed_at 降序) 保留高价值,删尾部。返回删除数。"""
    if len(items) <= cap:
        return 0
    keep_n = int(cap * keep_ratio)
    items.sort(key=lambda m: (int(m.get("reinforce_count", 0)), str(m.get("last_accessed_at", ""))),
               reverse=True)
    to_delete = items[keep_n:]
    for m in to_delete:
        mid = str(m.get("memory_id", ""))
        if mid:
            delete_memory(mid)
    return len(to_delete)


def prune_domain(domain: str, *, user_id: str = DEFAULT_USER_ID,
                 cap: int = 50, keep_ratio: float = 0.8) -> int:
    """遗忘剪枝(H6):某 domain 记忆(站点经验/教训)数超 cap → 删到 cap*keep_ratio。

    保留高价值:按 (reinforce_count 降序, last_accessed_at 降序) 排,删尾部低分/最久未用的。
    仅作用于该 domain 下的记忆(global 偏好 domain 为空,不在此列,另见 prune_global_preferences)。
    """
    ensure_collection()
    items = scroll_memories(user_id=user_id, scope=SCOPE_DOMAIN, domain=domain, limit=1000)
    return _prune_items(items, cap, keep_ratio)


def prune_global_preferences(*, user_id: str = DEFAULT_USER_ID,
                             cap: int = 50, keep_ratio: float = 0.8) -> int:
    """遗忘剪枝(H6):global 偏好(domain 为空)超 cap → 删到 cap*keep_ratio。

    偏好 domain 为空,prune_domain 覆盖不到,需单独剪枝防无限堆积。
    保留高 reinforce_count 的偏好。
    """
    ensure_collection()
    items = scroll_memories(user_id=user_id, memory_type=MEMORY_TYPE_PREFERENCE,
                            scope=SCOPE_GLOBAL, limit=1000)
    return _prune_items(items, cap, keep_ratio)


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
                  scope: Optional[str], domain: Optional[str],
                  include_invalid: bool = False) -> models.Filter:
    """组装 Qdrant payload 过滤条件。

    memory_type 可传 str(单类)或 list(多类,用 MatchAny)——
    recall 要同时查 site_experience+lesson 两类。
    include_invalid=False(默认)时追加 valid==True,过滤掉已失效记忆
    (时间失效:矛盾记忆标记失效而非删除,检索默认不返回,但可回溯)。
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
    if not include_invalid:
        must.append(models.FieldCondition(key="valid", match=models.MatchValue(value=True)))
    return models.Filter(must=must)


def search_memories(query_vector: list[float], *, top_k: int,
                    query_text: str = "",
                    user_id: str = DEFAULT_USER_ID,
                    memory_type: Optional[Any] = None,
                    scope: Optional[str] = None,
                    domain: Optional[str] = None,
                    include_invalid: bool = False) -> list[dict[str, Any]]:
    """hybrid 检索:dense(语义)+ sparse(BM25)双路 prefetch → RRF 融合。

    query_text 用于算 sparse 向量;为空或稀疏不可用时,sparse 路命中为空,
    RRF 自动退化为纯 dense。memory_type 可传 str 或 list(多类)。
    include_invalid=False 默认过滤已失效记忆。
    collection 不存在时返回空。返回 [{memory_id, content, score, ...payload}]，融合分降序。
    """
    ensure_collection()
    flt = _build_filter(user_id=user_id, memory_type=memory_type, scope=scope,
                        domain=domain, include_invalid=include_invalid)
    limit = max(1, int(top_k))
    prefetch = [
        models.Prefetch(query=query_vector, using=DENSE_VECTOR_NAME, filter=flt, limit=limit * 2),
    ]
    sparse_vec = to_sparse_vector(query_text) if query_text else None
    if sparse_vec is not None and sparse_vec.indices:
        prefetch.append(models.Prefetch(
            query=sparse_vec, using=SPARSE_VECTOR_NAME, filter=flt, limit=limit * 2))
    try:
        result = get_client().query_points(
            collection_name=MEMORY_COLLECTION,
            prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=limit,
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


def dense_scores(query_vector: list[float], *, top_k: int,
                 user_id: str = DEFAULT_USER_ID,
                 memory_type: Optional[Any] = None,
                 scope: Optional[str] = None,
                 domain: Optional[str] = None,
                 include_invalid: bool = False) -> dict[str, float]:
    """纯 dense 检索,返回 {memory_id: cosine}(Cosine 距离下 query_points 的 score 即余弦)。

    供相关性闸门用:hybrid RRF 分数量级极小、绝对阈值不可用,故单独取 dense 余弦判绝对相关性。
    collection 不存在返回空 dict。
    """
    ensure_collection()
    flt = _build_filter(user_id=user_id, memory_type=memory_type, scope=scope,
                        domain=domain, include_invalid=include_invalid)
    try:
        result = get_client().query_points(
            collection_name=MEMORY_COLLECTION,
            query=query_vector, using=DENSE_VECTOR_NAME,
            query_filter=flt, limit=max(1, int(top_k)), with_payload=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return {}
        raise
    hits = result.points if hasattr(result, "points") else []
    out: dict[str, float] = {}
    for hit in hits:
        mid = str((hit.payload or {}).get("memory_id", ""))
        if mid:
            out[mid] = float(hit.score)
    return out


def scroll_memories(*, user_id: str = DEFAULT_USER_ID,
                    memory_type: Optional[Any] = None,
                    scope: Optional[str] = None,
                    domain: Optional[str] = None,
                    limit: int = 200,
                    include_invalid: bool = False) -> list[dict[str, Any]]:
    """filter-only 拉取记忆(不做相似度检索),供常驻偏好全量取回 / 遗忘剪枝 / CRUD 列表用。

    与向量无关,按 payload 过滤条件 scroll。include_invalid=True 时含已失效记忆
    (CRUD 回溯用)。collection 不存在返回空。
    """
    ensure_collection()
    try:
        points, _ = get_client().scroll(
            collection_name=MEMORY_COLLECTION,
            scroll_filter=_build_filter(
                user_id=user_id, memory_type=memory_type, scope=scope,
                domain=domain, include_invalid=include_invalid),
            limit=max(1, int(limit)),
            with_payload=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            return []
        raise
    return [dict(p.payload or {}) for p in points]


def count_memories(user_id: str = DEFAULT_USER_ID, *,
                   memory_type: Optional[Any] = None,
                   domain: Optional[str] = None,
                   include_invalid: bool = False) -> int:
    """统计记忆条数(测试/一致性校验 + 启发式兜底用)。

    可按 memory_type、domain 过滤——启发式兜底要数"某站点有几条 site_experience"。
    include_invalid=False 默认只数有效记忆。
    """
    ensure_collection()
    result = get_client().count(
        collection_name=MEMORY_COLLECTION,
        count_filter=_build_filter(
            user_id=user_id, memory_type=memory_type, scope=None,
            domain=domain, include_invalid=include_invalid),
        exact=True,
    )
    return int(result.count)
