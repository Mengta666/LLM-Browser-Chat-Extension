# -*- coding: utf-8 -*-
"""SearXNG JSON API 对接。

GET {SEARXNG_API_URL}?q=...&format=json → {results: [{url, title, content, score}]}
超时/异常返空列表(不拖垮 chat 主流程)。
"""

import os
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

SEARXNG_API_URL = os.getenv("SEARXNG_API_URL", "http://localhost:8888/search")
SEARCH_TIMEOUT = int(os.getenv("SEARCH_TIMEOUT", "20"))


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def search_searxng(query: str, count: int = 5) -> list[SearchResult]:
    if not query or not query.strip():
        return []
    params = {
        'q': query.strip(),
        'format': 'json',
        'pageno': 1,
        'safesearch': '1',
        'language': 'all',
    }
    try:
        resp = requests.get(SEARXNG_API_URL, params=params, timeout=SEARCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    results = data.get('results', [])
    results.sort(key=lambda x: float(x.get('score', 0)), reverse=True)
    out = []
    for r in results[:count]:
        title = str(r.get('title', '')).strip()
        url = str(r.get('url', '')).strip()
        snippet = str(r.get('content', '')).strip()
        if url:
            out.append(SearchResult(title=title, url=url, snippet=snippet))
    return out
