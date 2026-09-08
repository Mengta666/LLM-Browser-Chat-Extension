"""Reranker 模块 — 二阶段精排。

支持远程 API（zerank-2 / Cohere Rerank / BGE）。
未启用或调用失败时降级为原始 RRF 排序。
"""
import requests
from typing import Any
from agent.memory.config import (
    KB_RERANK_ENABLED, KB_RERANK_API_URL, KB_RERANK_API_KEY,
    KB_RERANK_MODEL, KB_RERANK_TOP_K
)


def rerank_chunks(query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对检索到的 chunks 进行精排。

    Args:
        query: 用户查询
        chunks: 粗召回结果，每个 dict 需包含 'content' 字段

    Returns:
        精排后的 chunks（降序），未启用或失败时返回原序
    """
    if not KB_RERANK_ENABLED or not chunks:
        return chunks

    if not KB_RERANK_API_URL or not KB_RERANK_API_KEY:
        return chunks  # 降级：配置不全

    try:
        documents = [c.get("content", "") for c in chunks]
        payload = {
            "model": KB_RERANK_MODEL,
            "query": query,
            "documents": documents,
            "top_n": min(KB_RERANK_TOP_K, len(documents)),
        }
        headers = {
            "Authorization": f"Bearer {KB_RERANK_API_KEY}",
            "Content-Type": "application/json"
        }
        resp = requests.post(
            KB_RERANK_API_URL,
            json=payload,
            headers=headers,
            timeout=10
        )
        resp.raise_for_status()

        results = resp.json().get("results", [])  # [{index, relevance_score}, ...]
        # 按返回顺序重排 chunks（API 已排序，降序）
        reranked = []
        for item in results:
            idx = item.get("index")
            if 0 <= idx < len(chunks):
                chunk_copy = chunks[idx].copy()
                chunk_copy["rerank_score"] = item.get("relevance_score", 0)
                reranked.append(chunk_copy)

        return reranked[:KB_RERANK_TOP_K]

    except Exception:
        # 降级：reranker 失败不影响检索，返回原序
        return chunks
