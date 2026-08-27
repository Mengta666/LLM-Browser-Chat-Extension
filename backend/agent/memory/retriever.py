"""记忆检索(注入 chat 前的读路径)。

分层召回(对齐 MemGPT core-常驻 / archival-按需):
- retrieve_core_memories:persona + preference,**常驻**注入每轮对话(scroll 全量,不过阈值)。
- recall_episodic_memories:episodic 事件记忆,**按需**召回,过双相关性闸门。

均不调 LLM(纯向量)。Qdrant 不可用/空库时返回空,不抛异常,保证 chat 主流程不受影响。
"""

from datetime import datetime, timezone
from typing import Any

from agent.memory import vector as V
from agent.memory.config import (
    MEMORY_RECALL_TOP_K, RESIDENT_PREFERENCE_CHAR_LIMIT,
    MEMORY_TYPE_PERSONA, MEMORY_TYPE_EPISODIC,
    SCOPE_GLOBAL, CHAT_USER_ID, INSTRUCT_CHAT,
    CHAT_CORE_TYPES, CHAT_CORE_TOP_K, RECALL_MIN_COSINE, RECALL_REL_RATIO,
    RECALL_W_RECENCY, RECALL_W_IMPORTANCE, RECALL_GAP_REF, RECALL_HALFLIFE_HOURS,
)
from rag.embedder import embed_query


# ═══════════════════════════════════════════════════════════════════════════════
# chat 分层记忆读路径(core 常驻 + episodic 按需,对齐 MemGPT)
# ═══════════════════════════════════════════════════════════════════════════════

def retrieve_core_memories(user_id: str = CHAT_USER_ID,
                           top_k: int = CHAT_CORE_TOP_K) -> list[dict[str, Any]]:
    """取 chat 常驻记忆(persona + preference,scope=global),每轮无条件注入。

    scroll 全量取回(与相似度无关——core 是"永远该记着"的),排序:
    persona 优先(用户是谁比偏好更根本)→ reinforce_count 降序 → created_at 升序(稳定)。
    Qdrant 不可用/空库返回空。
    """
    try:
        items = V.scroll_memories(
            user_id=user_id, memory_type=CHAT_CORE_TYPES, scope=SCOPE_GLOBAL, limit=200)
    except Exception:
        return []

    def _sort_key(m: dict[str, Any]):
        persona_first = 0 if m.get("memory_type") == MEMORY_TYPE_PERSONA else 1
        return (persona_first, -int(m.get("reinforce_count", 0)), str(m.get("created_at", "")))

    items.sort(key=_sort_key)
    return items[:top_k]


def build_core_block(memories: list[dict[str, Any]]) -> str:
    """把 core 记忆组装成注入每轮对话的文本块。空则返回空串。

    persona / preference 分组呈现;累计超 RESIDENT_PREFERENCE_CHAR_LIMIT 即停(容量护栏)。
    """
    if not memories:
        return ""
    personas = [m for m in memories if m.get("memory_type") == MEMORY_TYPE_PERSONA]
    prefs = [m for m in memories if m.get("memory_type") != MEMORY_TYPE_PERSONA]

    lines = ["## 关于用户(始终参考)"]
    used = 0
    if personas:
        lines.append("身份:")
        for m in personas:
            content = str(m.get("content", "")).strip()
            if not content:
                continue
            if used + len(content) > RESIDENT_PREFERENCE_CHAR_LIMIT:
                break
            lines.append(f"- {content}")
            used += len(content)
    if prefs:
        lines.append("偏好:")
        for m in prefs:
            content = str(m.get("content", "")).strip()
            if not content:
                continue
            if used + len(content) > RESIDENT_PREFERENCE_CHAR_LIMIT:
                break
            lines.append(f"- {content}")
            used += len(content)
    return "\n".join(lines) if len(lines) > 1 else ""


def _recency_score(created_at: str, *, now_ts: float) -> float:
    """指数衰减:0.5**(age_hours/half_life)。用 created_at(事件新鲜度)。解析失败返回 0.5。"""
    raw = str(created_at or "")
    if not raw:
        return 0.5
    try:
        ts = datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return 0.5
    age_hours = max(0.0, (now_ts - ts) / 3600.0)
    return 0.5 ** (age_hours / max(1e-6, RECALL_HALFLIFE_HOURS))


def recall_episodic_memories(query: str, *,
                             top_k: int = MEMORY_RECALL_TOP_K,
                             user_id: str = CHAT_USER_ID,
                             chat_id: str = "") -> list[dict[str, Any]]:
    """按需召回 chat 事件记忆(episodic),仅本会话(chat_id 隔离),过双闸门 + 三因子重排。

    双闸门(防 Lost-in-the-Middle 噪声):
    ① 绝对余弦门(主):额外发一次纯 dense 检索取余弦,过滤 cosine < RECALL_MIN_COSINE。
    ② 相对比门(兜底):过滤 rrf_score < top1_rrf * RECALL_REL_RATIO。
    过闸门后按三因子(relevance 余弦 + recency 时间衰减 + importance/confidence)gap-gated
    稀释重排(对齐 Generative Agents;相关度扎堆时才放大次要因子),取 top_k。命中项 touch 回写访问信号。
    全被挡则返回空(宁可不注入,不注入噪声)。Qdrant 不可用/空库/query 空返回空。
    """
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []
    try:
        query_vector = embed_query(normalized_query, INSTRUCT_CHAT)
        candidates = V.search_memories(
            query_vector, query_text=normalized_query, top_k=top_k * 2, user_id=user_id,
            memory_type=MEMORY_TYPE_EPISODIC, scope=SCOPE_GLOBAL, chat_id=chat_id,
        )
        # 闸门①数据:纯 dense 余弦(hybrid RRF 分丢了绝对相关性,单独取)
        cos_map = V.dense_scores(
            query_vector, top_k=top_k * 2, user_id=user_id,
            memory_type=MEMORY_TYPE_EPISODIC, scope=SCOPE_GLOBAL, chat_id=chat_id)
    except Exception:
        return []

    if not candidates:
        return []
    # 闸门②基准:最高 RRF 分
    top_rrf = max((float(c.get("score", 0.0)) for c in candidates), default=0.0)
    rrf_floor = top_rrf * RECALL_REL_RATIO

    passed: list[dict[str, Any]] = []
    for c in candidates:
        mid = str(c.get("memory_id", ""))
        cosine = cos_map.get(mid, 0.0)
        rrf = float(c.get("score", 0.0))
        # 双门取交集:余弦够高 且 RRF 不显著低于榜首
        if cosine >= RECALL_MIN_COSINE and rrf >= rrf_floor:
            c = dict(c)
            c["cosine"] = cosine
            passed.append(c)

    if not passed:
        return []

    # 三因子重排(gap-gated 稀释):relevance 用原始 cosine,recency/importance 受稀释门控。
    # spread=候选 cosine 极差;dilution=1-min(1,spread/GAP_REF)。
    # 相关度拉得开→dilution→0→relevance 主导;扎堆→dilution→1→放手让次要因子打破 tie。
    now_ts = datetime.now(timezone.utc).timestamp()
    rel_raw = [float(m.get("cosine", 0.0)) for m in passed]
    spread = (max(rel_raw) - min(rel_raw)) if rel_raw else 0.0
    dilution = 1.0 - min(1.0, spread / max(1e-9, RECALL_GAP_REF))
    for i, m in enumerate(passed):
        rec = _recency_score(m.get("created_at", ""), now_ts=now_ts)
        imp = float(m.get("confidence", 1.0))
        m["_recency"] = rec
        m["_dilution"] = dilution
        m["_score3"] = rel_raw[i] + dilution * (RECALL_W_RECENCY * rec + RECALL_W_IMPORTANCE * imp)

    passed.sort(key=lambda m: m.get("_score3", 0.0), reverse=True)
    result = passed[:top_k]

    # 命中回写访问信号(best-effort,激活 reinforce_count/last_accessed_at 死字段)
    try:
        V.touch_memories([str(m.get("memory_id", "")) for m in result if m.get("memory_id")])
    except Exception:
        pass
    return result


def build_episodic_block(memories: list[dict[str, Any]]) -> str:
    """把 episodic 召回结果组装成注入文本块。空则返回空串(不注入)。"""
    if not memories:
        return ""
    lines = ["## 相关历史(无关可忽略)"]
    for item in memories:
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"- {content}")
    return "\n".join(lines) if len(lines) > 1 else ""
