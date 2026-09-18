"""Reranker 模块 — 二阶段精排。

支持远程 API（zerank-2 / Cohere Rerank / BGE）。
未启用或调用失败时降级为原始 RRF 排序。
"""
import math
import requests
from typing import Any
from observability.logger import get_logger
from agent.memory.config import (
    KB_RERANK_ENABLED, KB_RERANK_API_URL, KB_RERANK_API_KEY,
    KB_RERANK_MODEL, KB_RERANK_TOP_K
)


def rerank_chunks(query: str, chunks: list[dict[str, Any]], *, diagnostics: dict | None = None) -> list[dict[str, Any]]:
    """对检索到的 chunks 进行精排。

    Args:
        query: 用户查询
        chunks: 粗召回结果，每个 dict 需包含 'content' 字段

    Returns:
        精排后的 chunks（降序），未启用或失败时返回原序
    """
    if diagnostics is None:
        diagnostics = {}
    diagnostics.update(rerank_status="disabled" if not KB_RERANK_ENABLED else "no_candidates", fallback_reason=None)
    if not KB_RERANK_ENABLED or not chunks:
        return chunks

    if not KB_RERANK_API_URL or not KB_RERANK_API_KEY:
        diagnostics.update(rerank_status="fallback", fallback_reason="missing_config")
        get_logger("kb").warn("kb_rerank_fallback", data={"reason": "missing_config"})
        return chunks  # 降级：配置不全

    failure_reason = "request_failed"
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

        failure_reason = "invalid_response"
        results = resp.json()["results"]  # [{index, relevance_score}, ...]
        if not isinstance(results, list):
            raise ValueError("results must be a list")
        # 按返回顺序重排 chunks（API 已排序，降序）
        reranked = []
        seen = set()
        for item in results:
            idx = item["index"]
            score = item["relevance_score"]
            if type(idx) is not int or not 0 <= idx < len(chunks) or idx in seen:
                raise ValueError("invalid or duplicate index")
            if type(score) not in (int, float) or not math.isfinite(score):
                raise ValueError("invalid relevance_score")
            seen.add(idx)
            chunk_copy = chunks[idx].copy()
            chunk_copy["rerank_score"] = score
            reranked.append(chunk_copy)

        diagnostics["rerank_status"] = "success"
        return reranked[:KB_RERANK_TOP_K]

    except Exception:
        diagnostics.update(rerank_status="fallback", fallback_reason=failure_reason)
        # 降级：reranker 失败不影响检索，返回原序
        get_logger("kb").warn("kb_rerank_fallback", data={"reason": failure_reason})
        return chunks
