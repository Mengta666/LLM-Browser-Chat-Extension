"""知识库生命周期故障注入：隔离 SQLite、内存 Qdrant，无真实服务调用。"""

import importlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_backend import runtime, tmp_path, insert_chunks


def seed(runtime, *, kb_id=None, count=3, published=True, run_id=None, valid=True, owner=None):
    owner = owner or runtime.config.CHAT_USER_ID
    if kb_id is None:
        kb_id = "kb_" + uuid4().hex
        runtime.store.create_kb(kb_id, owner, "synthetic", "", "2026-01-01")
    doc_id = "doc_" + uuid4().hex
    run_id = uuid4().hex if run_id is None else run_id
    runtime.store.create_doc(doc_id, kb_id, "test.txt", "txt", 1, "2026-01-01",
                             index_run_id=run_id, user_id=owner)
    items = [{"content": f"part-{i}", "vector": [1.0, 0.0], "memory_type": "kb_chunk",
              "user_id": owner, "kb_id": kb_id, "doc_id": doc_id, "index_run_id": run_id,
              "chunk_idx": i, "chunk_id": i, "prev_chunk_id": i-1 if i else None,
              "next_chunk_id": i+1 if i+1<count else None, "valid": valid} for i in range(count)]
    runtime.vector.batch_insert_memories(items)
    if published:
        assert runtime.store.publish_index(kb_id, doc_id, run_id, count, "now", owner)
    return kb_id, doc_id, run_id, items


def hits(runtime, kb_id):
    runtime.monkeypatch.setattr(runtime.kb, "embed_query", lambda *a: [1.0, 0.0])
    return runtime.kb.search_kb(kb_id, "synthetic", top_k=5)


def active(runtime, kb_id, doc_id=None):
    return runtime.vector.scroll_memories(user_id=runtime.config.CHAT_USER_ID,
                                         memory_type="kb_chunk", kb_id=kb_id, doc_id=doc_id, limit=500)


def prepare_process(runtime, count=3):
    kb_id = runtime.kb.create_kb("synthetic upload")["kb_id"]
    doc_id, run_id = "doc_"+uuid4().hex, uuid4().hex
    runtime.store.create_doc(doc_id, kb_id, "test.txt", "txt", 4, "now", index_run_id=run_id)
    path = runtime.tmp_path / f"{doc_id}.txt"
    path.write_text("synthetic", encoding="utf-8")
    runtime.monkeypatch.setattr(runtime.kb.chunker, "chunk_document",
                               lambda *a: [{"text": f"part-{i}"} for i in range(count)])
    runtime.monkeypatch.setattr(runtime.kb, "embed_texts", lambda texts, **kw: [[1.0, 0.0] for _ in texts])
    return kb_id, doc_id, run_id, path


def test_vectors_not_visible_until_metadata_publish(runtime):
    kb_id, doc_id, run_id, path = prepare_process(runtime)
    original = runtime.store.publish_index
    observed = []

    def publish(*args, **kwargs):
        observed.append(len(active(runtime, kb_id)))
        assert runtime.store.get_doc(doc_id)["status"] == "pending"
        assert hits(runtime, kb_id) == []
        return original(*args, **kwargs)

    runtime.monkeypatch.setattr(runtime.store, "publish_index", publish)
    runtime.kb._process_doc(kb_id, doc_id, path, "txt", run_id)
    assert observed == [3] and hits(runtime, kb_id)
    assert not path.exists()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_publish_failure_never_exposes_activated_vectors(runtime, cleanup_fails):
    kb_id, doc_id, run_id, path = prepare_process(runtime)
    client = runtime.vector.get_client()
    set_payload = client.set_payload

    def publish(*a, **kw):
        raise sqlite3.OperationalError("synthetic commit failure")

    def payload(*a, **kw):
        if cleanup_fails and not kw["payload"]["valid"]:
            raise RuntimeError("synthetic cleanup outage")
        return set_payload(*a, **kw)

    runtime.monkeypatch.setattr(runtime.store, "publish_index", publish)
    runtime.monkeypatch.setattr(client, "set_payload", payload)
    runtime.kb._process_doc(kb_id, doc_id, path, "txt", run_id)
    doc = runtime.store.get_doc(doc_id)
    assert doc["status"] == "failed" and not doc["published_run_id"]
    assert bool(doc["sync_pending"]) == cleanup_fails
    assert hits(runtime, kb_id) == []
    runtime.monkeypatch.setattr(client, "set_payload", set_payload)
    runtime.kb.reconcile_pending()
    assert not active(runtime, kb_id)
    assert not runtime.store.get_doc(doc_id)["sync_pending"]


def test_staging_retry_reuses_point_ids(runtime):
    kb_id, doc_id, run_id, items = seed(runtime, published=False, valid=False)
    runtime.vector.batch_insert_memories(items)
    assert runtime.vector.kb_index_complete(user_id=runtime.config.CHAT_USER_ID, kb_id=kb_id,
                                          doc_id=doc_id, index_run_id=run_id, expected=3)
    assert not active(runtime, kb_id)
    assert hits(runtime, kb_id) == []


def test_activation_applied_but_ack_lost_is_not_published(runtime):
    kb_id, doc_id, run_id, path = prepare_process(runtime)
    client = runtime.vector.get_client()
    original = client.set_payload

    def lose_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs['payload']['valid']:
            assert active(runtime, kb_id)
            raise TimeoutError('synthetic acknowledgement lost')
        return result

    runtime.monkeypatch.setattr(client, 'set_payload', lose_ack)
    runtime.kb._process_doc(kb_id, doc_id, path, 'txt', run_id)
    assert runtime.store.get_doc(doc_id)['status'] == 'failed'
    assert not active(runtime, kb_id) and hits(runtime, kb_id) == []


def test_stale_worker_cannot_publish_or_fail_new_run(runtime):
    kb_id, doc_id, run_id, _ = seed(runtime, published=False, valid=False)
    with runtime.store._transaction() as conn:
        conn.execute('UPDATE kb_docs SET index_run_id=? WHERE doc_id=?', ('new-run', doc_id))
    assert not runtime.store.publish_index(kb_id, doc_id, run_id, 3, 'now', runtime.config.CHAT_USER_ID)
    runtime.store.fail_index(kb_id, doc_id, run_id, 'old failure')
    runtime.store.set_doc_sync(doc_id, run_id, False)
    doc = runtime.store.get_doc(doc_id)
    assert doc['status'] == 'pending' and doc['sync_pending'] and not doc['error_msg']


@pytest.mark.parametrize('failure', ['vector', 'sqlite'])
def test_purge_failure_keeps_retryable_intent(runtime, failure):
    kb_id, _, _, _ = seed(runtime)
    runtime.kb.delete_kb(kb_id)
    target, name = ((runtime.vector, 'purge_kb_chunks') if failure == 'vector'
                    else (runtime.store, 'finish_purge'))
    original = getattr(target, name)

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic purge failure')

    runtime.monkeypatch.setattr(target, name, fail)
    assert runtime.api.delete(f'/v1/kb/{kb_id}/hard').status_code == 503
    assert runtime.store.get_kb(kb_id)['sync_action'] == 'purge'
    assert hits(runtime, kb_id) == []
    assert runtime.api.post(f'/v1/kb/{kb_id}/restore').status_code == 409
    runtime.monkeypatch.setattr(target, name, original)
    runtime.kb.reconcile_pending()
    assert runtime.store.get_kb(kb_id) is None
    assert runtime.vector.scroll_memories(user_id=runtime.config.CHAT_USER_ID,
        memory_type='kb_chunk', kb_id=kb_id, include_invalid=True) == []


def test_legacy_missing_version_is_supported_but_incomplete_index_is_hidden(runtime):
    kb_id, doc_id, _, _ = seed(runtime, run_id='')
    client = runtime.vector.get_client()
    client.delete_payload(collection_name=runtime.config.MEMORY_COLLECTION,
        keys=['index_run_id'], points=runtime.vector._kb_index_filter(runtime.config.CHAT_USER_ID, kb_id))
    assert hits(runtime, kb_id)
    runtime.kb.delete_kb(kb_id)
    assert runtime.api.post(f'/v1/kb/{kb_id}/restore').status_code == 200
    with runtime.store._transaction() as conn:
        conn.execute('UPDATE kb_docs SET chunk_count=4,sync_pending=1 WHERE doc_id=?', (doc_id,))
    runtime.kb.reconcile_pending()
    assert runtime.store.get_doc(doc_id)['sync_pending']
    assert hits(runtime, kb_id) == []


def test_delete_sqlite_transaction_rolls_back_both_tables(runtime):
    kb_id, doc_id, _, _ = seed(runtime)
    with runtime.store._transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_delete BEFORE UPDATE OF deleted_at ON kb_kbs
                        WHEN NEW.deleted_at!='' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        runtime.kb.delete_kb(kb_id)
    assert not runtime.store.get_kb(kb_id)['deleted_at']
    assert not runtime.store.get_doc(doc_id)['deleted_at']
    assert hits(runtime, kb_id)


@pytest.mark.parametrize("target", ["doc", "kb"])
def test_delete_during_late_batch_then_restore_does_not_publish(runtime, target):
    kb_id = runtime.kb.create_kb("race")["kb_id"]
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    process = runtime.kb._process_doc
    client = runtime.vector.get_client()
    upsert = client.upsert
    runtime.monkeypatch.setattr(runtime.kb.chunker, "chunk_document", lambda *a: [{"text":"part"}])
    runtime.monkeypatch.setattr(runtime.kb, "embed_texts", lambda *a, **k: [[1.0, 0.0]])

    def delayed(*a, **kw):
        entered.set()
        assert release.wait(10)
        return upsert(*a, **kw)

    def worker(*a, **kw):
        try:
            return process(*a, **kw)
        finally:
            finished.set()

    runtime.monkeypatch.setattr(client, "upsert", delayed)
    runtime.monkeypatch.setattr(runtime.kb, "_process_doc", worker)
    response = runtime.api.post(f"/v1/kb/{kb_id}/docs", files={"file":("race.txt",b"synthetic","text/plain")})
    assert response.status_code == 200
    doc_id = response.json()["doc_id"]
    try:
        assert entered.wait(10)
        path = f"/v1/kb/{kb_id}" + (f"/docs/{doc_id}" if target == "doc" else "")
        deleted = runtime.api.delete(path)
        assert deleted.status_code == 200 and deleted.json()["sync_pending"]
        if target == "kb":
            assert runtime.api.delete(f"/v1/kb/{kb_id}/hard").status_code == 409
            assert runtime.api.post(f"/v1/kb/{kb_id}/restore").status_code == 200
    finally:
        release.set()
        assert finished.wait(10)
    assert runtime.store.get_doc(doc_id)["status"] == "failed"
    assert not active(runtime, kb_id) and hits(runtime, kb_id) == []


@pytest.mark.parametrize("target", ["doc", "kb"])
def test_delete_outage_hides_content_and_reconciles(runtime, target):
    kb_id, doc_id, _, _ = seed(runtime)
    client = runtime.vector.get_client()
    original = client.set_payload

    def fail(*a, **k):
        raise RuntimeError("synthetic outage")

    runtime.monkeypatch.setattr(client, "set_payload", fail)
    result = (runtime.kb.delete_doc(kb_id,doc_id) if target == "doc" else runtime.kb.delete_kb(kb_id))
    assert result["sync_pending"] and active(runtime, kb_id)
    assert hits(runtime, kb_id) == []
    runtime.monkeypatch.setattr(client, "set_payload", original)
    runtime.kb.reconcile_pending()
    assert not active(runtime, kb_id)
    assert not runtime.store.get_doc(doc_id)["sync_pending"]


@pytest.mark.parametrize("failure", ["second_vector", "sqlite"])
def test_restore_failure_is_hidden_and_retryable(runtime, failure):
    kb_id, doc_id, _, _ = seed(runtime)
    _, other_doc, _, _ = seed(runtime, kb_id=kb_id)
    runtime.kb.delete_kb(kb_id)
    client = runtime.vector.get_client()
    original = client.set_payload
    complete = runtime.store.finish_restore
    activations = []

    def payload(*a, **kw):
        if kw["payload"]["valid"]:
            activations.append(True)
            if failure == "second_vector" and len(activations) == 2:
                raise RuntimeError("synthetic restore outage")
        return original(*a, **kw)

    def commit(*a, **kw):
        raise sqlite3.OperationalError("synthetic sqlite failure")

    runtime.monkeypatch.setattr(client, "set_payload", payload)
    if failure == "sqlite":
        runtime.monkeypatch.setattr(runtime.store, "finish_restore", commit)
    response = runtime.api.post(f"/v1/kb/{kb_id}/restore")
    assert response.status_code == 503 and active(runtime, kb_id)
    assert runtime.store.get_kb(kb_id)["deleted_at"]
    assert runtime.store.get_doc(doc_id)["deleted_at"] and runtime.store.get_doc(other_doc)["deleted_at"]
    assert hits(runtime, kb_id) == []
    runtime.monkeypatch.setattr(runtime.store, "finish_restore", complete)
    runtime.monkeypatch.setattr(client, "set_payload", original)
    runtime.kb.reconcile_pending()
    assert len(active(runtime, kb_id)) == 6 and hits(runtime, kb_id)
    assert runtime.api.post(f"/v1/kb/{kb_id}/restore").status_code == 200


def test_pending_restore_cannot_override_new_delete(runtime):
    kb_id, _, _, _ = seed(runtime)
    runtime.kb.delete_kb(kb_id)
    original = runtime.vector.set_kb_chunks_valid

    def failed(*a, **kw):
        raise RuntimeError("synthetic outage")

    runtime.monkeypatch.setattr(runtime.vector, "set_kb_chunks_valid", failed)
    assert runtime.api.post(f"/v1/kb/{kb_id}/restore").status_code == 503
    assert runtime.api.delete(f"/v1/kb/{kb_id}").status_code == 200
    runtime.monkeypatch.setattr(runtime.vector, "set_kb_chunks_valid", original)
    runtime.kb.reconcile_pending()
    assert runtime.store.get_kb(kb_id)["deleted_at"] and not active(runtime,kb_id)


def test_failed_document_is_not_reactivated_by_kb_restore(runtime):
    kb_id, doc_id, run_id, _ = seed(runtime, published=False)
    runtime.store.fail_index(kb_id,doc_id,run_id,"synthetic failure")
    runtime.kb.delete_kb(kb_id)
    runtime.kb.restore_kb(kb_id)
    assert runtime.store.get_doc(doc_id)["status"] == "failed"
    assert not active(runtime,kb_id) and hits(runtime,kb_id) == []


def test_wrong_owner_cannot_read_or_modify(runtime):
    kb_id, doc_id, _, _ = seed(runtime, owner="foreign-synthetic-owner")
    before_kb, before_doc = runtime.store.get_kb(kb_id), runtime.store.get_doc(doc_id)
    for method, suffix in [("GET","/docs"),("DELETE",f"/docs/{doc_id}"),
                           ("DELETE",""),("POST","/restore"),("DELETE","/hard")]:
        assert runtime.api.request(method,f"/v1/kb/{kb_id}{suffix}").status_code == 404
    assert runtime.api.post(f"/v1/kb/{kb_id}/docs",files={"file":("t.txt",b"data","text/plain")}).status_code == 404
    assert runtime.api.get(f"/v1/kb/{kb_id}/docs/{doc_id}/status").json()["status"] == "not_found"
    assert hits(runtime,kb_id) == []
    assert runtime.store.get_kb(kb_id) == before_kb and runtime.store.get_doc(doc_id) == before_doc


def test_stale_version_excluded_before_rerank_and_from_neighbors(runtime):
    kb_id, doc_id, run_id, items = seed(runtime)
    stale = [{**i, "index_run_id":"obsolete", "content":"obsolete"} for i in items]
    runtime.vector.batch_insert_memories(stale)
    calls = []

    def candidates(*a, **kw):
        calls.append(kw["top_k"])
        return [{**item, "score": 1.0} for item in (stale + items)[:kw["top_k"]]]

    runtime.monkeypatch.setattr(runtime.vector,"search_memories",candidates)
    runtime.monkeypatch.setattr(runtime.kb,"embed_query",lambda *a:[1.0,0.0])
    result = runtime.kb.search_kb(kb_id,"synthetic",top_k=1)
    assert calls == [1,3,9]
    assert result and all(r["index_run_id"] == run_id and "obsolete" not in r["content"] for r in result)
    assert runtime.kb._get_sibling_chunk(kb_id,doc_id,1,"obsolete") is None


def test_delete_during_rerank_is_checked_again(runtime):
    kb_id, doc_id, _, _ = seed(runtime)

    def rerank(query, chunks):
        runtime.kb.delete_doc(kb_id,doc_id)
        return chunks

    runtime.monkeypatch.setattr(runtime.reranker,"rerank_chunks",rerank)
    assert hits(runtime,kb_id) == []


def test_core_memory_is_never_changed_by_kb_lifecycle(runtime):
    kb_id, doc_id, _, _ = seed(runtime)
    memory = runtime.vector.insert_memory("synthetic core",vector=[1.0,0.0],memory_type="core",
                                         user_id=runtime.config.CHAT_USER_ID,kb_id=kb_id,doc_id=doc_id)
    for operation in (runtime.kb.delete_kb, runtime.kb.restore_kb, runtime.kb.delete_kb, runtime.kb.hard_delete_kb):
        operation(kb_id)
        assert runtime.vector.get_memory(memory["memory_id"])["valid"]
    assert runtime.store.get_kb(kb_id) is None and not active(runtime,kb_id)


def test_duplicate_creation_is_atomic_and_failed_content_can_be_reuploaded(runtime):
    kb_id = runtime.kb.create_kb("duplicate")["kb_id"]

    def create(_):
        doc_id = uuid4().hex
        try:
            runtime.store.create_doc(doc_id,kb_id,"t.txt","txt",1,"now","same-hash",index_run_id="run")
            return doc_id
        except runtime.store.KBConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(create,range(2)))
    assert sum(doc is not None for doc in created) == 1
    runtime.store.fail_index(kb_id,next(doc for doc in created if doc),"run","failed")
    assert create(0) is not None


def test_delete_kb_and_create_doc_transaction_boundary(runtime):
    kb_id = runtime.kb.create_kb("atomic delete")["kb_id"]
    barrier = threading.Barrier(2)

    def create():
        barrier.wait()
        try:
            runtime.store.create_doc("race-doc",kb_id,"t.txt","txt",1,"now",index_run_id="run")
        except runtime.store.KBNotFound:
            pass

    def delete():
        barrier.wait()
        runtime.store.mark_kb_deleted(kb_id,"now",runtime.config.CHAT_USER_ID)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create),pool.submit(delete)]
        for future in futures:
            future.result(timeout=10)
    doc = runtime.store.get_doc("race-doc")
    assert doc is None or (doc["deleted_at"] and doc["status"] == "failed")


def test_restore_sqlite_transaction_rolls_back_both_tables(runtime):
    kb_id, doc_id, _, _ = seed(runtime)
    runtime.kb.delete_kb(kb_id)
    with runtime.store._transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_restore BEFORE UPDATE OF deleted_at ON kb_docs
                        WHEN NEW.deleted_at='' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
    assert runtime.api.post(f"/v1/kb/{kb_id}/restore").status_code == 503
    assert runtime.store.get_kb(kb_id)["deleted_at"] and runtime.store.get_doc(doc_id)["deleted_at"]
    assert hits(runtime,kb_id) == []
    with runtime.store._transaction() as conn:
        conn.execute("DROP TRIGGER fail_restore")
    runtime.kb.reconcile_pending()
    assert hits(runtime,kb_id)


def test_actual_startup_recovers_pending_index(runtime):
    kb_id, doc_id, _, _ = seed(runtime,published=False)
    reconciled = threading.Event()
    original = runtime.kb.reconcile_pending

    def reconcile():
        try:
            original()
        finally:
            runtime.kb._recovery_stop.set()
            reconciled.set()

    runtime.monkeypatch.setattr(runtime.kb,"reconcile_pending",reconcile)
    app = importlib.import_module("app").app
    with TestClient(app):
        assert reconciled.wait(10)
        assert runtime.store.get_doc(doc_id)["status"] == "failed"
        assert "重新上传" in runtime.store.get_doc(doc_id)["error_msg"]
        assert not active(runtime,kb_id) and hits(runtime,kb_id) == []


def test_legacy_migration_backups_and_checks_before_visibility(runtime):
    kb_id, doc_id = insert_chunks(runtime)
    old_path = runtime.tmp_path / "legacy.sqlite3"
    runtime.monkeypatch.setattr(runtime.store,"MEMORY_DB_PATH",old_path)
    with sqlite3.connect(old_path) as conn:
        conn.execute("""CREATE TABLE kb_kbs(kb_id TEXT PRIMARY KEY,user_id TEXT,name TEXT,description TEXT,
                        created_at TEXT,updated_at TEXT,deleted_at TEXT DEFAULT '')""")
        conn.execute("""CREATE TABLE kb_docs(doc_id TEXT PRIMARY KEY,kb_id TEXT,filename TEXT,file_type TEXT,
                        file_bytes INTEGER,chunk_count INTEGER,status TEXT,error_msg TEXT,created_at TEXT,
                        indexed_at TEXT,deleted_at TEXT DEFAULT '')""")
        conn.execute("INSERT INTO kb_kbs VALUES(?,?,?,'','now','now','')",(kb_id,runtime.config.CHAT_USER_ID,"legacy"))
        conn.execute("INSERT INTO kb_docs VALUES(?,?,'t.txt','txt',1,3,'indexed','','now','now','')",(doc_id,kb_id))
    runtime.store._ensure_tables()
    backups = list(runtime.tmp_path.glob("legacy.kb-lifecycle-*.sqlite3"))
    assert len(backups) == 1 and runtime.store.get_doc(doc_id)["sync_pending"]
    assert hits(runtime,kb_id) == []
    with sqlite3.connect(backups[0]) as conn:
        assert "index_run_id" not in {r[1] for r in conn.execute("PRAGMA table_info(kb_docs)")}
        assert conn.execute("SELECT filename FROM kb_docs").fetchone()[0] == "t.txt"
    runtime.kb.reconcile_pending()
    assert not runtime.store.get_doc(doc_id)["sync_pending"] and hits(runtime,kb_id)
    runtime.store._ensure_tables()
    assert len(list(runtime.tmp_path.glob("legacy.kb-lifecycle-*.sqlite3"))) == 1


def test_migration_failure_does_not_leave_partial_schema(runtime):
    path = runtime.tmp_path / 'failed-migration.sqlite3'
    runtime.monkeypatch.setattr(runtime.store, 'MEMORY_DB_PATH', path)
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE kb_docs(doc_id TEXT PRIMARY KEY,kb_id TEXT,filename TEXT,
            file_type TEXT,file_bytes INTEGER,chunk_count INTEGER,status TEXT,error_msg TEXT,
            created_at TEXT,indexed_at TEXT,deleted_at TEXT DEFAULT '')''')
        conn.execute("INSERT INTO kb_docs VALUES('d','k','t.txt','txt',1,1,'indexed','','now','now','')")
    original = runtime.store._get_conn

    def failing_connection():
        conn = original()
        count = 0

        def authorize(action, *args):
            nonlocal count
            if action == sqlite3.SQLITE_ALTER_TABLE:
                count += 1
                if count == 8:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    runtime.monkeypatch.setattr(runtime.store, '_get_conn', failing_connection)
    with pytest.raises(sqlite3.DatabaseError):
        runtime.store._ensure_tables()
    with sqlite3.connect(path) as conn:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(kb_docs)')}
        assert 'index_run_id' not in columns
        assert conn.execute('SELECT status FROM kb_docs').fetchone()[0] == 'indexed'
    runtime.monkeypatch.setattr(runtime.store, '_get_conn', original)
    runtime.store._ensure_tables()
    assert runtime.store.get_doc('d')['sync_pending']
