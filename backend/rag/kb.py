"""知识库生命周期：暂存索引、条件发布、逻辑删除与可重试向量同步。"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent.memory import vector as V
from agent.memory.config import CHAT_USER_ID, INSTRUCT_KB, KB_CHUNK_OVERLAP, KB_CHUNK_SIZE, KB_SEARCH_TOP_K, MEMORY_TYPE_KB_CHUNK
from observability.logger import get_logger
from rag import chunker, parser
from rag.embedder import embed_query, embed_texts
from storage import kb_store as KS

_log = get_logger("kb")
_locks_guard = threading.Lock()
_locks = {}
_workers = set()
_workers_guard = threading.Lock()
_recovery_stop = threading.Event()
_recovery_thread = None


class KBSyncUnavailable(RuntimeError):
    pass


class IndexCancelled(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kb_lock(kb_id):
    with _locks_guard:
        return _locks.setdefault(kb_id, threading.RLock())


def _require_kb(kb_id, *, active=False):
    kb = KS.get_kb(kb_id)
    if not kb or kb["user_id"] != CHAT_USER_ID or (active and (kb["deleted_at"] or kb["sync_action"])):
        raise KS.KBNotFound("知识库不存在或不可用")
    return kb


def _has_worker(kb_id, doc_id=None):
    with _workers_guard:
        return any(k == kb_id and (doc_id is None or d == doc_id) for k, d, _ in _workers)


def _restore_index(kb_id, doc):
    run_id = doc["published_run_id"]
    if not V.kb_index_complete(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc["doc_id"],
                               index_run_id=run_id, expected=doc["chunk_count"]):
        raise KBSyncUnavailable("索引片段不完整，需重新上传或人工核验")
    V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc["doc_id"], valid=False)
    V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc["doc_id"],
                         index_run_id=run_id, valid=True)


def _sync_doc_locked(kb_id, doc):
    kb = _require_kb(kb_id)
    if doc["status"] == "pending" and not doc["deleted_at"] and not kb["deleted_at"]:
        return False
    try:
        if doc["status"] == "indexed" and not doc["deleted_at"] and not kb["deleted_at"]:
            _restore_index(kb_id, doc)
        else:
            run_id = None if doc["deleted_at"] or kb["deleted_at"] else doc["index_run_id"]
            V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc["doc_id"],
                                 index_run_id=run_id, valid=False)
        if _has_worker(kb_id, doc["doc_id"]):
            return False
        KS.set_doc_sync(doc["doc_id"], doc["index_run_id"], False)
        return True
    except Exception as exc:
        error = str(exc) if isinstance(exc, KBSyncUnavailable) else type(exc).__name__
        KS.set_doc_sync(doc["doc_id"], doc["index_run_id"], True, error)
        _log.warn("kb_sync_pending", data={"kb_id": kb_id, "doc_id": doc["doc_id"], "error_type": type(exc).__name__})
        return False


def _sync_kb_locked(kb_id):
    kb = _require_kb(kb_id)
    try:
        if kb["sync_action"] == "delete":
            V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, valid=False)
            if _has_worker(kb_id):
                return False
            KS.finish_delete_sync(kb_id, kb["delete_batch_id"])
        elif kb["sync_action"] == "restore":
            docs = [d for d in KS.all_docs(kb_id) if d["deleted_reason"] == "cascade_from_kb"
                    and d["delete_batch_id"] == kb["delete_batch_id"]]
            for doc in docs:
                if doc["status"] == "indexed":
                    _restore_index(kb_id, doc)
                else:
                    V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc["doc_id"], valid=False)
            if not KS.finish_restore(kb_id, kb["delete_batch_id"]):
                raise KBSyncUnavailable("恢复状态已改变，请重试")
        elif kb["sync_action"] == "purge":
            if _has_worker(kb_id):
                return False
            V.purge_kb_chunks(user_id=CHAT_USER_ID, kb_id=kb_id)
            KS.finish_purge(kb_id)
        else:
            complete = True
            for doc in KS.all_docs(kb_id):
                if doc["sync_pending"]:
                    complete = _sync_doc_locked(kb_id, doc) and complete
            return complete
        return True
    except Exception as exc:
        error = str(exc) if isinstance(exc, KBSyncUnavailable) else type(exc).__name__
        KS.set_kb_sync_error(kb_id, error)
        _log.warn("kb_sync_pending", data={"kb_id": kb_id, "error_type": type(exc).__name__})
        return False


def reconcile_pending() -> None:
    for kb_id in KS.pending_kbs(CHAT_USER_ID):
        lock = _kb_lock(kb_id)
        if not lock.acquire(blocking=False):
            continue
        try:
            if KS.get_kb(kb_id):
                _sync_kb_locked(kb_id)
        finally:
            lock.release()


def start_recovery() -> None:
    global _recovery_thread
    if _recovery_thread and _recovery_thread.is_alive():
        return
    KS.recover_interrupted(CHAT_USER_ID)
    _recovery_stop.clear()

    def run():
        while not _recovery_stop.is_set():
            try:
                reconcile_pending()
            except Exception as exc:
                _log.warn("kb_recovery_failed", data={"error_type": type(exc).__name__})
            _recovery_stop.wait(30)

    _recovery_thread = threading.Thread(target=run, daemon=True, name="kb-recovery")
    _recovery_thread.start()


def stop_recovery() -> None:
    _recovery_stop.set()
    if _recovery_thread:
        _recovery_thread.join(timeout=1)


def create_kb(name: str, description: str = "") -> dict[str, Any]:
    kb_id, now = f"kb_{uuid.uuid4().hex[:8]}", _now_iso()
    KS.create_kb(kb_id, CHAT_USER_ID, name, description, now)
    return {"kb_id": kb_id, "name": name, "description": description, "created_at": now}


def list_kbs() -> list[dict[str, Any]]:
    return KS.list_kbs(CHAT_USER_ID)


def delete_kb(kb_id: str) -> dict:
    with _kb_lock(kb_id):
        KS.mark_kb_deleted(kb_id, _now_iso(), CHAT_USER_ID)
        complete = _sync_kb_locked(kb_id)
        return {"ok": True, "sync_pending": not complete}


def add_doc(kb_id: str, file_path: str | Path, filename: str, file_type: str,
            content_hash: str = "") -> dict[str, Any]:
    path = Path(file_path)
    doc_id, run_id = f"doc_{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    with _kb_lock(kb_id):
        KS.create_doc(doc_id, kb_id, filename, file_type, path.stat().st_size, _now_iso(),
                      content_hash, index_run_id=run_id, user_id=CHAT_USER_ID)
        with _workers_guard:
            _workers.add((kb_id, doc_id, run_id))
        worker = threading.Thread(target=_process_doc, args=(kb_id,doc_id,path,file_type,run_id),
                                  daemon=True, name=f"kb-doc-{doc_id}")
        try:
            worker.start()
        except Exception:
            with _workers_guard:
                _workers.discard((kb_id, doc_id, run_id))
            KS.fail_index(kb_id, doc_id, run_id, "无法启动索引，请重新上传")
            raise
    return {"doc_id": doc_id, "status": "pending", "filename": filename}


def _process_doc(kb_id: str, doc_id: str, path: Path, file_type: str, run_id: str) -> None:
    def check_current():
        if not KS.index_is_current(kb_id, doc_id, run_id, CHAT_USER_ID):
            raise IndexCancelled("索引任务已失效")

    try:
        check_current()
        text = parser.parse(path, file_type)
        chunks = chunker.chunk_document(text, KB_CHUNK_SIZE, KB_CHUNK_OVERLAP)
        if not chunks:
            raise ValueError("文档没有可索引的文本")
        check_current()
        vectors = embed_texts([c["text"] for c in chunks], batch_size=32)
        if len(vectors) != len(chunks):
            raise ValueError("向量数量与片段数量不一致")
        check_current()
        filename = KS.get_doc(doc_id)["filename"]
        items = [{
            "content": chunk["text"], "vector": vector, "memory_type": MEMORY_TYPE_KB_CHUNK,
            "user_id": CHAT_USER_ID, "kb_id": kb_id, "doc_id": doc_id, "index_run_id": run_id,
            "source": filename, "chunk_idx": i, "chunk_id": i,
            "prev_chunk_id": i - 1 if i else None,
            "next_chunk_id": i + 1 if i + 1 < len(chunks) else None, "valid": False,
        } for i, (chunk, vector) in enumerate(zip(chunks, vectors))]
        V.batch_insert_memories(items, batch_size=64, before_batch=check_current)
        with _kb_lock(kb_id):
            check_current()
            if not V.kb_index_complete(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id,
                                       index_run_id=run_id, expected=len(chunks)):
                raise ValueError("索引片段不完整")
            V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id,
                                 index_run_id=run_id, valid=True)
            if not KS.publish_index(kb_id, doc_id, run_id, len(chunks), _now_iso(), CHAT_USER_ID):
                raise IndexCancelled("发布条件已失效")
    except Exception as exc:
        KS.fail_index(kb_id, doc_id, run_id,
                      "索引已取消，请重新上传" if isinstance(exc, IndexCancelled)
                      else f"索引失败（{type(exc).__name__}），请重新上传")
        _log.warn("kb_index_failed", data={"kb_id": kb_id, "doc_id": doc_id, "error_type": type(exc).__name__})
    finally:
        try:
            with _kb_lock(kb_id):
                with _workers_guard:
                    _workers.discard((kb_id, doc_id, run_id))
                if KS.get_kb(kb_id):
                    _sync_kb_locked(kb_id)
        finally:
            path.unlink(missing_ok=True)


def list_docs(kb_id: str) -> list[dict[str, Any]]:
    _require_kb(kb_id, active=True)
    return KS.list_docs(kb_id)


def get_doc_status(kb_id: str, doc_id: str) -> dict[str, Any]:
    kb, doc = KS.get_kb(kb_id), KS.get_doc(doc_id)
    if not kb or kb["user_id"] != CHAT_USER_ID or kb["deleted_at"] or not doc or doc["kb_id"] != kb_id or doc["deleted_at"]:
        return {"status": "not_found"}
    return {key: doc[key] for key in ("status","error_msg","chunk_count","indexed_at","sync_pending","sync_error")}


def delete_doc(kb_id: str, doc_id: str) -> dict:
    with _kb_lock(kb_id):
        KS.mark_doc_deleted(kb_id, doc_id, _now_iso(), CHAT_USER_ID)
        complete = _sync_doc_locked(kb_id, KS.get_doc(doc_id))
        return {"ok": True, "sync_pending": not complete}


def _admit_chunks(kb_id: str, chunks: list[dict]) -> list[dict]:
    docs = KS.searchable_docs(kb_id, CHAT_USER_ID, list({c.get("doc_id", "") for c in chunks}))
    return [c for c in chunks if c.get("doc_id") in docs
            and c.get("index_run_id", "") == docs[c["doc_id"]]["published_run_id"]]


def _get_sibling_chunk(kb_id: str, doc_id: str, chunk_id: int,
                       index_run_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    docs = KS.searchable_docs(kb_id, CHAT_USER_ID, [doc_id])
    if doc_id not in docs:
        return None
    published = docs[doc_id]["published_run_id"]
    if index_run_id is not None and index_run_id != published:
        return None
    chunk = V.get_kb_chunk(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id,
                          index_run_id=published, chunk_idx=chunk_id)
    admitted = _admit_chunks(kb_id, [chunk]) if chunk else []
    return admitted[0] if admitted else None


def _recall_score(chunk: dict) -> float:
    score = chunk.get("rerank_score")
    return chunk.get("score", 0) if score is None else score


def search_kb(kb_id: str, query: str, top_k: int = KB_SEARCH_TOP_K) -> list[dict[str, Any]]:
    """检索 KB,返回 top-K chunks(带 doc/source/chunk_idx)。

    流程: 粗召回(hybrid search) → rerank(可选) → 相关性过滤 → 日志
    """
    import time
    from agent.memory.config import KB_RECALL_MIN_SCORE, KB_RERANK_ENABLED
    from .reranker import rerank_chunks
    from observability.logger import get_logger

    start = time.time()
    kb = KS.get_kb(kb_id)
    if not kb or kb["user_id"] != CHAT_USER_ID or kb["deleted_at"] or kb["sync_action"]:
        return []
    query_vec = embed_query(query, INSTRUCT_KB)

    top_k = max(1, min(int(top_k), 500))
    results = []
    raw_count = 0
    for multiplier in (1, 3, 9):
        limit = min(top_k * multiplier, 500)
        candidates = V.search_memories(
            query_vector=query_vec, query_text=query, top_k=limit, user_id=CHAT_USER_ID,
            memory_type=MEMORY_TYPE_KB_CHUNK, kb_id=kb_id, include_invalid=False)
        raw_count = len(candidates)
        results = _admit_chunks(kb_id, candidates)[:top_k]
        if len(results) >= top_k or raw_count < limit or limit == 500:
            break

    # Rerank（如果启用）
    admitted_count = len(results)
    diagnostics = {}
    results = rerank_chunks(query, results, diagnostics=diagnostics)
    scores = [_recall_score(chunk) for chunk in results]

    # 相关性过滤
    filtered = []
    for chunk in results:
        score = _recall_score(chunk)
        if score >= KB_RECALL_MIN_SCORE:
            filtered.append(chunk)

    # 窗口扩展：为每个命中 chunk 附带前后 ±1 chunk
    expanded = []
    for chunk in filtered:
        doc_id = chunk.get("doc_id", "")
        chunk_id = chunk.get("chunk_id")
        prev_id = chunk.get("prev_chunk_id")
        next_id = chunk.get("next_chunk_id")

        # 读取前后 chunk（同一文档内）
        context_chunks = [chunk]  # 中心 chunk

        if prev_id is not None:
            prev_chunk = _get_sibling_chunk(kb_id, doc_id, prev_id, chunk.get('index_run_id', ''))
            if prev_chunk:
                context_chunks.insert(0, prev_chunk)

        if next_id is not None:
            next_chunk = _get_sibling_chunk(kb_id, doc_id, next_id, chunk.get('index_run_id', ''))
            if next_chunk:
                context_chunks.append(next_chunk)

        # 拼接上下文
        expanded_content = "\n".join([c.get("content", "") for c in context_chunks])
        expanded_chunk = chunk.copy()
        expanded_chunk["content"] = expanded_content
        expanded_chunk["window_size"] = len(context_chunks)  # 标记扩展了几个 chunk
        expanded_chunk["window_chunk_ids"] = [c.get("chunk_idx", c.get("chunk_id")) for c in context_chunks]
        expanded.append(expanded_chunk)

    expanded = _admit_chunks(kb_id, expanded)

    # 日志
    try:
        _kb_log = get_logger("kb")
        _kb_log.info("kb_search", data={
            "kb_id": kb_id,
            "query_head": query[:60],
            "raw_count": raw_count,
            "admitted_count": admitted_count,
            "reranked_count": len(results),
            "filtered_count": len(filtered),
            "expanded_count": len(expanded),
            "rerank_enabled": KB_RERANK_ENABLED,
            "top_score": max(scores) if scores else None,
            "min_score": min(scores) if scores else None,
            "score_threshold": KB_RECALL_MIN_SCORE,
            "filtered_top_score": max((_recall_score(c) for c in filtered), default=None),
            **diagnostics,
            "score_source": ("rerank" if results[0].get("rerank_score") is not None else "recall") if results else "none",
            "avg_window": sum(c.get("window_size", 1) for c in expanded) / len(expanded) if expanded else 0,
            "elapsed_ms": int((time.time() - start) * 1000)
        })
    except Exception:
        pass

    return expanded



def list_deleted_kbs() -> list[dict[str, Any]]:
    return KS.list_deleted_kbs(CHAT_USER_ID)


def restore_kb(kb_id: str) -> dict:
    with _kb_lock(kb_id):
        KS.request_restore(kb_id, CHAT_USER_ID)
        if not _sync_kb_locked(kb_id):
            raise KBSyncUnavailable("恢复尚未完成，后台将继续同步，也可重新尝试")
        return {"ok": True, "sync_pending": False}


def hard_delete_kb(kb_id: str) -> dict[str, Any]:
    with _kb_lock(kb_id):
        _require_kb(kb_id)
        if _has_worker(kb_id):
            raise KS.KBConflict("索引任务正在结束，请稍后再彻底删除")
        KS.request_purge(kb_id, CHAT_USER_ID)
        try:
            count = V.purge_kb_chunks(user_id=CHAT_USER_ID, kb_id=kb_id)
            docs = KS.finish_purge(kb_id)
            return {"ok": True, "chunks_deleted": count, "docs_deleted": docs}
        except Exception as exc:
            KS.set_kb_sync_error(kb_id, type(exc).__name__)
            raise KBSyncUnavailable("彻底删除尚未完成，可重试；知识库仍不可检索") from exc
