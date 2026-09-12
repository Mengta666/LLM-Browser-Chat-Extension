"""离线审计回归：真实业务模块 + 临时 SQLite + 内存 Qdrant。

不加载真实 .env，不连接线上服务。xfail 是正确行为断言的已知失败，
用 --runxfail 可查看原始失败；正常对照场景必须通过。
"""

import importlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAI
from qdrant_client import QdrantClient


BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_path():
    # Windows 沙箱不能重开 pytest 创建的 0700 目录；保留独立审计数据便于复查。
    root = Path(os.environ.get("AUDIT_TEST_TEMP", tempfile.gettempdir()))
    path = root / f"browser-agent-audit-{uuid4().hex}"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(BACKEND))
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for key, value in {
        "OPENAI_API_KEY": "offline-test-placeholder",
        "MODEL_BASE_URL": "https://offline.invalid/v1",
        "EMBEDDING_API_KEY": "offline-test-placeholder",
        "EMBEDDING_BASE_URL": "https://offline.invalid/v1",
        "EMBEDDING_MODEL": "offline-test",
        "MEMORY_VECTOR_SIZE": "2",
        "QDRANT_MEMORY_COLLECTION": "audit_only",
        "SEARCH_ENABLED": "0",
        "MEMORY_RETHINK_DAEMON_ENABLED": "0",
    }.items():
        monkeypatch.setenv(key, value)

    def deny_network(*args, **kwargs):
        raise AssertionError("Audit tests must not access the network")

    original_socket_connect = socket.socket.connect

    def guarded_connect(sock, address):
        caller = sys._getframe(1)
        if caller.f_globals.get("__name__") == "socket" and caller.f_code.co_name == "_fallback_socketpair":
            return original_socket_connect(sock, address)
        return deny_network()

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    original_connect = sqlite3.connect

    def isolated_connect(database, *args, **kwargs):
        if str(database) != ":memory:":
            assert Path(database).resolve().is_relative_to(tmp_path.resolve())
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", isolated_connect)
    config = importlib.import_module("agent.memory.config")
    monkeypatch.setattr(config, "MEMORY_DB_PATH", tmp_path / "kb.sqlite3")
    monkeypatch.setattr(config, "KB_RECALL_MIN_SCORE", 0.5)
    monkeypatch.setattr(config, "KB_RERANK_ENABLED", False)
    monkeypatch.setattr(config, "CHAT_COMPACT_KEEP_PAIRS", 3)

    logger = importlib.import_module("observability.logger")
    monkeypatch.setattr(logger.StructuredLogger, "_write_file", lambda *a: None)
    store = importlib.import_module("storage.kb_store")
    monkeypatch.setattr(store, "MEMORY_DB_PATH", tmp_path / "kb.sqlite3")
    store._ensure_tables()
    history = importlib.import_module("storage.chat_store")
    monkeypatch.setattr(history, "_DB_PATH", tmp_path / "history.sqlite3")
    monkeypatch.setattr(history, "_conn", None)

    vector = importlib.import_module("agent.memory.vector")
    client = QdrantClient(":memory:")
    monkeypatch.setattr(vector, "_client", client)
    monkeypatch.setattr(vector, "_collection_ready", False)
    monkeypatch.setattr(vector, "MEMORY_VECTOR_SIZE", 2)
    monkeypatch.setattr(vector, "MEMORY_COLLECTION", "audit_only")
    kb = importlib.import_module("rag.kb")
    reranker = importlib.import_module("rag.reranker")
    monkeypatch.setattr(reranker, "KB_RERANK_ENABLED", False)
    chat = importlib.import_module("api.chat")
    compact = importlib.import_module("agent.memory.chat_compact")
    agentic = importlib.import_module("api.agentic")
    tools = importlib.import_module("search.tools")
    monkeypatch.setattr(chat, "_build_memory_system", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_save_history", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_schedule_memory_write", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_SEARCH_ON", False)
    monkeypatch.setattr(compact, "CHAT_COMPACT_KEEP_PAIRS", 3)

    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(importlib.import_module("api.kb").router)
    app.include_router(importlib.import_module("api.sessions").router)
    with TestClient(app) as api:
        yield SimpleNamespace(
            config=config, store=store, history=history, vector=vector,
            kb=kb, chat=chat, compact=compact, agentic=agentic, tools=tools,
            reranker=reranker, api=api, tmp_path=tmp_path, monkeypatch=monkeypatch,
        )
    if history._conn is not None:
        history._conn.close()
    client.close()


def set_model(runtime, handler):
    client = OpenAI(
        api_key="offline-test-placeholder", base_url="https://offline.invalid/v1",
        max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    runtime.monkeypatch.setattr(runtime.chat, "_llm_client", client)
    runtime.monkeypatch.setattr(runtime.agentic, "_llm_client", client)
    return client


def completion(content="ok", arguments=None):
    message = {"role": "assistant", "content": content}
    if arguments is not None:
        message["tool_calls"] = [{
            "id": "audit_call", "type": "function",
            "function": {"name": "kb_search", "arguments": arguments},
        }]
    return httpx.Response(200, json={
        "id": "audit", "object": "chat.completion", "created": 0,
        "model": "offline-test", "choices": [{
            "index": 0, "message": message,
            "finish_reason": "tool_calls" if arguments is not None else "stop",
        }],
    })


def insert_chunks(runtime, count=3, vector=None):
    kb_id = runtime.kb.create_kb("Audit synthetic KB")["kb_id"]
    doc_id = "audit_doc"
    runtime.store.create_doc(doc_id, kb_id, "audit.txt", "txt", 10, "2026-01-01")
    runtime.vector.batch_insert_memories([
        {
            "content": f"part-{i}", "vector": vector or [1.0, 0.0],
            "memory_type": "kb_chunk", "user_id": runtime.config.CHAT_USER_ID,
            "kb_id": kb_id, "doc_id": doc_id, "source": "audit.txt",
            "chunk_idx": i, "chunk_id": i,
            "prev_chunk_id": i - 1 if i else None,
            "next_chunk_id": i + 1 if i + 1 < count else None,
        } for i in range(count)
    ])
    return kb_id, doc_id


def test_vector_roundtrip_control(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    rows = runtime.vector.scroll_memories(
        user_id=runtime.config.CHAT_USER_ID, memory_type="kb_chunk",
        kb_id=kb_id, doc_id=doc_id, limit=20,
    )
    assert sorted(r["chunk_id"] for r in rows) == [0, 1, 2]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F4: sibling lookup omits query_vector")
def test_existing_neighbor_is_returned(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    neighbor = runtime.kb._get_sibling_chunk(kb_id, doc_id, 1)
    assert neighbor is not None, "Chunk 1 exists in actual Qdrant but sibling lookup returns None"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F4: search window misses existing neighbors")
def test_search_expands_existing_neighbor(runtime):
    kb_id, _ = insert_chunks(runtime)
    runtime.monkeypatch.setattr(runtime.kb, "embed_query", lambda *a: [1.0, 0.0])
    results = runtime.kb.search_kb(kb_id, "?", top_k=1)
    assert results
    assert results[0]["window_size"] >= 2, results[0]


def test_delete_indexed_doc_control(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    response = runtime.api.delete(f"/v1/kb/{kb_id}/docs/{doc_id}")
    assert response.status_code == 200
    assert runtime.store.get_doc(doc_id)["deleted_at"]
    assert not runtime.vector.scroll_memories(
        user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id, limit=20,
    )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F2: pending deletion races with index upsert")
def test_delete_during_embedding_leaves_no_active_chunks(runtime):
    kb_id = runtime.kb.create_kb("Audit race KB")["kb_id"]
    entered = threading.Event()
    resume = threading.Event()
    finished = threading.Event()
    doc_ids = []
    original_update = runtime.store.update_doc_status

    def update_status(doc_id, *args, **kwargs):
        original_update(doc_id, *args, **kwargs)
        finished.set()

    def controlled_embedding(texts, **kwargs):
        entered.set()
        assert resume.wait(10), "Test did not release the embedding barrier"
        return [[1.0, 0.0] for _ in texts]

    # The splitter package is optional in this checkout; keep that boundary deterministic.
    runtime.monkeypatch.setattr(runtime.kb.chunker, "chunk_document", lambda text, *a: [{"text": text, "chunk_id": 0}])
    runtime.monkeypatch.setattr(runtime.kb, "embed_texts", controlled_embedding)
    runtime.monkeypatch.setattr(runtime.store, "update_doc_status", update_status)
    try:
        response = runtime.api.post(f"/v1/kb/{kb_id}/docs", files={"file": ("audit.txt", b"synthetic audit text", "text/plain")})
        assert response.status_code == 200, response.text
        doc_id = response.json()["doc_id"]
        doc_ids.append(doc_id)
        assert entered.wait(10)
        assert runtime.api.delete(f"/v1/kb/{kb_id}/docs/{doc_id}").status_code == 200
    finally:
        resume.set()
        if doc_ids:
            assert finished.wait(10), "Indexing thread did not finish"
    assert runtime.store.get_doc(doc_id)["deleted_at"]
    assert runtime.api.get(f"/v1/kb/{kb_id}/docs").json() == []
    active = runtime.vector.scroll_memories(
        user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id, limit=20,
    )
    assert active == [], f"Deleted document still has {len(active)} active Qdrant chunks"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F5a: zero rerank score falls back to RRF")
def test_zero_rerank_score_is_filtered(runtime):
    kb_id, _ = insert_chunks(runtime, count=1)
    runtime.monkeypatch.setattr(runtime.kb, "embed_query", lambda *a: [1.0, 0.0])
    runtime.monkeypatch.setattr(runtime.reranker, "KB_RERANK_ENABLED", True)
    runtime.monkeypatch.setattr(runtime.reranker, "KB_RERANK_API_URL", "https://offline.invalid/rerank")
    runtime.monkeypatch.setattr(runtime.reranker, "KB_RERANK_API_KEY", "offline-test-placeholder")
    runtime.monkeypatch.setattr(runtime.reranker.requests, "post", lambda *a, **k: httpx.Response(
        200, request=httpx.Request("POST", "https://offline.invalid/rerank"),
        json={"results": [{"index": 0, "relevance_score": 0.0}]},
    ))
    assert runtime.kb.search_kb(kb_id, "?") == []


def test_rrf_rank_is_not_cosine_measurement(runtime):
    kb_id, _ = insert_chunks(runtime, count=1, vector=[0.0, 1.0])
    runtime.monkeypatch.setattr(runtime.kb, "embed_query", lambda *a: [1.0, 0.0])
    raw = runtime.vector.get_client().query_points(
        collection_name="audit_only", query=[1.0, 0.0], using="dense", limit=1,
    ).points
    assert raw[0].score == 0.0
    results = runtime.kb.search_kb(kb_id, "?")
    assert len(results) == 1
    assert results[0]["score"] == 0.5


@pytest.mark.parametrize("stream", [False, True])
def test_valid_tool_arguments_control(runtime, stream):
    calls = []

    def model(request):
        calls.append(json.loads(request.content))
        return completion("", '{"kb_id":"audit_kb","query":"test"}') if len(calls) == 1 else completion("recovered-answer")

    runtime.monkeypatch.setattr(runtime.tools, "handle_kb_search", lambda *a, **k: ("synthetic result", []))
    with set_model(runtime, model):
        response = runtime.api.post("/v1/chat/completions", json={
            "model": "offline-test", "messages": [{"role": "user", "content": "test"}],
            "kb_id": "audit_kb", "stream": stream,
        })
    assert response.status_code == 200
    assert "recovered-answer" in response.text
    assert len(calls) == 2


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F6: malformed arguments abort chat instead of feeding back tool error")
@pytest.mark.parametrize("stream", [False, True])
def test_malformed_tool_arguments_can_recover(runtime, stream):
    calls = []

    def model(request):
        calls.append(json.loads(request.content))
        return completion("", '{"query":') if len(calls) == 1 else completion("recovered-answer")

    with set_model(runtime, model):
        response = runtime.api.post("/v1/chat/completions", json={
            "model": "offline-test", "messages": [{"role": "user", "content": "test"}],
            "kb_id": "audit_kb", "stream": stream,
        })
    assert "recovered-answer" in response.text, response.text


def seed_history(runtime, chat_id, size=20):
    runtime.history.ensure_session(chat_id, "synthetic audit")
    messages = []
    for i in range(size):
        message = {"role": "user" if i % 2 == 0 else "assistant", "content": f"audit-{i}: " + "detail " * 120}
        runtime.history.add_message(chat_id, **message)
        messages.append(message)
    return messages


def set_summary_model(runtime):
    runtime.monkeypatch.setattr(runtime.compact, "_summarize_llm", lambda *a: "Synthetic compacted summary")


def test_compact_cursor_and_no_evicted_control(runtime):
    messages = seed_history(runtime, "audit_compact")
    set_summary_model(runtime)
    first = runtime.compact.compact_chat("audit_compact", force=True)
    second = runtime.compact.compact_chat("audit_compact", force=True)
    assert first["compacted"]
    assert runtime.history.get_summary("audit_compact")["msg_count"] == len(messages) - 6
    assert second == {"compacted": False, "reason": "no_evicted"}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F1: already summarized original messages are forwarded again")
@pytest.mark.parametrize("history_size", [20, 60])
def test_existing_summary_replaces_original_prefix_in_request(runtime, history_size):
    messages = seed_history(runtime, "audit_compact", history_size)
    set_summary_model(runtime)
    assert runtime.compact.compact_chat("audit_compact", force=True)["compacted"]
    messages.append({"role": "user", "content": "continue"})
    context_length = int(runtime.compact.estimate_tokens(messages)["total"] / 0.95)
    runtime.monkeypatch.setattr(runtime.compact, "CHAT_CONTEXT_LENGTH", context_length)
    captures = []

    def model(request):
        captures.append(json.loads(request.content))
        return completion()

    with set_model(runtime, model):
        response = runtime.api.post("/v1/chat/completions", json={
            "model": "offline-test", "chat_id": "audit_compact", "messages": messages, "stream": False,
        })
    assert response.status_code == 200
    sent = [m for m in captures[0]["messages"] if m["role"] != "system"]
    assert len(sent) <= 7, f"Summary cursor={history_size - 6}, but forwarded {len(sent)} original messages"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F7: synchronous compaction rebuild drops KB binding and base prompt")
def test_sync_compact_keeps_kb_binding_and_base_prompt(runtime):
    messages = seed_history(runtime, "audit_kb_compact")
    set_summary_model(runtime)
    kb_id = runtime.kb.create_kb("Audit bound KB")["kb_id"]
    messages.append({"role": "user", "content": "continue"})
    context_length = int(runtime.compact.estimate_tokens(messages)["total"] / 0.95)
    runtime.monkeypatch.setattr(runtime.compact, "CHAT_CONTEXT_LENGTH", context_length)
    captures = []

    def model(request):
        captures.append(json.loads(request.content))
        return completion()

    with set_model(runtime, model):
        response = runtime.api.post("/v1/chat/completions", json={
            "model": "offline-test", "chat_id": "audit_kb_compact", "messages": messages,
            "kb_id": kb_id, "stream": False,
        })
    assert response.status_code == 200
    assert runtime.history.get_summary("audit_kb_compact")["msg_count"] == 14
    sent = captures[0]["messages"]
    missing = []
    if kb_id not in json.dumps(sent):
        missing.append("bound KB ID")
    if runtime.chat._CHAT_BASE_SYSTEM not in sent[0]["content"]:
        missing.append("base prompt")
    assert not missing, f"The real outgoing model request lost: {missing}"


def test_short_chat_keeps_kb_binding_control(runtime):
    kb_id = runtime.kb.create_kb("Audit short KB")["kb_id"]
    item = runtime.chat.ChatRequest(chat_id="audit_short", kb_id=kb_id, messages=[{"role": "user", "content": "test"}])
    prepared = runtime.chat._prepare_messages(item)
    assert kb_id in prepared[0]["content"]
    assert runtime.chat._CHAT_BASE_SYSTEM in prepared[0]["content"]


def test_reopened_session_summary_tail_control(runtime):
    messages = seed_history(runtime, "audit_reopened")
    set_summary_model(runtime)
    assert runtime.compact.compact_chat("audit_reopened", force=True)["compacted"]
    response = runtime.api.get("/v1/sessions/audit_reopened/messages")
    assert response.status_code == 200
    data = response.json()
    # 同 sidepanel.resumeSession 的 summary + messages.slice(summary_msg_count)。
    resumed = [{"role": "system", "content": data["summary"]}]
    resumed.extend({"role": m["role"], "content": m["content"]} for m in data["messages"][data["summary_msg_count"]:])
    resumed.append({"role": "user", "content": "continue"})
    context_length = int(runtime.compact.estimate_tokens(messages)["total"] / 0.95)
    runtime.monkeypatch.setattr(runtime.compact, "CHAT_CONTEXT_LENGTH", context_length)
    prepared = runtime.chat._prepare_messages(runtime.chat.ChatRequest(
        chat_id="audit_reopened", messages=resumed,
    ))
    assert len([m for m in prepared if m["role"] != "system"]) == 7
    assert runtime.compact.estimate_tokens(prepared)["total"] < context_length * 0.9


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F9: incoming user message shifts tail and drops an unsummarized message")
def test_sync_compact_does_not_drop_unsummarized_boundary_message(runtime):
    messages = seed_history(runtime, "audit_boundary")
    summary_inputs = []

    def summarize(system, user):
        summary_inputs.append(user)
        return "Synthetic summary"

    runtime.monkeypatch.setattr(runtime.compact, "_summarize_llm", summarize)
    messages.append({"role": "user", "content": "continue"})
    context_length = int(runtime.compact.estimate_tokens(messages)["total"] / 0.95)
    runtime.monkeypatch.setattr(runtime.compact, "CHAT_CONTEXT_LENGTH", context_length)
    prepared = runtime.chat._prepare_messages(runtime.chat.ChatRequest(
        chat_id="audit_boundary", messages=messages,
    ))
    assert runtime.history.get_summary("audit_boundary")["msg_count"] == 14
    covered_text = "\n".join(summary_inputs) + json.dumps(prepared)
    lost = [f"audit-{i}: " for i in range(20) if f"audit-{i}: " not in covered_text]
    assert lost == [], f"Neither summarized nor forwarded: {lost}"
