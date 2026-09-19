"""可选正文服务客户端：本轮缓存、并发读取与相关段落预算。"""

from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
import re
import time
from urllib.parse import urlsplit

import requests

from .urls import normalize_web_url
from .excerpts import TurnWebBudget, select_blocks, validate_blocks


_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="web-reader")


def _read_one(base_url, key, url, deadline):
    remaining = deadline - time.monotonic()
    if remaining < 2:
        return {"status": "error", "error_code": "budget_exhausted"}
    try:
        with requests.Session() as session:
            # 服务地址由后端配置，不通过用户网页携带的地址或系统代理转发密钥。
            session.trust_env = False
            with session.post(base_url + "/v1/extract", json={"url": url, "timeout_seconds": min(30., remaining - 1)},
                              headers={"Authorization": "Bearer " + key}, allow_redirects=False,
                              timeout=(min(3, remaining), remaining), stream=True) as response:
                if response.status_code != 200:
                    code = {401: "unauthorized", 429: "reader_busy"}.get(response.status_code, "reader_unavailable")
                    return {"status": "error", "error_code": code}
                body = bytearray()
                for block in response.iter_content(16384):
                    if time.monotonic() >= deadline:
                        return {"status": "error", "error_code": "reader_timeout"}
                    body.extend(block)
                    if len(body) > 800_000:
                        return {"status": "error", "error_code": "invalid_response"}
                data = json.loads(body)
        if not isinstance(data, dict) or data.get("status") not in ("ok", "error"):
            raise ValueError()
        if data["status"] == "error":
            code = data.get("error_code", "")
            code = code if isinstance(code, str) and re.fullmatch(r"[a-z_0-9]{1,60}", code) else "invalid_response"
            return {"status": "error", "error_code": code}
        if (data.get("url") != url or not isinstance(data.get("content"), str) or not data["content"].strip()
                or len(data["content"]) > 100_000 or not isinstance(data.get("final_url"), str)
                or len(data["final_url"]) > 4096
                or not normalize_web_url(data["final_url"]) or not isinstance(data.get("title"), str)
                or len(data["title"]) > 500
                or type(data.get("truncated")) is not bool or type(data.get("content_length")) is not int
                or data["content_length"] < len(data["content"])
                or not isinstance(data.get("fetched_at"), str)
                or len(data["fetched_at"]) > 64
                or (data.get("extracted_date") is not None and
                    (not isinstance(data["extracted_date"], str) or len(data["extracted_date"]) > 64))):
            raise ValueError()
        validate_blocks(data)
        return data
    except requests.Timeout:
        return {"status": "error", "error_code": "reader_timeout"}
    except requests.RequestException:
        return {"status": "error", "error_code": "reader_unavailable"}
    except (ValueError, TypeError):
        return {"status": "error", "error_code": "invalid_response"}


def select_excerpt(text, query, token_budget):
    excerpt, truncated, _ = select_blocks(text, query, token_budget)
    return excerpt, truncated


def enrich_results(results, query, *, deadline, cache=None, context_tokens=None, budget=None):
    budget = budget if budget is not None else TurnWebBudget()
    available = min(budget.remaining, max(0, context_tokens)) if context_tokens is not None else budget.remaining
    meta = _fetch_results(results, deadline=deadline, cache=cache, context_tokens=available)
    meta["budget"] = budget.allocate(results, query, available)
    meta["included_count"] = sum(r.content_source == "page" and r.context_status == "included" for r in results)
    return meta


def _fetch_results(results, *, deadline, cache=None, context_tokens=None):
    if os.getenv("WEB_READER_ENABLED", "0").lower() not in ("1", "true"):
        return {"outcome": "disabled", "attempted": 0, "read_count": 0, "failed_count": 0}
    meta = {"outcome": "skipped", "attempted": 0, "read_count": 0, "failed_count": 0, "cached_count": 0}
    if not results:
        return meta
    try:
        base_url = os.getenv("WEB_READER_API_URL", "").strip().rstrip("/")
        parsed = urlsplit(base_url)
        key = os.getenv("WEB_READER_API_KEY", "").strip()
        max_pages = int(os.getenv("WEB_READER_MAX_PAGES", "3"))
        seconds = float(os.getenv("WEB_READER_TIMEOUT", "20"))
        tokens = int(os.getenv("WEB_READER_CONTEXT_TOKENS", "6000"))
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or len(key) < 32 or not key.isascii()
                or not 1 <= max_pages <= 5 or not 2 <= seconds <= 30 or not 128 <= tokens <= 16000):
            raise ValueError()
    except ValueError:
        for result in results:
            result.read_status = "error"
            result.read_error = "reader_configuration_error"
        return {**meta, "outcome": "error", "error_code": "reader_configuration_error"}
    if context_tokens is not None:
        tokens = min(tokens, max(0, context_tokens))
    deadline = min(deadline, time.monotonic() + seconds)
    if tokens < 128 or deadline - time.monotonic() < 2:
        return {**meta, "error_code": "budget_exhausted"}
    cache = {} if cache is None else cache
    selected = []
    seen = set()
    for result in results:
        identity = normalize_web_url(result.url)
        if identity and identity not in seen and len(selected) < max_pages:
            seen.add(identity)
            selected.append((result, identity))
        else:
            result.read_status = "skipped"
    pending = {}
    pages = {}
    for result, identity in selected:
        if identity in cache:
            pages[identity] = cache[identity]
            meta["cached_count"] += 1
        else:
            pending[_pool.submit(_read_one, base_url, key, result.url, deadline)] = identity
            meta["attempted"] += 1
    if pending:
        completed, unfinished = wait(pending, timeout=max(0, deadline - time.monotonic()))
        for future in completed:
            try:
                pages[pending[future]] = future.result()
            except Exception:
                pages[pending[future]] = {"status": "error", "error_code": "reader_unavailable"}
        for future in unfinished:
            future.cancel()
            pages[pending[future]] = {"status": "error", "error_code": "reader_timeout"}
    for result, identity in selected:
        page = pages[identity]
        if page.get("status") != "ok":
            result.read_status = "error"
            result.read_error = page.get("error_code", "reader_unavailable")
            meta["failed_count"] += 1
            continue
        cache[identity] = page
        cache[normalize_web_url(page["final_url"])] = page
        result.content = page["content"]
        if "blocks" in page:
            result.blocks = page["blocks"]
        result.final_url = page["final_url"]
        result.title = page.get("title") or result.title
        result.content_source = "page"
        result.read_status = "ok"
        result.read_error = ""
        result.truncated = page.get("truncated", False)
        result.content_length = page["content_length"]
        result.extracted_date = page.get("extracted_date")
        result.fetched_at = page.get("fetched_at")
        meta["read_count"] += 1
    meta["outcome"] = ("partial" if meta["failed_count"] else "read") if meta["read_count"] else "error" if meta["failed_count"] else "skipped"
    return meta
