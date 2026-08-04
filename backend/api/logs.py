"""日志查询 API 端点。"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from observability.logger import get_logger, get_all_loggers, LOG_DIR


router = APIRouter(prefix="/v1/logs", tags=["日志查询"])


@router.get("/query")
def query_logs(
    channel: str = "agent",
    session_id: str = "",
    level: str = "",
    event: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """从内存缓存查询日志。"""
    logger = get_logger(channel)
    return logger.query(session_id=session_id, level=level, event=event, limit=limit)


@router.get("/sessions")
def list_sessions(channel: str = "agent", limit: int = 20) -> list[dict[str, Any]]:
    """列出最近的会话（去重）。"""
    logger = get_logger(channel)
    sessions: dict[str, dict] = {}
    for entry in reversed(logger._memory):
        sid = entry.get("session_id", "")
        if not sid or sid in sessions:
            continue
        sessions[sid] = {
            "session_id": sid,
            "first_event": entry["event"],
            "timestamp": entry["timestamp"],
        }
        if len(sessions) >= limit:
            break
    return list(sessions.values())


@router.get("/files")
def list_log_files() -> list[dict[str, Any]]:
    """列出可用的日志文件。"""
    files = sorted(LOG_DIR.glob("*.jsonl"), reverse=True)
    return [{"name": f.name, "size_bytes": f.stat().st_size} for f in files[:30]]
