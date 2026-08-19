"""来源（source）构造、筛选与引用清洗（纯逻辑，无 IO）。

从 api/chat.py 抽出的自洽函数簇：URL 规范化、web/page source 构造、
按 URL/domain 限额筛选、引用编号解析与清洗。均为无状态纯函数，仅依赖入参、
re、urllib 与 core.utils.safe_float / common.page_identity.canonicalize_url。

select_web_sources 的三个限额通过 keyword-only 参数传入（默认取环境变量），
调用方 chat.py 显式传入其模块级常量，以便测试对 chat.WEB_MAX_* 的猴子补丁
仍能生效。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

from core.utils import safe_float
from common.page_identity import canonicalize_url

WEB_MAX_SOURCES = int(os.getenv("WEB_MAX_SOURCES", "6"))
WEB_MAX_SOURCES_PER_DOMAIN = int(os.getenv("WEB_MAX_SOURCES_PER_DOMAIN", "2"))
WEB_MAX_CHUNKS_PER_URL = int(os.getenv("WEB_MAX_CHUNKS_PER_URL", "1"))


def safe_canonicalize_url(url: str) -> str:
    """规范化 source URL，失败时保留原始 URL。"""
    normalized_url = str(url or "").strip()
    if not normalized_url:
        return ""

    try:
        return canonicalize_url(normalized_url)
    except ValueError:
        return normalized_url


def extract_domain(url: str) -> str:
    """从 URL 提取小写域名，解析失败时返回空字符串。"""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def build_web_source_entry(source_id: str, match: dict[str, Any]) -> dict[str, Any]:
    """把联网搜索召回片段转换成标准 source。"""
    url = str(match.get("url", ""))
    canonical_url = safe_canonicalize_url(url)
    return {
        "source_id": source_id,
        "type": "web",
        "source_kind": "web_search",
        "url": url,
        "canonical_url": canonical_url,
        "domain": extract_domain(canonical_url or url),
        "title": str(match.get("title", "")),
        "content": str(match.get("content") or match.get("preview") or ""),
        "preview": str(match.get("preview", "")),
        "score": float(match.get("score", 0.0) or 0.0),
        "rank": safe_float(match.get("rank") or match.get("search_rank")),
        "content_source": str(match.get("content_source", "")),
        "quality_score": safe_float(match.get("quality_score")),
        "source_key": str(match.get("source_key", "")),
    }


def build_web_snippet_source(source_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """搜索正文抓取失败或无命中时，退回搜索摘要作为可引用来源。"""
    content = str(result.get("snippet") or result.get("content") or "").strip()
    url = str(result.get("final_url") or result.get("url") or "")
    canonical_url = safe_canonicalize_url(url)
    return {
        "source_id": source_id,
        "type": "web",
        "source_kind": "web_search",
        "url": url,
        "canonical_url": canonical_url,
        "domain": extract_domain(canonical_url or url),
        "title": str(result.get("title", "")),
        "content": content,
        "preview": content[:160],
        "score": 0.0,
        "rank": safe_float(result.get("rank") or result.get("search_rank")),
        "content_source": str(result.get("content_source", "snippet")),
        "quality_score": safe_float(result.get("search_quality_score")),
        "source_key": f"web_snippet:{source_id}",
    }


def merge_source_content(chunks: list[str]) -> str:
    """把同一文档下召回的多个 chunk 合并为内部 prompt 证据。"""
    cleaned_chunks: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        normalized = str(chunk or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned_chunks.append(normalized)

    return "\n\n".join(f"片段 {index}：\n{chunk}" for index, chunk in enumerate(cleaned_chunks, start=1))


def build_reference_source(source: dict[str, Any]) -> dict[str, Any]:
    """把内部 chunk source 转成返回给前端的文档级引用。"""
    reference = {
        key: value
        for key, value in source.items()
        if key not in {"content", "preview", "chunk_id", "chunk_ids", "source_key"}
    }
    reference["reference_kind"] = "document"
    return reference


def select_web_sources(
    candidates: list[dict[str, Any]],
    source_start_index: int,
    *,
    max_sources: int = WEB_MAX_SOURCES,
    max_sources_per_domain: int = WEB_MAX_SOURCES_PER_DOMAIN,
    max_chunks_per_url: int = WEB_MAX_CHUNKS_PER_URL,
) -> list[dict[str, Any]]:
    """按 URL/domain 限额筛选文档级 web sources，并把 chunk 合并为内部证据。"""
    selected: list[dict[str, Any]] = []
    selected_by_url: dict[str, dict[str, Any]] = {}
    domain_counts: dict[str, int] = {}

    for candidate in candidates:
        canonical_url = str(candidate.get("canonical_url") or "").strip()
        url = str(candidate.get("url") or "").strip()
        url_key = canonical_url or safe_canonicalize_url(url) or url
        domain_key = str(candidate.get("domain") or extract_domain(url_key)).strip().lower()

        if url_key in selected_by_url:
            source = selected_by_url[url_key]
            content_chunks = source.setdefault("_content_chunks", [])
            if len(content_chunks) < max_chunks_per_url:
                content = str(candidate.get("content") or "").strip()
                if content:
                    content_chunks.append(content)
                    source["matched_chunk_count"] = len(content_chunks)
                    source["content"] = merge_source_content(content_chunks)
                    source["score"] = max(safe_float(source.get("score")), safe_float(candidate.get("score")))
                    source["quality_score"] = max(
                        safe_float(source.get("quality_score")),
                        safe_float(candidate.get("quality_score")),
                    )
            continue

        if domain_key and domain_counts.get(domain_key, 0) >= max_sources_per_domain:
            continue

        source = dict(candidate)
        source["source_id"] = f"S{source_start_index + len(selected)}"
        source["canonical_url"] = url_key
        source["domain"] = domain_key
        content = str(source.get("content") or "").strip()
        source["_content_chunks"] = [content] if content else []
        source["matched_chunk_count"] = len(source["_content_chunks"])
        source["content"] = merge_source_content(source["_content_chunks"])
        selected.append(source)
        selected_by_url[url_key] = source

        if domain_key:
            domain_counts[domain_key] = domain_counts.get(domain_key, 0) + 1

        if len(selected) >= max_sources:
            break

    for source in selected:
        source.pop("_content_chunks", None)

    return selected


def select_page_sources(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把当前页召回的多个 chunk 聚合成文档级 page source。"""
    selected: list[dict[str, Any]] = []
    selected_by_key: dict[str, dict[str, Any]] = {}

    for candidate in candidates:
        canonical_url = str(candidate.get("canonical_url") or "").strip()
        url = str(candidate.get("url") or "").strip()
        page_id = str(candidate.get("page_id") or "").strip()
        key = canonical_url or safe_canonicalize_url(url) or url or page_id

        if key in selected_by_key:
            source = selected_by_key[key]
            content_chunks = source.setdefault("_content_chunks", [])
            content = str(candidate.get("content") or "").strip()
            if content:
                content_chunks.append(content)
                source["matched_chunk_count"] = len(content_chunks)
                source["content"] = merge_source_content(content_chunks)
            chunk_id = str(candidate.get("chunk_id") or "").strip()
            if chunk_id:
                source.setdefault("chunk_ids", []).append(chunk_id)
            source["score"] = max(safe_float(source.get("score")), safe_float(candidate.get("score")))
            continue

        source = dict(candidate)
        source["source_id"] = f"S{len(selected) + 1}"
        source["canonical_url"] = key
        source["domain"] = str(source.get("domain") or extract_domain(key)).strip().lower()
        content = str(source.get("content") or "").strip()
        chunk_id = str(source.get("chunk_id") or "").strip()
        source["_content_chunks"] = [content] if content else []
        source["matched_chunk_count"] = len(source["_content_chunks"])
        source["chunk_ids"] = [chunk_id] if chunk_id else []
        source["content"] = merge_source_content(source["_content_chunks"])
        selected.append(source)
        selected_by_key[key] = source

    for source in selected:
        source.pop("_content_chunks", None)

    return selected


def format_source_block(source: dict[str, Any]) -> str:
    """把单条 source 格式化成注入模型的参考片段文本。"""
    score = source.get("score", 0.0)
    source_type = "联网搜索片段" if source.get("type") == "web" else "网页正文片段"
    return (
        f"[{source['source_id']}] {source_type} | score={score:.4f}\n"
        f"标题：{source['title']}\n"
        f"URL：{source['url']}\n"
        f"内容：\n{source['content']}"
    )


def parse_source_ids(text: str) -> list[str]:
    """从自由文本中抽取去重后的 S 编号列表。"""
    ordered_ids: list[str] = []
    seen: set[str] = set()

    for token in re.split(r"[\s,，]+", text.strip()):
        normalized = token.strip().upper()
        if not normalized:
            continue

        if normalized.startswith("S") and normalized[1:].isdigit():
            source_id = normalized
        elif normalized.isdigit():
            source_id = f"S{normalized}"
        else:
            continue

        if source_id in seen:
            continue

        seen.add(source_id)
        ordered_ids.append(source_id)

    return ordered_ids


def sanitize_cited_source_text(text: str, sources: list[dict[str, Any]]) -> str:
    """清洗回答里的引用块，只保留当前有效的 source_id。"""
    if not text:
        return ""

    valid_ids = {
        str(source.get("source_id", "")).strip().upper()
        for source in sources
        if str(source.get("source_id", "")).strip()
    }

    def replace_block(match: re.Match[str]) -> str:
        """替换单个方括号引用块。"""
        raw_block = match.group(1)
        parsed_ids = parse_source_ids(raw_block)
        if not parsed_ids:
            return match.group(0)

        filtered_ids = [source_id for source_id in parsed_ids if source_id in valid_ids]
        if not filtered_ids:
            return ""

        return "[" + ", ".join(filtered_ids) + "]"

    return re.sub(r"\[([^\]]+)\]", replace_block, text)


def extract_cited_source_ids(text: str, sources: list[dict[str, Any]]) -> list[str]:
    """从回答文本中提取真正被引用到的 source_id。"""
    if not text or not sources:
        return []

    valid_ids = {source["source_id"] for source in sources}
    ordered_ids: list[str] = []
    for bracket_content in re.findall(r"\[([^\]]+)\]", text):
        for source_id in parse_source_ids(bracket_content):
            if source_id in valid_ids and source_id not in ordered_ids:
                ordered_ids.append(source_id)

    return ordered_ids


def filter_cited_sources(text: str, sources: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """按回答中出现的引用编号过滤最终返回给前端的 sources。"""
    cited_ids = extract_cited_source_ids(text, sources)
    if not cited_ids:
        return [], []

    source_map = {source["source_id"]: source for source in sources}
    return [source_map[source_id] for source_id in cited_ids if source_id in source_map], cited_ids
