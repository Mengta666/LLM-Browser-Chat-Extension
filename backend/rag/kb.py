"""KB 业务逻辑层:上传 / 列表 / 删除 / 检索。

上传处理是后台 daemon 线程:parse → chunk → embed → insert_memory → update_doc_status。
检索复用 vector.search_memories,加 kb_id filter。
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.memory import vector as V
from agent.memory.config import (
    CHAT_USER_ID,
    INSTRUCT_KB,
    KB_CHUNK_OVERLAP,
    KB_CHUNK_SIZE,
    KB_SEARCH_TOP_K,
    MEMORY_TYPE_KB_CHUNK,
)
from rag import chunker, parser
from rag.embedder import embed_query, embed_texts
from storage import kb_store as KS


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_kb_id() -> str:
    return f"kb_{uuid.uuid4().hex[:8]}"


def _make_doc_id() -> str:
    return f"doc_{uuid.uuid4().hex[:8]}"


# ─── KB ───────────────────────────────────────────────────────────


def create_kb(name: str, description: str = "") -> dict[str, Any]:
    """建 KB,返回 {kb_id, name, description, created_at}。"""
    kb_id = _make_kb_id()
    now = _now_iso()
    KS.create_kb(kb_id, CHAT_USER_ID, name, description, now)
    return {"kb_id": kb_id, "name": name, "description": description, "created_at": now}


def list_kbs() -> list[dict[str, Any]]:
    """列出所有 KB(不含软删),返回 [{kb_id, name, description, created_at, updated_at}, ...]。"""
    return KS.list_kbs(CHAT_USER_ID)


def delete_kb(kb_id: str) -> None:
    """软删 KB + 级联软删所有 doc + 失效所有 chunks。"""
    now = _now_iso()
    docs = KS.list_docs_by_kb(kb_id, include_deleted=False)
    for doc in docs:
        delete_doc(kb_id, doc["doc_id"], reason="cascade_from_kb")
    KS.delete_kb(kb_id, now)


# ─── Doc ──────────────────────────────────────────────────────────


def add_doc(kb_id: str, file_path: str | Path, filename: str, file_type: str,
            content_hash: str = "") -> dict[str, Any]:
    """上传文档,立刻返回 {doc_id, status: "pending"},后台处理。"""
    path = Path(file_path)
    file_bytes = path.stat().st_size
    doc_id = _make_doc_id()
    now = _now_iso()
    KS.create_doc(doc_id, kb_id, filename, file_type, file_bytes, now, content_hash=content_hash)

    # 后台处理
    def _worker():
        try:
            _process_doc(kb_id, doc_id, path, file_type)
        except Exception as exc:
            KS.update_doc_status(doc_id, "failed", error_msg=str(exc)[:500])

    threading.Thread(target=_worker, daemon=True, name=f"kb-doc-{doc_id}").start()

    return {"doc_id": doc_id, "status": "pending", "filename": filename}


def _process_doc(kb_id: str, doc_id: str, path: Path, file_type: str) -> None:
    """后台处理:parse → chunk → embed → insert。"""
    try:
        # 1. parse
        text = parser.parse(path, file_type)

        # 2. chunk
        chunks = chunker.chunk_document(text, KB_CHUNK_SIZE, KB_CHUNK_OVERLAP)
        if not chunks:
            raise ValueError("切片结果为空")

        # 3. embed (分批,每批 32 条)
        chunk_texts = [c["text"] for c in chunks]
        vectors = embed_texts(chunk_texts, batch_size=32)

        # 4. batch insert (每批 64 条 upsert)
        filename = KS.get_doc(doc_id)["filename"]
        items = [
            {
                "content": chunk["text"],
                "vector": vec,
                "memory_type": MEMORY_TYPE_KB_CHUNK,
                "user_id": CHAT_USER_ID,
                "confidence": 1.0,
                "verified": False,
                "chat_id": "",
                "kb_id": kb_id,
                "doc_id": doc_id,
                "source": filename,
                "chunk_idx": i,
                "keywords": [],
            }
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        V.batch_insert_memories(items, batch_size=64)

        # 5. 更新状态
        KS.update_doc_status(doc_id, "indexed", chunk_count=len(chunks), indexed_at=_now_iso())
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def list_docs(kb_id: str) -> list[dict[str, Any]]:
    """列该 KB 下所有 doc(不含软删)。"""
    return KS.list_docs(kb_id)


def get_doc_status(kb_id: str, doc_id: str) -> dict[str, Any]:
    """查 doc 状态(pending / indexed / failed),供前端轮询。"""
    doc = KS.get_doc(doc_id)
    if not doc:
        return {"status": "not_found"}
    if doc["kb_id"] != kb_id:
        return {"status": "not_found"}
    return {
        "status": doc["status"],
        "error_msg": doc["error_msg"],
        "chunk_count": doc["chunk_count"],
        "indexed_at": doc["indexed_at"],
    }


def delete_doc(kb_id: str, doc_id: str, reason: str = "") -> None:
    """软删 doc + 失效该 doc 所有 chunks。"""
    now = _now_iso()
    _invalidate_doc_chunks(kb_id, doc_id)
    KS.delete_doc(doc_id, now, reason=reason)


def _invalidate_doc_chunks(kb_id: str, doc_id: str) -> None:
    """批量软失效该 doc 所有 chunks(一次 Qdrant API 调用)。"""
    V.invalidate_memories_by_filter(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id)


# ─── 检索 ─────────────────────────────────────────────────────────


def search_kb(kb_id: str, query: str, top_k: int = KB_SEARCH_TOP_K) -> list[dict[str, Any]]:
    """检索 KB,返回 top-K chunks(带 doc/source/chunk_idx)。"""
    query_vec = embed_query(query, INSTRUCT_KB)
    return V.search_memories(
        query_vector=query_vec,
        query_text=query,
        top_k=top_k,
        user_id=CHAT_USER_ID,
        memory_type=MEMORY_TYPE_KB_CHUNK,
        kb_id=kb_id,
        include_invalid=False,
    )


# ─── 回收站(trash) ──────────────────────────────────────────────


def list_deleted_kbs() -> list[dict[str, Any]]:
    """列已软删的 KB(附文档统计)。"""
    kbs = KS.list_deleted_kbs(CHAT_USER_ID)
    for kb in kbs:
        kb["doc_count"] = KS.count_docs_by_kb(kb["kb_id"])
    return kbs


def restore_kb(kb_id: str) -> None:
    """还原 KB + 级联恢复文档 + 恢复 Qdrant chunks。"""
    kb = KS.get_kb(kb_id)
    if not kb or not kb.get("deleted_at"):
        raise ValueError("KB 不在回收站")
    KS.restore_kb(kb_id)
    restored_doc_ids = KS.restore_docs_by_kb_cascade(kb_id)
    for doc_id in restored_doc_ids:
        V.restore_memories_by_filter(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id)


def hard_delete_kb(kb_id: str) -> dict[str, Any]:
    """彻底删除 KB:Qdrant 物删所有 chunks + SQLite 物删 docs + KB 行。"""
    kb = KS.get_kb(kb_id)
    if not kb or not kb.get("deleted_at"):
        raise ValueError("必须先软删,再从回收站彻底删除")
    chunks_deleted = V.delete_memories_by_filter(user_id=CHAT_USER_ID, kb_id=kb_id)
    docs_deleted = KS.hard_delete_docs_by_kb(kb_id)
    KS.hard_delete_kb(kb_id)
    return {"chunks_deleted": chunks_deleted, "docs_deleted": docs_deleted}
