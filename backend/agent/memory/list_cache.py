# -*- coding: utf-8 -*-
"""记忆列表内存缓存(叶子模块,无循环 import)。

单进程 dict + threading.Lock,Cache-Aside 模式:
- 读端 miss 时从 Qdrant 拉 → set cache
- 写端成功后 invalidate_all(清全部)
- TTL 兜底防脏(万一某写路径漏调 invalidate)

所有改动 memory 的函数(vector.py 4 个写函数 + promotion.demote + rethink.set_payload)
在 DB 操作成功后调 invalidate_all()。touch_memories 不 invalidate(只改
reinforce_count/last_accessed_at,前端不显示这两字段,清了反而命中率归零)。
"""

import threading
import time
from typing import Any, Optional


_cache: dict[tuple, dict[str, Any]] = {}
_lock = threading.Lock()
CACHE_TTL_SEC = 300  # 5 分钟兜底


def cache_key(memory_type: Optional[str],
              include_invalid: bool,
              include_episodic: bool) -> tuple:
    return (memory_type or "", bool(include_invalid), bool(include_episodic))


def get(key: tuple) -> Optional[dict]:
    with _lock:
        entry = _cache.get(key)
        if not entry:
            return None
        if time.time() > entry["expiry"]:
            _cache.pop(key, None)
            return None
        return entry["payload"]


def set(key: tuple, payload: dict) -> None:
    with _lock:
        _cache[key] = {"payload": payload, "expiry": time.time() + CACHE_TTL_SEC}


def invalidate_all() -> None:
    """所有写操作调用。清空全部 cache entry。"""
    with _lock:
        _cache.clear()
