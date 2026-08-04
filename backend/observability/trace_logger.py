"""聊天链路的结构化追踪日志工具（兼容层，重定向到 logger.py）。"""

from datetime import datetime, timezone
from typing import Any

from observability.logger import get_logger

_chat_log = get_logger("chat")


def utc_now_iso() -> str:
    """返回 UTC 时区的 ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def emit_trace(payload: dict[str, Any]) -> None:
    """以结构化日志形式输出追踪记录。"""
    event = payload.get("event", "trace")
    session_id = payload.get("session_id", payload.get("chat_id", ""))
    _chat_log.info(event, session_id=session_id, data=payload)
