"""联网搜索接口，负责搜索、抓正文并返回每页 chunk 命中结果。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tools.web_search import fetch_url, retrieve_web_context, search_web


router = APIRouter(prefix="/search", tags=["联网搜索 API"])


class Query(BaseModel):
    """搜索接口请求体。"""

    query: str = ""
    limit: int = Field(default=8, description="搜索结果 URL 数量")
    result_limit: int = Field(default=4, description="参与正文检索的成功页面数量")
    chunk_limit: int = Field(default=4, description="每个命中页面返回的 chunk 数量")


@router.post("")
async def search(query: Query) -> dict:
    """执行一次联网搜索，并为成功抓取的页面附加 chunk 召回结果。"""
    normalized_query = query.query.strip()
    if not normalized_query:
        raise HTTPException(status_code=400, detail="query is required")

    limit = max(1, query.limit)
    result_limit = max(1, min(query.result_limit, limit))
    chunk_limit = max(1, query.chunk_limit)

    try:
        search_result = search_web(normalized_query, limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"search_web error: {exc}") from exc

    results: list[dict] = []
    fetched_page_count = 0
    for result in search_result["results"]:
        title = result.get("title", "")
        url = result.get("url", "")
        snippet = result.get("snippet", "")

        item = {
            "title": title,
            "url": url,
            "final_url": "",
            "content": snippet,
            "snippet": snippet,
            "content_source": "snippet",
            "content_length": len(snippet),
            "fetch_error": "",
        }

        # 只对足够参与正文召回的结果做抓取，其余结果保留搜索摘要。
        if fetched_page_count >= result_limit:
            results.append(item)
            continue

        if not url:
            item["fetch_error"] = "missing result url"
            results.append(item)
            continue

        try:
            fetched = fetch_url(url)
            item["title"] = fetched.get("title") or title
            item["final_url"] = fetched.get("final_url", "")
            item["content"] = fetched.get("content", "")
            item["content_source"] = "page"
            item["content_length"] = fetched.get("content_length", len(item["content"]))
            fetched_page_count += 1
        except Exception as exc:
            item["fetch_error"] = str(exc)

        results.append(item)

    try:
        # 召回阶段只消费成功抓到 page 正文的结果。
        retrieved_context = retrieve_web_context(
            normalized_query,
            results,
            top_k_results=result_limit,
            top_k_chunks=chunk_limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"retrieve_web_context error: {exc}") from exc

    processed_results = retrieved_context["results"]
    fetched_count = sum(1 for item in processed_results if item.get("content_source") == "page")
    failed_count = sum(1 for item in processed_results if item.get("fetch_error"))

    return {
        "query": normalized_query,
        "limit": limit,
        "result_limit": result_limit,
        "chunk_limit": chunk_limit,
        "search_results_count": len(processed_results),
        "fetched_count": fetched_count,
        "failed_count": failed_count,
        "retrieved_page_count": retrieved_context["retrieved_page_count"],
        "retrieved_chunk_count": retrieved_context["retrieved_chunk_count"],
        "unresponsive_engines": search_result.get("unresponsive_engines", []),
        "results": processed_results,
    }
