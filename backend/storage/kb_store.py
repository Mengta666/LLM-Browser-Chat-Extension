"""知识库元数据与发布状态；SQLite 是知识库可检索性的依据。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from agent.memory.config import CHAT_USER_ID, MEMORY_DB_PATH

_lock = threading.Lock()


class KBNotFound(LookupError):
    pass


class KBConflict(ValueError):
    pass


def _get_conn() -> sqlite3.Connection:
    path = Path(MEMORY_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _transaction():
    with _lock, closing(_get_conn()) as conn, conn:
        yield conn


def _ensure_tables() -> None:
    with _transaction() as conn:
        old_columns = {r["name"] for r in conn.execute("PRAGMA table_info(kb_docs)")}
        if old_columns and "index_run_id" not in old_columns:
            backup = Path(MEMORY_DB_PATH).with_name(
                f"{Path(MEMORY_DB_PATH).stem}.kb-lifecycle-{uuid4().hex[:8]}.sqlite3")
            with closing(sqlite3.connect(str(backup))) as target:
                conn.backup(target)
        # sqlite3 默认不为 DDL 自动开启事务，迁移必须整体提交或回滚。
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_kbs (
                kb_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, deleted_at TEXT NOT NULL DEFAULT ''
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kb_docs (
                doc_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, filename TEXT NOT NULL,
                file_type TEXT NOT NULL, file_bytes INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending', error_msg TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, indexed_at TEXT NOT NULL DEFAULT '',
                deleted_at TEXT NOT NULL DEFAULT ''
            )""")
        additions = {
            "kb_kbs": {"delete_batch_id": "TEXT NOT NULL DEFAULT ''",
                       "sync_action": "TEXT NOT NULL DEFAULT ''",
                       "sync_error": "TEXT NOT NULL DEFAULT ''"},
            "kb_docs": {"content_hash": "TEXT NOT NULL DEFAULT ''",
                        "deleted_reason": "TEXT NOT NULL DEFAULT ''",
                        "delete_batch_id": "TEXT NOT NULL DEFAULT ''",
                        "index_run_id": "TEXT NOT NULL DEFAULT ''",
                        "published_run_id": "TEXT NOT NULL DEFAULT ''",
                        "sync_pending": "INTEGER NOT NULL DEFAULT 0",
                        "sync_error": "TEXT NOT NULL DEFAULT ''"},
        }
        for table, columns in additions.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if old_columns and "index_run_id" not in old_columns:
            # 旧索引先隔离，后台核对完整性后再开放；不重建或清空向量。
            conn.execute("UPDATE kb_docs SET sync_pending=1")
            conn.execute("UPDATE kb_kbs SET delete_batch_id=deleted_at WHERE deleted_at!=''")
            conn.execute("""
                UPDATE kb_docs SET delete_batch_id=(
                    SELECT delete_batch_id FROM kb_kbs WHERE kb_kbs.kb_id=kb_docs.kb_id)
                WHERE deleted_reason='cascade_from_kb' AND deleted_at!=''
                  AND EXISTS (SELECT 1 FROM kb_kbs WHERE kb_kbs.kb_id=kb_docs.kb_id
                              AND kb_kbs.deleted_at!='')""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_kbs_user ON kb_kbs(user_id,deleted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_docs_kb ON kb_docs(kb_id,deleted_at)")


_ensure_tables()


def _owned_kb(conn, kb_id, user_id, *, active=False):
    row = conn.execute("SELECT * FROM kb_kbs WHERE kb_id=? AND user_id=?",
                       (kb_id, user_id)).fetchone()
    if not row or (active and (row["deleted_at"] or row["sync_action"])):
        raise KBNotFound("知识库不存在或不可用")
    return row


def create_kb(kb_id: str, user_id: str, name: str, description: str, created_at: str) -> None:
    with _transaction() as conn:
        conn.execute("""INSERT INTO kb_kbs(kb_id,user_id,name,description,created_at,updated_at)
                        VALUES(?,?,?,?,?,?)""", (kb_id,user_id,name,description,created_at,created_at))


def get_kb(kb_id: str) -> Optional[dict[str, Any]]:
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM kb_kbs WHERE kb_id=?", (kb_id,)).fetchone()
    return dict(row) if row else None


def list_kbs(user_id: str) -> list[dict[str, Any]]:
    with _transaction() as conn:
        rows = conn.execute("SELECT * FROM kb_kbs WHERE user_id=? AND deleted_at='' ORDER BY created_at DESC",
                            (user_id,)).fetchall()
    return [dict(r) for r in rows]


def create_doc(doc_id: str, kb_id: str, filename: str, file_type: str,
               file_bytes: int, created_at: str, content_hash: str = "", *,
               index_run_id: str = "", user_id: str = CHAT_USER_ID) -> None:
    with _transaction() as conn:
        _owned_kb(conn, kb_id, user_id, active=True)
        if content_hash and conn.execute("""
            SELECT 1 FROM kb_docs WHERE kb_id=? AND content_hash=? AND deleted_at=''
            AND status IN ('pending','indexed')""", (kb_id, content_hash)).fetchone():
            raise KBConflict("该知识库已存在相同内容的待处理或已索引文档")
        conn.execute("""
            INSERT INTO kb_docs(doc_id,kb_id,filename,file_type,file_bytes,created_at,
                                content_hash,index_run_id,sync_pending)
            VALUES(?,?,?,?,?,?,?,?,1)""",
            (doc_id,kb_id,filename,file_type,file_bytes,created_at,content_hash,index_run_id))


def get_doc(doc_id: str) -> Optional[dict[str, Any]]:
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM kb_docs WHERE doc_id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_docs(kb_id: str) -> list[dict[str, Any]]:
    with _transaction() as conn:
        rows = conn.execute("SELECT * FROM kb_docs WHERE kb_id=? AND deleted_at='' ORDER BY created_at DESC",
                            (kb_id,)).fetchall()
    return [dict(r) for r in rows]


def document_catalog(kb_id: str, user_id: str, *, page: int = 1, limit: int = 20) -> dict:
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("page 必须是 1～1000000 的整数")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit 必须是 1～50 的整数")
    with _transaction() as conn:
        # 数量和当前页使用同一读取快照；不同页之间仍可能发生文档变更。
        conn.execute("BEGIN")
        kb = _owned_kb(conn, kb_id, user_id, active=True)
        counts = dict(conn.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(status='indexed'),0) AS indexed,
                   COALESCE(SUM(status='pending'),0) AS pending,
                   COALESCE(SUM(status='failed'),0) AS failed,
                   COALESCE(SUM(status='indexed' AND sync_pending=0 AND chunk_count>0),0) AS searchable
            FROM kb_docs WHERE kb_id=? AND deleted_at=''
        """, (kb_id,)).fetchone())
        rows = conn.execute("""
            SELECT doc_id,filename,status,chunk_count,sync_pending,
                   (status='indexed' AND sync_pending=0 AND chunk_count>0) AS searchable
            FROM kb_docs WHERE kb_id=? AND deleted_at=''
            ORDER BY created_at DESC,doc_id DESC LIMIT ? OFFSET ?
        """, (kb_id, limit, (page - 1) * limit)).fetchall()
    return {"kb_id": kb_id, "name": kb["name"], "counts": counts,
            "documents": [{**dict(row), "searchable": bool(row["searchable"]),
                           "sync_pending": bool(row["sync_pending"])} for row in rows],
            "page": page, "limit": limit, "has_more": page * limit < counts["total"]}


def all_docs(kb_id: str) -> list[dict[str, Any]]:
    with _transaction() as conn:
        rows = conn.execute("SELECT * FROM kb_docs WHERE kb_id=?", (kb_id,)).fetchall()
    return [dict(r) for r in rows]


def index_is_current(kb_id: str, doc_id: str, run_id: str, user_id: str) -> bool:
    with _transaction() as conn:
        return bool(conn.execute("""
            SELECT 1 FROM kb_docs d JOIN kb_kbs k ON k.kb_id=d.kb_id
            WHERE d.kb_id=? AND d.doc_id=? AND d.index_run_id=? AND d.status='pending'
              AND d.deleted_at='' AND k.deleted_at='' AND k.sync_action='' AND k.user_id=?""",
            (kb_id,doc_id,run_id,user_id)).fetchone())


def publish_index(kb_id: str, doc_id: str, run_id: str, count: int, now: str, user_id: str) -> bool:
    with _transaction() as conn:
        cursor = conn.execute("""
            UPDATE kb_docs SET status='indexed',published_run_id=?,chunk_count=?,indexed_at=?,
                               error_msg='',sync_pending=0,sync_error=''
            WHERE kb_id=? AND doc_id=? AND index_run_id=? AND status='pending' AND deleted_at=''
              AND EXISTS (SELECT 1 FROM kb_kbs k WHERE k.kb_id=kb_docs.kb_id
                          AND k.user_id=? AND k.deleted_at='' AND k.sync_action='')""",
            (run_id,count,now,kb_id,doc_id,run_id,user_id))
    return cursor.rowcount == 1


def fail_index(kb_id: str, doc_id: str, run_id: str, error: str) -> None:
    with _transaction() as conn:
        conn.execute("""UPDATE kb_docs SET status='failed',error_msg=?,sync_pending=1
                        WHERE kb_id=? AND doc_id=? AND index_run_id=? AND status='pending'""",
                     (error,kb_id,doc_id,run_id))


def mark_doc_deleted(kb_id: str, doc_id: str, now: str, user_id: str) -> None:
    with _transaction() as conn:
        _owned_kb(conn, kb_id, user_id, active=True)
        row = conn.execute("SELECT * FROM kb_docs WHERE kb_id=? AND doc_id=?", (kb_id,doc_id)).fetchone()
        if not row:
            raise KBNotFound("文档不存在于该知识库")
        conn.execute("""
            UPDATE kb_docs SET deleted_at=CASE WHEN deleted_at='' THEN ? ELSE deleted_at END,
                status=CASE WHEN status='pending' THEN 'failed' ELSE status END,
                error_msg=CASE WHEN status='pending' THEN '索引已取消，请重新上传' ELSE error_msg END,
                sync_pending=1 WHERE kb_id=? AND doc_id=?""", (now,kb_id,doc_id))


def mark_kb_deleted(kb_id: str, now: str, user_id: str) -> None:
    with _transaction() as conn:
        kb = _owned_kb(conn, kb_id, user_id)
        if kb["sync_action"] == "purge":
            raise KBConflict("知识库正在彻底删除")
        batch = kb["delete_batch_id"] if kb["deleted_at"] else uuid4().hex
        conn.execute("""
            UPDATE kb_docs SET deleted_at=?,deleted_reason='cascade_from_kb',delete_batch_id=?,
                status=CASE WHEN status='pending' THEN 'failed' ELSE status END,
                error_msg=CASE WHEN status='pending' THEN '索引已取消，请重新上传' ELSE error_msg END,
                sync_pending=1 WHERE kb_id=? AND deleted_at=''""", (now,batch,kb_id))
        conn.execute("""UPDATE kb_kbs SET deleted_at=?,delete_batch_id=?,sync_action='delete',sync_error=''
                        WHERE kb_id=? AND user_id=?""", (kb["deleted_at"] or now,batch,kb_id,user_id))


def request_restore(kb_id: str, user_id: str) -> None:
    with _transaction() as conn:
        kb = _owned_kb(conn, kb_id, user_id)
        if kb["sync_action"] == "purge":
            raise KBConflict("知识库正在彻底删除，不能还原")
        if kb["deleted_at"]:
            conn.execute("UPDATE kb_kbs SET sync_action='restore',sync_error='' WHERE kb_id=?", (kb_id,))


def finish_restore(kb_id: str, batch: str) -> bool:
    with _transaction() as conn:
        cursor = conn.execute("""
            UPDATE kb_kbs SET deleted_at='',sync_action='',sync_error=''
            WHERE kb_id=? AND delete_batch_id=? AND sync_action='restore'""", (kb_id,batch))
        if cursor.rowcount != 1:
            return False
        conn.execute("""
            UPDATE kb_docs SET deleted_at='',deleted_reason='',delete_batch_id='',
                sync_pending=CASE WHEN status='indexed' THEN 0 ELSE sync_pending END,sync_error=''
            WHERE kb_id=? AND deleted_reason='cascade_from_kb' AND delete_batch_id=?""", (kb_id,batch))
    return True


def finish_delete_sync(kb_id: str, batch: str) -> None:
    with _transaction() as conn:
        cursor = conn.execute("""
            UPDATE kb_kbs SET sync_action='',sync_error=''
            WHERE kb_id=? AND delete_batch_id=? AND sync_action='delete'""", (kb_id,batch))
        if cursor.rowcount:
            conn.execute("UPDATE kb_docs SET sync_pending=0,sync_error='' WHERE kb_id=?", (kb_id,))


def set_doc_sync(doc_id: str, run_id: str, pending: bool, error: str = "") -> None:
    with _transaction() as conn:
        conn.execute("UPDATE kb_docs SET sync_pending=?,sync_error=? WHERE doc_id=? AND index_run_id=?",
                     (int(pending),error,doc_id,run_id))


def set_kb_sync_error(kb_id: str, error: str) -> None:
    with _transaction() as conn:
        conn.execute("UPDATE kb_kbs SET sync_error=? WHERE kb_id=?", (error,kb_id))


def request_purge(kb_id: str, user_id: str) -> None:
    with _transaction() as conn:
        kb = _owned_kb(conn, kb_id, user_id)
        if not kb["deleted_at"]:
            raise KBConflict("必须先软删，再从回收站彻底删除")
        conn.execute("UPDATE kb_kbs SET sync_action='purge',sync_error='' WHERE kb_id=?", (kb_id,))


def finish_purge(kb_id: str) -> int:
    with _transaction() as conn:
        kb = conn.execute("SELECT * FROM kb_kbs WHERE kb_id=?", (kb_id,)).fetchone()
        if not kb or kb["sync_action"] != "purge" or not kb["deleted_at"]:
            raise KBConflict("彻底删除状态已改变")
        cursor = conn.execute("DELETE FROM kb_docs WHERE kb_id=?", (kb_id,))
        conn.execute("DELETE FROM kb_kbs WHERE kb_id=?", (kb_id,))
    return cursor.rowcount


def searchable_docs(kb_id: str, user_id: str, doc_ids: list[str]) -> dict[str, dict]:
    if not doc_ids:
        return {}
    with _transaction() as conn:
        placeholders = ",".join("?" for _ in doc_ids)
        rows = conn.execute(f"""
            SELECT d.* FROM kb_docs d JOIN kb_kbs k ON k.kb_id=d.kb_id
            WHERE d.kb_id=? AND k.user_id=? AND k.deleted_at='' AND k.sync_action=''
              AND d.deleted_at='' AND d.status='indexed' AND d.sync_pending=0 AND d.chunk_count>0
              AND d.doc_id IN ({placeholders})""", (kb_id,user_id,*doc_ids)).fetchall()
    return {row["doc_id"]: dict(row) for row in rows}


def list_deleted_kbs(user_id: str) -> list[dict[str, Any]]:
    with _transaction() as conn:
        rows = conn.execute("""
            SELECT k.*,(SELECT count(*) FROM kb_docs d WHERE d.kb_id=k.kb_id) AS doc_count
            FROM kb_kbs k WHERE user_id=? AND deleted_at!='' ORDER BY deleted_at DESC""", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def recover_interrupted(user_id: str) -> None:
    with _transaction() as conn:
        conn.execute("""
            UPDATE kb_docs SET status='failed',sync_pending=1,error_msg='后端重启中断索引，请重新上传'
            WHERE status='pending' AND kb_id IN (SELECT kb_id FROM kb_kbs WHERE user_id=?)""", (user_id,))


def pending_kbs(user_id: str) -> list[str]:
    with _transaction() as conn:
        rows = conn.execute("""
            SELECT k.kb_id FROM kb_kbs k WHERE user_id=? AND
                (sync_action!='' OR EXISTS(SELECT 1 FROM kb_docs d
                 WHERE d.kb_id=k.kb_id AND d.sync_pending=1))""", (user_id,)).fetchall()
    return [r["kb_id"] for r in rows]
