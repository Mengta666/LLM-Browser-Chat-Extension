"""会话历史存储(SQLite)。

存 chat 的完整对话消息,供"会话列表 + 续谈"使用。与长期记忆(Qdrant,全局跨会话)
是正交两层:这里按 chat_id 存"这次聊了什么",记忆存"用户是谁"。

设计从 chat 真实需求出发,不复用旧库的表结构。仿 agent/memory/history.py 的模式:
单连接 + 锁,写入量小、不追求高并发。任何异常都由调用方(api/service)吞掉,
存历史失败绝不能拖垮 chat 主流程。

两张表:
- chat_sessions:会话身份(chat_id, title, 时间戳, 软删标记)
- chat_messages:消息正文(message_id, chat_id, role, content, 时间戳)
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


# 独立库文件(与 agent_memory.sqlite3 分开,互不干扰)
_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "chat_history.sqlite3"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# 会话标题自动取首条 user 消息前 N 字
_TITLE_MAX_LEN = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    """惰性打开连接并建表。单连接 + 锁。"""
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id     TEXT PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                deleted_at  TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                message_id  TEXT PRIMARY KEY,
                chat_id     TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat "
            "ON chat_messages(chat_id, created_at);"
        )


def ensure_session(chat_id: str, first_user_text: str = "") -> bool:
    """会话不存在则新建(标题暂取首条 user 消息前 N 字);存在则刷新 updated_at。

    返回 True 表示本次是**新建**(供调用方决定是否触发 LLM 起标题),False 表示已存在。
    """
    if not chat_id:
        return False
    conn = _get_conn()
    now = _now_iso()
    title = str(first_user_text or "").strip().replace("\n", " ")[:_TITLE_MAX_LEN]
    with _lock, conn:
        row = conn.execute(
            "SELECT chat_id, title FROM chat_sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO chat_sessions (chat_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, title, now, now),
            )
            return True
        # 已存在:刷新时间;若旧标题为空且这次有文本,补上标题
        if not row["title"] and title:
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ?, title = ? WHERE chat_id = ?",
                (now, title, chat_id),
            )
        else:
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE chat_id = ?",
                (now, chat_id),
            )
        return False


def set_title(chat_id: str, title: str) -> bool:
    """设置会话标题(LLM 自动命名用,不改 updated_at 以免打乱列表排序)。空标题忽略。"""
    if not chat_id:
        return False
    new_title = str(title or "").strip().replace("\n", " ")[:_TITLE_MAX_LEN]
    if not new_title:
        return False
    conn = _get_conn()
    with _lock, conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE chat_id = ? AND deleted_at = ''",
            (new_title, chat_id),
        )
        return cur.rowcount > 0


def add_message(chat_id: str, role: str, content: str) -> None:
    """追加一条消息。role ∈ user/assistant。空 chat_id/content 忽略。"""
    if not chat_id or not str(content or "").strip():
        return
    conn = _get_conn()
    with _lock, conn:
        conn.execute(
            "INSERT INTO chat_messages (message_id, chat_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid4().hex, chat_id, role, content, _now_iso()),
        )


def list_sessions(limit: int = 100) -> list[dict[str, Any]]:
    """列出未删除会话,按 updated_at 倒序(最近在前)。"""
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT chat_id, title, created_at, updated_at FROM chat_sessions "
            "WHERE deleted_at = '' ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_messages(chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """取某会话的消息,按时间正序(供续谈重建对话)。"""
    if not chat_id:
        return []
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE chat_id = ? ORDER BY created_at LIMIT ?",
            (chat_id, max(1, int(limit))),
        ).fetchall()
    return [dict(r) for r in rows]


def rename_session(chat_id: str, title: str) -> bool:
    """重命名会话。会话不存在返回 False。"""
    if not chat_id:
        return False
    conn = _get_conn()
    new_title = str(title or "").strip().replace("\n", " ")[:_TITLE_MAX_LEN]
    with _lock, conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = ? "
            "WHERE chat_id = ? AND deleted_at = ''",
            (new_title, _now_iso(), chat_id),
        )
        return cur.rowcount > 0


def soft_delete(chat_id: str) -> bool:
    """软删会话(标记 deleted_at,消息正文保留可回溯)。会话不存在返回 False。"""
    if not chat_id:
        return False
    conn = _get_conn()
    with _lock, conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET deleted_at = ?, updated_at = ? "
            "WHERE chat_id = ? AND deleted_at = ''",
            (_now_iso(), _now_iso(), chat_id),
        )
        return cur.rowcount > 0
