"""记忆检索(注入 agent 前的读路径)。

对齐 mem0 的检索:embed(query) → Qdrant 语义搜索 → threshold 过滤。
不调 LLM(纯向量),按 domain + user 两个 scope 各取一批合并。
组装成注入 agent prompt 的文本块。
"""

from typing import Any, Optional
from urllib.parse import urlsplit

from agent.memory import vector as V
from agent.memory.config import (
    RETRIEVE_TOP_K, RETRIEVE_THRESHOLD,
    MEMORY_KIND_SEMANTIC, SCOPE_USER, SCOPE_DOMAIN, DEFAULT_USER_ID,
)
from rag.embedder import embed_text


def extract_domain(url: str) -> str:
    """从 URL 提取小写域名,失败返回空字符串。"""
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def retrieve_memories(query: str, *, domain: str = "",
                      top_k: int = RETRIEVE_TOP_K,
                      threshold: float = RETRIEVE_THRESHOLD,
                      user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """按查询检索相关记忆(user 全局偏好 + 当前 domain 站点事实)。

    Qdrant 不可用/空库时返回空列表(不抛异常,保证 agent 主流程不受影响)。
    返回按 score 降序、去重后的记忆列表。
    """
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return []

    try:
        query_vector = embed_text(normalized_query)
    except Exception:
        return []

    candidates: list[dict[str, Any]] = []
    try:
        # user 域:全局偏好,不限 domain
        candidates.extend(V.search_memories(
            query_vector, top_k=top_k, user_id=user_id,
            memory_kind=MEMORY_KIND_SEMANTIC, scope=SCOPE_USER,
        ))
        # domain 域:仅当前站点事实
        if domain:
            candidates.extend(V.search_memories(
                query_vector, top_k=top_k, user_id=user_id,
                memory_kind=MEMORY_KIND_SEMANTIC, scope=SCOPE_DOMAIN, domain=domain,
            ))
    except Exception:
        # 向量库异常 → 降级为空,不拖垮 agent
        return candidates

    # threshold 过滤 + 去重 + 排序
    seen_ids: set[str] = set()
    filtered: list[dict[str, Any]] = []
    for item in candidates:
        if float(item.get("score", 0.0)) < threshold:
            continue
        memory_id = str(item.get("memory_id", ""))
        if not memory_id or memory_id in seen_ids:
            continue
        seen_ids.add(memory_id)
        filtered.append(item)

    filtered.sort(key=lambda m: float(m.get("score", 0.0)), reverse=True)
    return filtered[:top_k]


def build_memory_block(memories: list[dict[str, Any]]) -> str:
    """把检索到的记忆组装成注入 prompt 的文本块。空则返回空串。"""
    if not memories:
        return ""

    lines = ["## 相关记忆(来自过往任务,供参考;与当前任务无关则忽略)"]
    for item in memories:
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        scope = item.get("scope", "")
        tag = f"[{item.get('domain', '')}] " if scope == SCOPE_DOMAIN and item.get("domain") else ""
        lines.append(f"- {tag}{content}")

    return "\n".join(lines) if len(lines) > 1 else ""


def retrieve_for_task(task: str, *, url: str = "",
                      user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """门面:按任务描述 + 当前页 URL 检索相关记忆。供 agent 任务开始时调用。"""
    return retrieve_memories(task, domain=extract_domain(url), user_id=user_id)
