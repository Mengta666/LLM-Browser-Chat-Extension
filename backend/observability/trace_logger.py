"""聊天链路的结构化追踪日志工具。"""

import json
import logging
from datetime import datetime, timezone
from typing import Any


_LOGGER = logging.getLogger("chat_trace")

if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)

_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False


def utc_now_iso() -> str:
    """返回 UTC 时区的 ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def emit_trace(payload: dict[str, Any]) -> None:
    """以单行 JSON 的形式输出追踪日志。"""
    _LOGGER.info(json.dumps(payload, ensure_ascii=False))
