# -*- coding: utf-8 -*-
"""SearXNG JSON API 对接。

GET {SEARXNG_API_URL}?q=...&format=json → {results: [{url, title, content, score}]}
结构化返回搜索状态，保留部分引擎成功的结果。
"""

import os
import time
import math
from datetime import datetime, timezone
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
    content: str = ""
    final_url: str = ""
    content_source: str = "snippet"
    read_status: str = "not_requested"
    read_error: str = ""
    extracted_date: str | None = None
    fetched_at: str | None = None
    truncated: bool = False
    content_length: int = 0


def search_searxng_with_status(query: str, count: int = 5, *, timeout: float | None = None) -> tuple[list[SearchResult], dict]:
    started = time.monotonic()
    meta = {'outcome': 'error', 'searched_at': datetime.now(timezone.utc).isoformat(),
            'unresponsive_engines': []}
    results = []
    params = {
        'q': query.strip(),
        'format': 'json',
        'pageno': 1,
        'safesearch': '1',
        'language': 'all',
    }
    try:
        if not query.strip():
            raise ValueError('empty_query')
        resp = requests.get(SEARXNG_API_URL, params=params,
                            timeout=min(SEARCH_TIMEOUT, timeout) if timeout is not None else SEARCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise ValueError('invalid_results')
        errors = data.get('unresponsive_engines') or []
        if not isinstance(errors, list):
            raise ValueError('invalid_engines')
        meta['unresponsive_engines'] = [
            [str(e[0])[:80], str(e[1])[:120]] for e in errors[:20]
            if isinstance(e, (list, tuple)) and len(e) >= 2]
        ranked = []
        for row in data['results']:
            if not isinstance(row, dict) or not isinstance(row.get('url'), str) or not row['url'].strip():
                continue
            try:
                score = float(row.get('score') or 0)
            except (TypeError, ValueError):
                score = 0
            ranked.append((score if math.isfinite(score) else 0, row))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        results = [SearchResult(str(row.get('title') or '').strip(), row['url'].strip(),
                                str(row.get('content') or '').strip()) for _, row in ranked[:count]]
        if results:
            meta['outcome'] = 'partial' if errors else 'found'
        elif errors:
            meta.update(error_code='search_engines_failed', error='搜索引擎返回错误，未获得可用结果；不能据此认定没有相关信息。')
        else:
            meta['outcome'] = 'empty'
    except requests.Timeout:
        meta.update(error_code='search_timeout', error='搜索服务请求超时。')
    except requests.ConnectionError:
        meta.update(error_code='search_connection_error', error='无法连接搜索服务，请检查后端网络、代理和搜索地址。')
    except requests.HTTPError as exc:
        meta.update(error_code='search_http_error', error='搜索服务返回 HTTP 错误。', http_status=exc.response.status_code)
    except requests.exceptions.JSONDecodeError:
        meta.update(error_code='search_invalid_response', error='搜索服务没有返回有效 JSON。')
    except requests.RequestException:
        meta.update(error_code='search_request_error', error='搜索服务请求失败。')
    except (ValueError, TypeError):
        meta.update(error_code='search_invalid_response', error='搜索服务响应格式不正确。')
    meta['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return results, meta


def search_searxng(query: str, count: int = 5) -> list[SearchResult]:
    return search_searxng_with_status(query, count)[0]
