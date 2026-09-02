# -*- coding: utf-8 -*-
"""搜索统一入口。后续扩展 Tavily/Bing 直连时只改这里。"""

import os
from pathlib import Path

from dotenv import load_dotenv

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

SEARCH_ENABLED = os.getenv("SEARCH_ENABLED", "0") == "1"
SEARCH_RESULT_COUNT = int(os.getenv("SEARCH_RESULT_COUNT", "5"))

from search.searxng import search_searxng, SearchResult  # noqa: E402


def search_web(query: str, count: int = SEARCH_RESULT_COUNT) -> list[SearchResult]:
    if not SEARCH_ENABLED:
        return []
    return search_searxng(query, count)
