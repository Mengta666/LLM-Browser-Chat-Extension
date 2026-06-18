"""联网搜索与网页正文召回工具。"""

import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import trafilatura
from dotenv import load_dotenv

from rag.chunker import chunk_text
from rag.cleaner import clean_page_text
from rag.retriever import retrieve_chunks


__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)


def build_preview(text: str, max_length: int = 160) -> str:
    """把长正文压缩成适合前端展示的简短预览。"""
    normalized = clean_page_text(text)
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


def search_web(query: str, top_k: int = 5) -> dict[str, Any]:
    """通过 SearXNG 发起搜索，并返回排序后的原始结果。"""
    normalized_query = clean_page_text(query)
    if not normalized_query:
        raise ValueError("query is required")

    url = os.getenv("SEARXNG_API_URL")
    if not url:
        raise RuntimeError("SEARXNG_API_URL is not set")

    try:
        response = requests.get(
            url=url,
            params={
                "q": normalized_query,
                "format": "json",
                "categories": "general",
                "engines": "google,brave,startpage",
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"web search request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"SearXNG returned {response.status_code}: {response.text[:1000]}"
        )

    payload = response.json()
    results = payload.get("results", [])
    results = sorted(
        results,
        key=lambda item: item.get("score", 0),
        reverse=True,
    )[:top_k]

    return {
        "query": normalized_query,
        "top_k": top_k,
        "results": [
            {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("content", ""),
                "rank": result.get("score", 0),
            }
            for result in results
        ],
        "unresponsive_engines": payload.get("unresponsive_engines", []),
    }


def fetch_url(url: str) -> dict[str, Any]:
    """下载网页并抽取可用于检索的正文。"""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url is required")

    target_url = url.strip()
    response = trafilatura.fetch_response(
        target_url,
        decode=True,
        with_headers=True,
    )
    if response is None or not response.html:
        raise RuntimeError(f"failed to download url: {target_url}")

    content = trafilatura.extract(
        response.html,
        url=response.url,
        output_format="txt",
        favor_precision=True,
        include_comments=False,
        include_tables=True,
        include_images=False,
        include_links=False,
        with_metadata=False,
    )
    if not content:
        raise RuntimeError(f"failed to extract main content: {response.url or target_url}")

    metadata = trafilatura.extract_metadata(
        response.html,
        default_url=response.url,
    )
    title = ""
    if metadata is not None:
        title = metadata.as_dict().get("title", "") or ""

    cleaned = clean_page_text(content)
    if not cleaned:
        raise RuntimeError(f"empty cleaned content: {response.url or target_url}")

    return {
        "url": target_url,
        # 某些抓取结果会返回相对路径，这里统一补成绝对地址。
        "final_url": urljoin(target_url, response.url or ""),
        "title": title,
        "content": cleaned,
        "content_length": len(cleaned),
    }


def retrieve_web_context(
    query: str,
    results: list[dict[str, Any]],
    top_k_results: int = 3,
    top_k_chunks: int = 8,
) -> dict[str, Any]:
    """对成功抓取的页面正文做分块召回，并把命中结果挂回各自页面。"""
    if not isinstance(results, list):
        raise ValueError("results must be a list")

    if not isinstance(top_k_results, int) or top_k_results <= 0:
        raise ValueError("top_k_results must be > 0")

    if not isinstance(top_k_chunks, int) or top_k_chunks <= 0:
        raise ValueError("top_k_chunks must be > 0")

    normalized_query = clean_page_text(query)
    processed_results: list[dict[str, Any]] = []
    retrieved_page_count = 0
    retrieved_chunk_count = 0
    page_results_used = 0

    for result_index, raw_result in enumerate(results):
        item = dict(raw_result) if isinstance(raw_result, dict) else {}
        item.setdefault("title", "")
        item.setdefault("url", "")
        item.setdefault("final_url", "")
        item.setdefault("content", "")
        item.setdefault("snippet", "")
        item.setdefault("content_source", "snippet")
        item.setdefault("content_length", 0)
        item.setdefault("fetch_error", "")
        item["matches"] = []
        item["retrieve_error"] = ""

        # top_k_results 表示参与正文召回的成功页面数，而不是结果数组下标。
        should_retrieve = (
            page_results_used < top_k_results
            and item.get("content_source") == "page"
            and bool(normalized_query)
        )

        if should_retrieve:
            page_results_used += 1
            content = item.get("content", "")
            try:
                content_chunks = chunk_text(content)
                if content_chunks:
                    retrieved = retrieve_chunks(
                        normalized_query,
                        content_chunks,
                        top_k=top_k_chunks,
                    )
                    normalized_matches = []
                    for chunk in retrieved:
                        if not isinstance(chunk, dict):
                            continue

                        chunk_content = chunk.get("content", "")
                        if not isinstance(chunk_content, str) or not chunk_content.strip():
                            continue

                        normalized_matches.append(
                            {
                                "type": "web",
                                "title": item.get("title", ""),
                                "url": item.get("final_url") or item.get("url", ""),
                                "preview": build_preview(chunk_content or item.get("snippet", "")),
                                "content": chunk_content,
                                "score": float(chunk.get("score", 0.0) or 0.0),
                                "source_key": f"web:{result_index}:{int(chunk.get('chunk_index', 10**9))}",
                                "result_index": result_index,
                                "chunk_index": int(chunk.get("chunk_index", 10**9)),
                            }
                        )
                    item["matches"] = normalized_matches
                    if normalized_matches:
                        retrieved_page_count += 1
                        retrieved_chunk_count += len(normalized_matches)
            except Exception as exc:
                item["retrieve_error"] = str(exc)

        processed_results.append(item)

    return {
        "results": processed_results,
        "retrieved_page_count": retrieved_page_count,
        "retrieved_chunk_count": retrieved_chunk_count,
    }


if __name__ == "__main__":
    """本地手工验证搜索、抓取与召回链路。"""
    search_result = search_web("what is rag?", 2)
    fetched_results = []
    for result in search_result["results"]:
        try:
            fetched = fetch_url(result["url"])
            fetched_results.append(
                {
                    "title": fetched.get("title") or result["title"],
                    "url": result["url"],
                    "final_url": fetched.get("final_url", ""),
                    "content": fetched.get("content", ""),
                    "snippet": result.get("snippet", ""),
                    "content_source": "page",
                    "content_length": fetched.get("content_length", 0),
                    "fetch_error": "",
                }
            )
        except Exception as exc:
            fetched_results.append(
                {
                    "title": result.get("title", ""),
                    "url": result.get("url", ""),
                    "final_url": "",
                    "content": result.get("snippet", ""),
                    "snippet": result.get("snippet", ""),
                    "content_source": "snippet",
                    "content_length": len(result.get("snippet", "")),
                    "fetch_error": str(exc),
                }
            )
    print(retrieve_web_context("what is rag?", fetched_results))
