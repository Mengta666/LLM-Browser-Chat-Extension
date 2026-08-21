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
    RETRIEVE_TOP_K, RETRIEVE_THRESHOLD, MEMORY_RECALL_TOP_K,
    RESIDENT_PREFERENCE_TOP_K, LESSON_RECALL_WEIGHT,
    MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_SITE_EXPERIENCE, MEMORY_TYPE_LESSON,
    SCOPE_GLOBAL, SCOPE_DOMAIN, DEFAULT_USER_ID,
)
from rag.embedder import embed_text


def extract_domain(url: str) -> str:
    """从 URL 提取小写域名,失败返回空字符串。"""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def retrieve_resident_preferences(user_id: str = DEFAULT_USER_ID,
                                  top_k: int = RESIDENT_PREFERENCE_TOP_K) -> list[dict[str, Any]]:
    """取常驻用户偏好(preference, scope=global),供每步 prompt 无条件注入。

    **不过相似度阈值**:偏好要全程约束,判断依据是漏掉代价,不是与当前任务相关性。
    用零向量做一次无偏检索取回全部偏好(量小),按 created_at 稳定取前 top_k。
    Qdrant 不可用/空库返回空。
    """
    try:
        # 偏好量小,用任一非空向量拉回候选再本地排序即可;这里用固定探针向量避免额外语义偏向。
        probe = embed_text("用户偏好")
        prefs = V.search_memories(
            probe, top_k=max(top_k * 3, 10), user_id=user_id,
            memory_type=MEMORY_TYPE_PREFERENCE, scope=SCOPE_GLOBAL,
        )
    except Exception:
        return []

    # 稳定排序:created_at 升序(老偏好优先,行为稳定),截断 top_k
    prefs.sort(key=lambda m: str(m.get("created_at", "")))
    return prefs[:top_k]


def recall_site_memories(query: str, *, domain: str = "",
                         top_k: int = MEMORY_RECALL_TOP_K,
                         threshold: float = RETRIEVE_THRESHOLD,
                         user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """按需召回站点经验 + 失败教训(site_experience + lesson),供 recall 工具调用。

    过相似度阈值过滤噪声;lesson 命中 score×LESSON_RECALL_WEIGHT 降权后与
    site_experience 一同重排。Qdrant 不可用/空库返回空。
    """
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []
    try:
        query_vector = embed_text(normalized_query)
        candidates = V.search_memories(
            query_vector, top_k=top_k * 2, user_id=user_id,
            memory_type=[MEMORY_TYPE_SITE_EXPERIENCE, MEMORY_TYPE_LESSON],
            scope=SCOPE_DOMAIN, domain=(domain or None),
        )
    except Exception:
        return []

    ranked: list[dict[str, Any]] = []
    for item in candidates:
        raw_score = float(item.get("score", 0.0))
        if raw_score < threshold:
            continue
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
    """把常驻偏好组装成注入每步 prompt 的文本块。空则返回空串。"""
    if not preferences:
        return ""
    lines = ["## 用户偏好(始终遵守)"]
    for item in preferences:
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"- {content}")
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
