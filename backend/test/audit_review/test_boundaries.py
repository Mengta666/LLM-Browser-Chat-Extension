"""第二轮隔离审计：真实路由、SQLite 和内存 Qdrant，不加载真实配置或联网。

xfail 表示正确行为尚未满足；使用 --runxfail 显示实际断言及状态证据。
"""

import importlib
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_backend import insert_chunks, runtime, tmp_path


def seed_numbered_history(runtime, size):
    chat_id = "audit_boundary_history"
    runtime.history.ensure_session(chat_id, "Synthetic boundary test")
    for i in range(size):
        runtime.history.add_message(chat_id, "user" if i % 2 == 0 else "assistant", f"message-{i:04d}")
    assert runtime.history.count_messages(chat_id) == size
    return chat_id


@pytest.mark.parametrize("size", [20, 200])
def test_history_latest_message_control(runtime, size):
    chat_id = seed_numbered_history(runtime, size)
    response = runtime.api.get(f"/v1/sessions/{chat_id}/messages")
    assert response.status_code == 200
    assert response.json()["messages"][-1]["content"] == f"message-{size - 1:04d}"


@pytest.mark.parametrize("size", [201, 260, 520])
def test_history_latest_message_survives_reopen(runtime, size):
    chat_id = seed_numbered_history(runtime, size)
    response = runtime.api.get(f"/v1/sessions/{chat_id}/messages")
    assert response.status_code == 200
    data = response.json()
    actual = data["messages"][-1]["content"]
    assert actual == f"message-{size - 1:04d}", {"stored": size, "returned": data["count"], "last": actual}


@pytest.mark.parametrize("size", [20, 260])
def test_summary_reopen_keeps_unsummarized_tail(runtime, size):
    chat_id = seed_numbered_history(runtime, size)
    runtime.history.set_summary(chat_id, "Synthetic summary", size - 6)
    data = runtime.api.get(f"/v1/sessions/{chat_id}/messages").json()
    # Same indexing contract used by resumeSession in sidepanel.js.
    tail = data["messages"][data["summary_msg_count"]:]
    assert [message["content"] for message in tail] == [f"message-{i:04d}" for i in range(size - 6, size)], {
        "stored": size, "returned": data["count"], "summary_cursor": data["summary_msg_count"], "tail_count": len(tail),
    }


def test_cross_kb_doc_status_is_hidden_control(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    other = runtime.kb.create_kb("Other synthetic KB")["kb_id"]
    assert other != kb_id
    result = runtime.api.get(f"/v1/kb/{other}/docs/{doc_id}/status")
    assert result.json()["status"] == "not_found"
    assert runtime.store.get_doc(doc_id)["deleted_at"] == ""


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F11: delete ignores document's owning KB in SQLite")
@pytest.mark.parametrize("existing_parent", [True, False])
def test_cross_kb_delete_cannot_modify_other_doc(runtime, existing_parent):
    kb_id, doc_id = insert_chunks(runtime)
    wrong_parent = runtime.kb.create_kb("Other synthetic KB")["kb_id"] if existing_parent else "kb_nonexistent"
    response = runtime.api.delete(f"/v1/kb/{wrong_parent}/docs/{doc_id}")
    doc = runtime.store.get_doc(doc_id)
    active = runtime.vector.scroll_memories(user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id, limit=20)
    runtime.monkeypatch.setattr(runtime.kb, "embed_query", lambda *args: [1.0, 0.0])
    hits = runtime.kb.search_kb(kb_id, "part")
    assert not doc["deleted_at"] and response.status_code in (400, 404), {
        "http_status": response.status_code, "other_doc_deleted": bool(doc["deleted_at"]), "other_doc_active_chunks": len(active),
        "search_hits": len(hits),
    }


def test_kb_restore_roundtrip_control(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    assert runtime.api.delete(f"/v1/kb/{kb_id}").status_code == 200
    assert runtime.api.post(f"/v1/kb/{kb_id}/restore").status_code == 200
    assert not runtime.store.get_kb(kb_id)["deleted_at"]
    assert not runtime.store.get_doc(doc_id)["deleted_at"]
    assert len(runtime.vector.scroll_memories(user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id, limit=20)) == 3


def test_kb_restore_preserves_independently_deleted_doc_control(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    assert runtime.api.delete(f"/v1/kb/{kb_id}/docs/{doc_id}").status_code == 200
    assert runtime.api.delete(f"/v1/kb/{kb_id}").status_code == 200
    assert runtime.api.post(f"/v1/kb/{kb_id}/restore").status_code == 200
    assert runtime.store.get_doc(doc_id)["deleted_at"]
    assert not runtime.vector.scroll_memories(user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id, limit=20)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F12: failed vector restore commits metadata and cannot be retried")
def test_restore_after_transient_vector_failure_can_retry(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    assert runtime.api.delete(f"/v1/kb/{kb_id}").status_code == 200
    client = runtime.vector.get_client()
    original = client.set_payload
    restore_attempts = []

    def fail_once(*args, **kwargs):
        if kwargs["payload"].get("valid"):
            restore_attempts.append(True)
            if len(restore_attempts) == 1:
                raise RuntimeError("Synthetic temporary Qdrant outage")
        return original(*args, **kwargs)

    runtime.monkeypatch.setattr(client, "set_payload", fail_once)
    with TestClient(runtime.api.app, raise_server_exceptions=False) as api:
        first = api.post(f"/v1/kb/{kb_id}/restore")
        second = api.post(f"/v1/kb/{kb_id}/restore")
    active = runtime.vector.scroll_memories(user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id, limit=20)
    assert first.status_code == 500
    assert second.status_code == 200 and len(active) == 3, {
        "first_status": first.status_code, "retry_status": second.status_code,
        "kb_active": not runtime.store.get_kb(kb_id)["deleted_at"],
        "doc_active": not runtime.store.get_doc(doc_id)["deleted_at"],
        "active_chunks": len(active), "vector_restore_attempts": len(restore_attempts),
    }


@pytest.mark.parametrize("chunk_count,fail_batch", [
    (64, None), (65, None), (65, 1),
    pytest.param(65, 2, marks=pytest.mark.xfail(strict=True, raises=AssertionError,
        reason="F13: failed later batch leaves searchable fragments of failed document")),
])
def test_upload_batch_failure_leaves_no_active_partial_doc(runtime, chunk_count, fail_batch):
    kb_id = runtime.kb.create_kb("Synthetic upload boundary KB")["kb_id"]
    finished = threading.Event()
    update_status = runtime.store.update_doc_status

    def finished_status(*args, **kwargs):
        update_status(*args, **kwargs)
        finished.set()

    runtime.monkeypatch.setattr(runtime.store, "update_doc_status", finished_status)
    runtime.monkeypatch.setattr(runtime.kb.chunker, "chunk_document", lambda text, *args: [
        {"text": f"synthetic part {i}", "chunk_id": i} for i in range(chunk_count)
    ])
    runtime.monkeypatch.setattr(runtime.kb, "embed_texts", lambda texts, **kwargs: [[1.0, 0.0] for _ in texts])
    client = runtime.vector.get_client()
    original = client.upsert
    batch_sizes = []

    def upsert(*args, **kwargs):
        batch_sizes.append(len(kwargs["points"]))
        if len(batch_sizes) == fail_batch:
            raise RuntimeError("Synthetic batch write failure")
        return original(*args, **kwargs)

    runtime.monkeypatch.setattr(client, "upsert", upsert)
    response = runtime.api.post(f"/v1/kb/{kb_id}/docs", files={"file": ("audit-batch.txt", b"Synthetic boundary document", "text/plain")})
    assert response.status_code == 200
    assert finished.wait(10), "Document worker did not complete"
    doc_id = response.json()["doc_id"]
    doc = runtime.store.get_doc(doc_id)
    active = runtime.vector.scroll_memories(user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id, limit=100)
    if fail_batch is None:
        assert doc["status"] == "indexed"
        assert doc["chunk_count"] == len(active) == chunk_count
    else:
        assert doc["status"] == "failed"
        runtime.monkeypatch.setattr(runtime.kb, "embed_query", lambda *args: [1.0, 0.0])
        hits = runtime.kb.search_kb(kb_id, "synthetic part")
        assert not active, {"status": doc["status"], "metadata_chunk_count": doc["chunk_count"],
                            "active_chunks": len(active), "search_hits": len(hits), "attempted_batch_sizes": batch_sizes}


@pytest.mark.parametrize("filtered", [True, pytest.param(False, marks=pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="F14: unfiltered log query slices deque and returns HTTP 500"))])
def test_log_query_works_without_event_filter(runtime, filtered):
    logs = importlib.import_module("observability.logger")
    logger = logs.StructuredLogger("audit_boundary")
    runtime.monkeypatch.setattr(logs, "_loggers", {"audit_boundary": logger})
    logger.info("synthetic_event")
    app = FastAPI()
    app.include_router(importlib.import_module("api.logs").router)
    with TestClient(app, raise_server_exceptions=False) as api:
        response = api.get("/v1/logs/query", params={"channel": "audit_boundary", "limit": 1,
                                                   **({"event": "synthetic_event"} if filtered else {})})
    assert response.status_code == 200, {"filtered": filtered, "http_status": response.status_code}
    assert response.json()[0]["event"] == "synthetic_event"
