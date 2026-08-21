"""记忆变更审计日志(SQLite)。

这是纯审计层:记录每条记忆的 ADD/UPDATE/DELETE 演化痕迹,便于调试与回溯。
不参与检索,丢失不影响功能——事实源在 Qdrant payload。
"""

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from agent.memory.config import MEMORY_DB_PATH


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    """惰性打开连接并建表。单连接 + 锁,审计写入量很小,不追求并发。"""
    global _conn
    if _conn is None:
        MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(MEMORY_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """建审计表。"""
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_history (
                history_id    TEXT PRIMARY KEY,
                memory_id     TEXT NOT NULL,
                event         TEXT NOT NULL,
                prev_content  TEXT NOT NULL DEFAULT '',
                new_content   TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_history_memory_id "
            "ON memory_history(memory_id, created_at);"
        )


def add_history(memory_id: str, event: str,
                prev_content: str = "", new_content: str = "") -> None:
    """追加一条变更记录。event ∈ ADD/UPDATE/DELETE。"""
    conn = _get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO memory_history "
            "(history_id, memory_id, event, prev_content, new_content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid4().hex, memory_id, event, prev_content or "", new_content or "", _now_iso()),
        )


def list_history(memory_id: str) -> list[dict[str, Any]]:
    """按时间顺序取某条记忆的全部变更痕迹(调试用)。"""
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM memory_history WHERE memory_id = ? ORDER BY created_at",
            (memory_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_events(event: Optional[str] = None) -> int:
    """统计审计条数(测试用)。event 为空则统计全部。"""
    conn = _get_conn()
    with _lock:
        if event is None:
            row = conn.execute("SELECT COUNT(*) AS c FROM memory_history").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_history WHERE event = ?", (event,)
            ).fetchone()
    return int(row["c"])
