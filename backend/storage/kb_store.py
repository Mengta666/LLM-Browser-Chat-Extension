"""KB 元数据存储:SQLite 存 kb_kbs / kb_docs 两表。

与 chat_store 共用 chat_history.sqlite3,但表名独立、不互相依赖。
删 KB / doc 是软删(deleted_at),不物删。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from agent.memory.config import MEMORY_DB_PATH

_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """复用 chat_history.sqlite3(与 chat_store 共库不共表)。"""
    path = Path(MEMORY_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables() -> None:
    """建表(幂等):kb_kbs / kb_docs。"""
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_kbs (
                kb_id       TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                deleted_at  TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kb_kbs_user
            ON kb_kbs(user_id, deleted_at)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_docs (
                doc_id       TEXT PRIMARY KEY,
                kb_id        TEXT NOT NULL,
                filename     TEXT NOT NULL,
                file_type    TEXT NOT NULL,
                file_bytes   INTEGER NOT NULL,
                chunk_count  INTEGER NOT NULL DEFAULT 0,
                status       TEXT NOT NULL DEFAULT 'pending',
                error_msg    TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL,
                indexed_at   TEXT NOT NULL DEFAULT '',
                deleted_at   TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_kb_docs_kb
            ON kb_docs(kb_id, deleted_at)
        """)
        # 兼容迁移:存量库补 content_hash 列
        try:
            conn.execute("ALTER TABLE kb_docs ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        conn.commit()


_ensure_tables()


# ─── KB ───────────────────────────────────────────────────────────


def create_kb(kb_id: str, user_id: str, name: str, description: str, created_at: str) -> None:
    """建 KB(user_id 是 CHAT_USER_ID)。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO kb_kbs (kb_id, user_id, name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kb_id, user_id, name, description, created_at, created_at),
        )
        conn.commit()


def list_kbs(user_id: str) -> list[dict[str, Any]]:
    """列出该用户的所有 KB(不含软删)。"""
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT kb_id, name, description, created_at, updated_at
            FROM kb_kbs
            WHERE user_id = ? AND deleted_at = ''
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_kb(kb_id: str) -> Optional[dict[str, Any]]:
    """查单个 KB(含软删)。"""
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM kb_kbs WHERE kb_id = ?",
            (kb_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_kb(kb_id: str, deleted_at: str) -> None:
    """软删 KB。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            "UPDATE kb_kbs SET deleted_at = ? WHERE kb_id = ?",
            (deleted_at, kb_id),
        )
        conn.commit()


# ─── Doc ──────────────────────────────────────────────────────────


def create_doc(doc_id: str, kb_id: str, filename: str, file_type: str,
               file_bytes: int, created_at: str, content_hash: str = "") -> None:
    """建 doc(status 默认 pending)。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO kb_docs (doc_id, kb_id, filename, file_type, file_bytes, created_at, status, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (doc_id, kb_id, filename, file_type, file_bytes, created_at, content_hash),
        )
        conn.commit()


def find_duplicate_doc(kb_id: str, content_hash: str) -> Optional[dict[str, Any]]:
    """查同 KB 下是否已有相同 hash 的未删除文档。"""
    if not content_hash:
        return None
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT doc_id, filename, status FROM kb_docs WHERE kb_id = ? AND content_hash = ? AND deleted_at = ''",
            (kb_id, content_hash),
        ).fetchone()
    return dict(row) if row else None


def list_docs(kb_id: str) -> list[dict[str, Any]]:
    """列出该 KB 下所有 doc(不含软删)。"""
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT doc_id, filename, file_type, file_bytes, chunk_count, status, error_msg, created_at, indexed_at
            FROM kb_docs
            WHERE kb_id = ? AND deleted_at = ''
            ORDER BY created_at DESC
            """,
            (kb_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_doc(doc_id: str) -> Optional[dict[str, Any]]:
    """查单个 doc(含软删)。"""
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM kb_docs WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    return dict(row) if row else None


def update_doc_status(doc_id: str, status: str, error_msg: str = "",
                      chunk_count: int = 0, indexed_at: str = "") -> None:
    """更新 doc 状态(pending / indexed / failed)。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            """
            UPDATE kb_docs
            SET status = ?, error_msg = ?, chunk_count = ?, indexed_at = ?
            WHERE doc_id = ?
            """,
            (status, error_msg, chunk_count, indexed_at, doc_id),
        )
        conn.commit()


def delete_doc(doc_id: str, deleted_at: str) -> None:
    """软删 doc。"""
    with _lock, _get_conn() as conn:
        conn.execute(
            "UPDATE kb_docs SET deleted_at = ? WHERE doc_id = ?",
            (deleted_at, doc_id),
        )
        conn.commit()


def list_docs_by_kb(kb_id: str, include_deleted: bool = False) -> list[dict[str, Any]]:
    """列该 KB 下所有 doc_id(供级联软删)。"""
    with _lock, _get_conn() as conn:
        if include_deleted:
            rows = conn.execute(
                "SELECT doc_id FROM kb_docs WHERE kb_id = ?",
                (kb_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT doc_id FROM kb_docs WHERE kb_id = ? AND deleted_at = ''",
                (kb_id,),
            ).fetchall()
    return [dict(r) for r in rows]
