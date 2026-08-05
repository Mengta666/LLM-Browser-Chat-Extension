"""通用结构化日志系统。

支持多通道（agent/chat/system）、文件持久化（JSONL 按天轮转）、内存缓存（供 API 查询）。
任何模块通过 get_logger(channel) 获取实例即可使用。

用法:
    from observability.logger import get_logger
    log = get_logger("agent")
    log.info("session_create", session_id="abc", data={"task": "xxx"})
"""

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_DIR.mkdir(exist_ok=True)


class StructuredLogger:
    """结构化日志器：写入 JSONL 文件 + 内存环形缓存。"""

    def __init__(self, channel: str, max_memory_entries: int = 1000):
        self.channel = channel
        self._memory: deque[dict[str, Any]] = deque(maxlen=max_memory_entries)

    def log(
        self,
        level: str,
        event: str,
        session_id: str = "",
        data: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "channel": self.channel,
            "level": level,
            "event": event,
            "session_id": session_id,
            "data": data or {},
            "duration_ms": duration_ms,
        }
        self._write_file(entry)
        self._memory.append(entry)
        return entry

    def info(self, event: str, **kwargs) -> dict[str, Any]:
        return self.log("info", event, **kwargs)

    def warn(self, event: str, **kwargs) -> dict[str, Any]:
        return self.log("warn", event, **kwargs)

    def error(self, event: str, **kwargs) -> dict[str, Any]:
        return self.log("error", event, **kwargs)

    def debug(self, event: str, **kwargs) -> dict[str, Any]:
        return self.log("debug", event, **kwargs)

    def query(
        self,
        session_id: str = "",
        level: str = "",
        event: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """从内存缓存中查询日志条目。"""
        results = self._memory
        if session_id:
            results = [e for e in results if e["session_id"] == session_id]
        if level:
            results = [e for e in results if e["level"] == level]
        if event:
            results = [e for e in results if event in e["event"]]
        return results[-limit:]

    def _write_file(self, entry: dict[str, Any]) -> None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filepath = LOG_DIR / f"{self.channel}_{date_str}.jsonl"
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


_loggers: dict[str, StructuredLogger] = {}


def get_logger(channel: str = "system") -> StructuredLogger:
    """获取指定通道的 logger 实例（单例）。"""
    if channel not in _loggers:
        _loggers[channel] = StructuredLogger(channel)
    return _loggers[channel]


def get_all_loggers() -> dict[str, StructuredLogger]:
    """获取所有已创建的 logger。"""
    return _loggers
