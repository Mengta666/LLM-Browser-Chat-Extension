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
    MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC,
    SCOPE_GLOBAL, DEFAULT_USER_ID, CHAT_USER_ID, CHAT_CORE_TYPES,
    EPISODIC_CAP, EPISODIC_KEEP_RATIO, EPISODIC_PRUNE_GRACE_HOURS,
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


def _ensure_payload_indexes() -> None:
    """为高频过滤字段建 payload 索引(幂等):user_id/memory_type/chat_id/valid。

    _build_filter 的每个 must 条件都需要索引,否则 Qdrant 走线性全表扫描
    (无索引时几十个点的 scroll 就要十几秒)。keyword 型精确匹配,bool 用 bool 型。
    重复建同名同型索引是 no-op;异常静默(索引缺失只影响性能不影响正确性)。
    """
    client = get_client()
    keyword_fields = ["user_id", "memory_type", "chat_id", "scope", "subject"]
    for field in keyword_fields:
        try:
            client.create_payload_index(
                collection_name=MEMORY_COLLECTION, field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD, wait=True)
        except Exception:
            pass
    try:
        client.create_payload_index(
            collection_name=MEMORY_COLLECTION, field_name="valid",
            field_schema=models.PayloadSchemaType.BOOL, wait=True)
    except Exception:
        pass


def ensure_collection() -> None:
    """确保记忆 collection 存在:dense(4096/Cosine 具名)+ sparse(IDF)双向量。

    hybrid 检索需具名双向量。维度硬校验 dense==4096(拒绝维度炸弹)。
    并建 payload 索引(过滤字段,防线性扫描)。
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
        _ensure_payload_indexes()
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
    _ensure_payload_indexes()  # 已存在的老 collection 也补建(幂等)
    _collection_ready = True


def _build_payload(memory_id: str, content: str, *,
                   memory_type: str, scope: str, domain: str,
                   user_id: str, created_at: str, updated_at: str,
                   confidence: float = 1.0, verified: bool = False,
                   entry_url: str = "", intent_keywords: Optional[list[str]] = None,
                   keywords: Optional[list[str]] = None,
                   reinforce_count: int = 0, last_accessed_at: str = "",
                   valid: bool = True, invalid_at: str = "",
                   chat_id: str = "",
                   promoted_from: str = "",
                   stability_score: float = 0.5,
                   subject: str = "",
                   expires_at: str = "",
                   superseded_by: str = "") -> dict[str, Any]:
    """组装 point payload(事实源)。

    confidence/verified/reinforce_count 是生命周期门控;
    keywords 供 BM25 稀疏向量与检索;
    valid/invalid_at 是时间失效(矛盾时标记失效而非物理删除,可回溯,对齐 Zep)。
    chat_id 是第二级隔离键:episodic 带所属会话 id(仅本会话检索),
    core 留空表全局(跨所有会话)。
    confidence 兼作 importance 存储位(1-10 归一到 0.1-1.0,供检索三因子重排与 GC 排序)。
    promoted_from(批次 B4):若非空,表示这条 core 是由 episodic 晋升上来的,
    值为原 chat_id——供 demote_memory 回退,以及审计层留痕。
    stability_score(批次 D):LLM 抽取时打分——这条 fact 有多稳定(0=一次性事件,1=长期不变属性)。
    与 memory_type 独立,供 consolidate LLM 判 promote 时使用。缺省 0.5 中性。
    subject(批次 E · P1):LLM 抽取时的主题短语(自由文本,如"回答语言偏好""编程语言")。
    供 CONSOLIDATE 候选拉取加副通道——按 subject 硬匹配,补 embedding 相似度低但同主题的漏检。
    抽不到明确主题给空串,不进 subject 副通道(退化到 embedding 单通道)。
    expires_at(批次 E · P2):预计失效时刻(未来 ISO 时间);EXTRACT 明确抽到时限
    信号词("这周""下周""临时"等)才 set,身份/长期偏好留空。
    rethink 整理时若 expires_at < now 归为 expired 组。**与 invalid_at 语义分工**:
    invalid_at 是"已失效时刻(过去)",expires_at 是"预计失效时刻(未来)"。
    superseded_by(批次 E · P2):若非空,表示这条被某条更新的取代,值为新 memory_id。
    不软失效、只标注——留给应答 LLM 参考、或用户手动清理。rethink 判 conflict/merge
    时,failed(被替代)条会同时被 invalidate + set superseded_by。
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
        "chat_id": chat_id or "",
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
        "promoted_from": promoted_from or "",
        "stability_score": float(stability_score),
        "subject": str(subject or "")[:64],
        "expires_at": expires_at or "",
        "superseded_by": superseded_by or "",
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
                  memory_type: str = MEMORY_TYPE_CORE,
                  scope: str = SCOPE_GLOBAL, domain: str = "",
                  user_id: str = DEFAULT_USER_ID,
                  confidence: float = 1.0, verified: bool = False,
                  entry_url: str = "", intent_keywords: Optional[list[str]] = None,
                  keywords: Optional[list[str]] = None,
                  reinforce_count: int = 0, chat_id: str = "",
                  promoted_from: str = "",
                  stability_score: float = 0.5,
                  subject: str = "",
                  expires_at: str = "") -> dict[str, Any]:
    """写入一条新记忆(dense+sparse 双向量),返回落库的 payload(含生成的 memory_id)。"""
    ensure_collection()
    memory_id = make_memory_id()
    now = _now_iso()
    payload = _build_payload(
        memory_id, content, memory_type=memory_type, scope=scope,
        domain=domain, user_id=user_id, created_at=now, updated_at=now,
        confidence=confidence, verified=verified,
        entry_url=entry_url, intent_keywords=intent_keywords,
        keywords=keywords, reinforce_count=reinforce_count, chat_id=chat_id,
        promoted_from=promoted_from, stability_score=stability_score,
        subject=subject, expires_at=expires_at,
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
    try:
        from agent.memory import list_cache
        list_cache.invalidate_all()
    except Exception:
        pass
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
        memory_type=existing.get("memory_type", MEMORY_TYPE_CORE),
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
        chat_id=existing.get("chat_id", ""),
        promoted_from=existing.get("promoted_from", ""),
        stability_score=existing.get("stability_score", 0.5),
        subject=existing.get("subject", ""),
        expires_at=existing.get("expires_at", ""),
        superseded_by=existing.get("superseded_by", ""),
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
    try:
        from agent.memory import list_cache
        list_cache.invalidate_all()
    except Exception:
        pass
    return payload


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
    try:
        from agent.memory import list_cache
        list_cache.invalidate_all()
    except Exception:
        pass
    return {**existing, **patch}


def touch_memories(memory_ids: list[str]) -> None:
    """召回命中后回写访问信号:reinforce_count+1、last_accessed_at=now(激活死字段)。

    只改 payload、不动 4096 向量(轻),供检索 recency×frequency 与 GC 排序用。
    best-effort:任何异常静默吞掉(访问回写失败不该拖垮 chat 读路径)。
    逐条 get→set(条数少,每轮 ≤top_k),避免读改写竞态用 set_payload 增量。
    """
    if not memory_ids:
        return
    now = _now_iso()
    client = get_client()
    for mid in memory_ids:
        try:
            existing = get_memory(str(mid))
            if existing is None:
                continue
            patch = {
                "reinforce_count": int(existing.get("reinforce_count", 0)) + 1,
                "last_accessed_at": now,
            }
            client.set_payload(
                collection_name=MEMORY_COLLECTION, payload=patch,
                points=[_point_id(str(mid))], wait=False)
        except Exception:
            continue


def prune_global_preferences(*, user_id: str = CHAT_USER_ID) -> int:
    """core 遗忘剪枝(P0 后):只物理清除已软失效超宽限窗的僵尸条,活跃 core 永不物理删。

    对齐 MemGPT:core memory 永不按条数 evict。活跃身份/偏好的控量交给写路径的
    UPDATE/DELETE 决策(矛盾软失效)+ 注入侧 CORE_CHAR_BUDGET 字符预算(importance 优先填充);
    存量若最终超预算,靠后续的 core 摘要/合并压缩,而非在这里近随机物理删。
    这里只回收超宽限窗的 valid=false 僵尸——**留出宽限窗是为了 include_invalid=True 的回溯**
    功能有意义(记忆面板"恢复被删记忆")。宽限期内软失效条能被 CRUD 层看到并恢复,
    过期后才真正回收存储。
    chat_id="" 只扫全局 core,别误动会话 episodic。返回物理删除数。
    """
    ensure_collection()
    items = scroll_memories(user_id=user_id, memory_type=CHAT_CORE_TYPES,
                            scope=SCOPE_GLOBAL, chat_id="", limit=1000,
                            include_invalid=True)
    now_ts = datetime.now(timezone.utc).timestamp()
    grace_cutoff = now_ts - EPISODIC_PRUNE_GRACE_HOURS * 3600
    removed = 0
    for m in items:
        if m.get("valid", True):
            continue
        # 宽限窗内的 invalid 保留(用户可回溯);超窗才物删
        inv_at = str(m.get("invalid_at", ""))
        if inv_at:
            try:
                if datetime.fromisoformat(inv_at).timestamp() >= grace_cutoff:
                    continue
            except ValueError:
                pass  # 时间戳解析失败,当作过期处理(容错删除)
        mid = str(m.get("memory_id", ""))
        if mid:
            delete_memory(mid)
            removed += 1
    return removed


def prune_episodic(chat_id: str, *, user_id: str = CHAT_USER_ID,
                   cap: int = EPISODIC_CAP,
                   keep_ratio: float = EPISODIC_KEEP_RATIO) -> int:
    """会话级 episodic 遗忘剪枝:某会话的 episodic 超 cap → 软失效尾部到 cap*keep_ratio。

    与 core 剪枝(只清僵尸)不同,episodic 是容量 GC:①按 (verified 保留, importance/confidence 降序,
    reinforce_count 降序, last_accessed_at 降序) 排序,把 importance 纳入淘汰键;②用 invalidate_memory
    软失效(可回溯)而非物理删;③跳过 verified=True 与 created_at 在宽限窗内的新记忆(防误删)。
    只扫单个会话(几十条),在写入后同步调用,非后台全量扫描。返回失效条数。
    """
    if not chat_id:
        return 0
    ensure_collection()
    items = scroll_memories(user_id=user_id, memory_type=MEMORY_TYPE_EPISODIC,
                            scope=SCOPE_GLOBAL, chat_id=chat_id, limit=1000)
    if len(items) <= cap:
        return 0

    now = datetime.now(timezone.utc)
    grace_cutoff = now.timestamp() - EPISODIC_PRUNE_GRACE_HOURS * 3600

    def _in_grace(m: dict[str, Any]) -> bool:
        raw = str(m.get("created_at", ""))
        if not raw:
            return False
        try:
            ts = datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return False
        return ts >= grace_cutoff

    # 高价值在前:verified > 高 importance > 高 reinforce > 近期访问
    items.sort(key=lambda m: (
        1 if m.get("verified") else 0,
        float(m.get("confidence", 1.0)),
        int(m.get("reinforce_count", 0)),
        str(m.get("last_accessed_at", "")),
    ), reverse=True)

    keep_n = int(cap * keep_ratio)
    invalidated = 0
    for m in items[keep_n:]:
        # 保护:verified 或 宽限窗内新记忆 不失效
        if m.get("verified") or _in_grace(m):
            continue
        mid = str(m.get("memory_id", ""))
        if mid:
            invalidate_memory(mid)
            invalidated += 1
    return invalidated


def delete_memory(memory_id: str) -> None:
    """删除一条记忆(幂等,不存在也不报错)。"""
    ensure_collection()
    get_client().delete(
        collection_name=MEMORY_COLLECTION,
        points_selector=models.PointIdsList(points=[_point_id(memory_id)]),
        wait=True,
    )
    try:
        from agent.memory import list_cache
        list_cache.invalidate_all()
    except Exception:
        pass


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
                  include_invalid: bool = False,
                  chat_id: Optional[str] = None,
                  subject: Optional[str] = None) -> models.Filter:
    """组装 Qdrant payload 过滤条件。

    memory_type 可传 str(单类)或 list(多类,用 MatchAny)——
    memory_type 可传 str(单类)或 list(多类,用 MatchAny)——合并后 core 检索传单元素列表亦可。
    include_invalid=False(默认)时追加 valid==True,过滤掉已失效记忆
    (时间失效:矛盾记忆标记失效而非删除,检索默认不返回,但可回溯)。
    chat_id 是第二级隔离:None(默认)不加条件(向后兼容全局行为);
    ""(空串)显式匹配全局记忆(core);"X" 仅匹配该会话(episodic)。
    subject(批次 E · P1):None 不过滤;非 None 精确匹配 payload.subject
    (含空串,用于 subject 副通道硬匹配;调用方保证只对非空 subject 用它)。
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
    if chat_id is not None:
        must.append(models.FieldCondition(key="chat_id", match=models.MatchValue(value=chat_id)))
    if subject is not None:
        must.append(models.FieldCondition(key="subject", match=models.MatchValue(value=subject)))
    if not include_invalid:
        must.append(models.FieldCondition(key="valid", match=models.MatchValue(value=True)))
    return models.Filter(must=must)


def search_memories(query_vector: list[float], *, top_k: int,
                    query_text: str = "",
                    user_id: str = DEFAULT_USER_ID,
                    memory_type: Optional[Any] = None,
                    scope: Optional[str] = None,
                    domain: Optional[str] = None,
                    include_invalid: bool = False,
                    chat_id: Optional[str] = None,
                    subject: Optional[str] = None) -> list[dict[str, Any]]:
    """hybrid 检索:dense(语义)+ sparse(BM25)双路 prefetch → RRF 融合。

    query_text 用于算 sparse 向量;为空或稀疏不可用时,sparse 路命中为空,
    RRF 自动退化为纯 dense。memory_type 可传 str 或 list(多类)。
    include_invalid=False 默认过滤已失效记忆。chat_id 见 _build_filter(会话隔离)。
    subject 见 _build_filter(批次 E P1 subject 副通道)。
    collection 不存在时返回空。返回 [{memory_id, content, score, ...payload}]，融合分降序。
    """
    ensure_collection()
    flt = _build_filter(user_id=user_id, memory_type=memory_type, scope=scope,
                        domain=domain, include_invalid=include_invalid,
                        chat_id=chat_id, subject=subject)
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
                 include_invalid: bool = False,
                 chat_id: Optional[str] = None,
                 subject: Optional[str] = None) -> dict[str, float]:
    """纯 dense 检索,返回 {memory_id: cosine}(Cosine 距离下 query_points 的 score 即余弦)。

    供相关性闸门用:hybrid RRF 分数量级极小、绝对阈值不可用,故单独取 dense 余弦判绝对相关性。
    chat_id 见 _build_filter(会话隔离)。subject 见 _build_filter(批次 E P1)。
    collection 不存在返回空 dict。
    """
    ensure_collection()
    flt = _build_filter(user_id=user_id, memory_type=memory_type, scope=scope,
                        domain=domain, include_invalid=include_invalid,
                        chat_id=chat_id, subject=subject)
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
                    include_invalid: bool = False,
                    chat_id: Optional[str] = None,
                    subject: Optional[str] = None) -> list[dict[str, Any]]:
    """filter-only 拉取记忆(不做相似度检索),供常驻偏好全量取回 / 遗忘剪枝 / CRUD 列表用。

    与向量无关,按 payload 过滤条件 scroll。include_invalid=True 时含已失效记忆
    (CRUD 回溯用)。chat_id 见 _build_filter(会话隔离)。subject 见 _build_filter
    (批次 E P1 · subject 副通道硬匹配用)。collection 不存在返回空。
    """
    ensure_collection()
    try:
        points, _ = get_client().scroll(
            collection_name=MEMORY_COLLECTION,
            scroll_filter=_build_filter(
                user_id=user_id, memory_type=memory_type, scope=scope,
                domain=domain, include_invalid=include_invalid,
                chat_id=chat_id, subject=subject),
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
                   include_invalid: bool = False,
                   chat_id: Optional[str] = None,
                   subject: Optional[str] = None) -> int:
    """统计记忆条数(测试/一致性校验用)。

    可按 memory_type、domain、chat_id、subject 过滤。include_invalid=False 默认只数有效记忆。
    """
    ensure_collection()
    result = get_client().count(
        collection_name=MEMORY_COLLECTION,
        count_filter=_build_filter(
            user_id=user_id, memory_type=memory_type, scope=None,
            domain=domain, include_invalid=include_invalid,
            chat_id=chat_id, subject=subject),
        exact=True,
    )
    return int(result.count)
