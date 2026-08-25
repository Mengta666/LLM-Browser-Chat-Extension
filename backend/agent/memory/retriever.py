"""记忆检索(注入 agent 前的读路径)。

分层召回(对齐 MemGPT core-常驻 / archival-按需):
- retrieve_resident_preferences:强用户偏好,**常驻**注入每步 prompt。不过相似度阈值
  (偏好要全带,判断依据是"漏掉代价高"而非相关性),量小、按 top-K 截断。
- recall_site_memories:站点经验 + 失败教训,**按需**由 recall 工具触发。过相似度阈值,
  lesson 命中降权并标"待验证"。

均不调 LLM(纯向量)。Qdrant 不可用/空库时返回空,不抛异常,保证 agent 主流程不受影响。
"""

from typing import Any
from urllib.parse import urlsplit

from agent.memory import vector as V
from agent.memory.config import (
    MEMORY_RECALL_TOP_K,
    RESIDENT_PREFERENCE_TOP_K, RESIDENT_PREFERENCE_CHAR_LIMIT, LESSON_RECALL_WEIGHT,
    MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_SITE_EXPERIENCE, MEMORY_TYPE_LESSON,
    MEMORY_TYPE_PERSONA, MEMORY_TYPE_EPISODIC,
    SCOPE_GLOBAL, SCOPE_DOMAIN, DEFAULT_USER_ID, CHAT_USER_ID, INSTRUCT_SITE, INSTRUCT_CHAT,
    CHAT_CORE_TYPES, CHAT_CORE_TOP_K, RECALL_MIN_COSINE, RECALL_REL_RATIO,
)
from rag.embedder import embed_query


def extract_domain(url: str) -> str:
    """从 URL 提取小写域名,失败返回空字符串。"""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def retrieve_resident_preferences(user_id: str = DEFAULT_USER_ID,
                                  top_k: int = RESIDENT_PREFERENCE_TOP_K) -> list[dict[str, Any]]:
    """取常驻用户偏好(preference, scope=global),供每步 prompt 无条件注入。

    用 scroll(filter-only,与相似度无关)全量取回——偏好要全程约束,不是按当前任务相关性检索。
    排序:reinforce_count 降序(高频/高置信优先常驻)+ created_at 升序(稳定),截断 top_k。
    Qdrant 不可用/空库返回空。
    """
    try:
        prefs = V.scroll_memories(
            user_id=user_id, memory_type=MEMORY_TYPE_PREFERENCE, scope=SCOPE_GLOBAL, limit=200)
    except Exception:
        return []

    # 高 reinforce 优先(高频/高置信偏好先常驻),同分按 created_at 稳定
    prefs.sort(key=lambda m: (-int(m.get("reinforce_count", 0)), str(m.get("created_at", ""))))
    return prefs[:top_k]


def recall_site_memories(query: str, *, domain: str = "",
                         top_k: int = MEMORY_RECALL_TOP_K,
                         user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """按需召回站点经验 + 失败教训(site_experience + lesson),供 recall 工具调用。

    hybrid(dense+BM25)召回,RRF 融合。截断:主用 top_k 硬截断(RRF 分数量级极小,
    绝对阈值不适用);lesson 命中 rank_score×LESSON_RECALL_WEIGHT 降权后与
    site_experience 一同重排。Qdrant 不可用/空库返回空。
    """
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []
    try:
        query_vector = embed_query(normalized_query, INSTRUCT_SITE)
        candidates = V.search_memories(
            query_vector, query_text=normalized_query, top_k=top_k * 2, user_id=user_id,
            memory_type=[MEMORY_TYPE_SITE_EXPERIENCE, MEMORY_TYPE_LESSON],
            scope=SCOPE_DOMAIN, domain=(domain or None),
        )
    except Exception:
        return []

    ranked: list[dict[str, Any]] = []
    for item in candidates:
        raw_score = float(item.get("score", 0.0))
        item = dict(item)
        # lesson 降权(低权+待验证),用于排序,不改原始 score 语义
        if item.get("memory_type") == MEMORY_TYPE_LESSON:
            item["rank_score"] = raw_score * LESSON_RECALL_WEIGHT
        else:
            item["rank_score"] = raw_score
        ranked.append(item)

    ranked.sort(key=lambda m: m.get("rank_score", 0.0), reverse=True)
    return ranked[:top_k]


def build_preference_block(preferences: list[dict[str, Any]]) -> str:
    """把常驻偏好组装成注入每步 prompt 的文本块。空则返回空串。

    容量护栏(仿 Letta core block 字符上限):累计超 RESIDENT_PREFERENCE_CHAR_LIMIT 即停,
    偏好已按 reinforce_count 降序,高频/高置信优先常驻,低频自然被挡在外(下沉召回层)。
    """
    if not preferences:
        return ""
    lines = ["## 用户偏好(始终遵守)"]
    used = 0
    for item in preferences:
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        if used + len(content) > RESIDENT_PREFERENCE_CHAR_LIMIT:
            break
        lines.append(f"- {content}")
        used += len(content)
    return "\n".join(lines) if len(lines) > 1 else ""


def build_recall_block(memories: list[dict[str, Any]]) -> str:
    """把 recall 到的站点经验/教训组装成注入历史的文本块。空则返回提示。"""
    if not memories:
        return "(未回忆到该站点的相关经验)"
    lines = ["## 回忆到的站点经验(供参考,与当前任务无关则忽略)"]
    for item in memories:
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        if item.get("memory_type") == MEMORY_TYPE_LESSON:
            lines.append(f"- ⚠待验证教训:{content}")
        else:
            lines.append(f"- {content}")
    return "\n".join(lines) if len(lines) > 1 else "(未回忆到该站点的相关经验)"


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


def recall_episodic_memories(query: str, *,
                             top_k: int = MEMORY_RECALL_TOP_K,
                             user_id: str = CHAT_USER_ID) -> list[dict[str, Any]]:
    """按需召回 chat 事件记忆(episodic),过双相关性闸门(防 Lost-in-the-Middle 噪声)。

    双闸门:
    ① 绝对余弦门(主):额外发一次纯 dense 检索取余弦,过滤 cosine < RECALL_MIN_COSINE。
    ② 相对比门(兜底):过滤 rrf_score < top1_rrf * RECALL_REL_RATIO。
    两门取交集后 top_k。全被挡则返回空(宁可不注入,不注入噪声)。
    Qdrant 不可用/空库/query 空返回空。
    """
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []
    try:
        query_vector = embed_query(normalized_query, INSTRUCT_CHAT)
        candidates = V.search_memories(
            query_vector, query_text=normalized_query, top_k=top_k * 2, user_id=user_id,
            memory_type=MEMORY_TYPE_EPISODIC, scope=SCOPE_GLOBAL,
        )
        # 闸门①数据:纯 dense 余弦(hybrid RRF 分丢了绝对相关性,单独取)
        cos_map = V.dense_scores(
            query_vector, top_k=top_k * 2, user_id=user_id,
            memory_type=MEMORY_TYPE_EPISODIC, scope=SCOPE_GLOBAL)
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

    passed.sort(key=lambda m: m.get("score", 0.0), reverse=True)
    return passed[:top_k]


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
