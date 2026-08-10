"""RAG 页面刷新快照替换流程的轻量回归测试。"""

import os
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("EMBEDDING_MODEL", "test-embedding")


def test_db_replace_latest_snapshot_for_page() -> None:
    """验证 DB 层替换 latest snapshot 时会同步所有 chat_page 绑定。"""
    from storage import db as db_module

    database = object.__new__(db_module.Database)
    database.conn = sqlite3.connect(":memory:", check_same_thread=False)
    database.conn.row_factory = sqlite3.Row
    database.cursor = database.conn.cursor()
    database.init_db()
    try:
        database.upsert_chat("chat_old")
        database.replace_latest_snapshot_for_page(
            chat_id="chat_old",
            page_id="page_1",
            canonical_url="https://example.com/page",
            title="Old",
            latest_snapshot_id="snap_old",
            content_hash="content_old",
            url="https://example.com/page",
            chunker_version="v1",
            embedding_model="embed",
            page_context_id="ctx_old",
        )
        database.upsert_chat("chat_other")
        database.upsert_chat_page("chat_other", "page_1", "snap_old", "ctx_other")

        replaced_snapshot_ids = database.replace_latest_snapshot_for_page(
            chat_id="chat_current",
            page_id="page_1",
            canonical_url="https://example.com/page",
            title="New",
            latest_snapshot_id="snap_new",
            content_hash="content_new",
            url="https://example.com/page",
            chunker_version="v1",
            embedding_model="embed",
            page_context_id="ctx_new",
        )

        assert replaced_snapshot_ids == ["snap_old"]
        assert database.get_page("page_1")["latest_snapshot_id"] == "snap_new"
        assert database.get_snapshot("snap_old")["is_active"] == 0
        assert database.get_snapshot("snap_new")["is_active"] == 1
        assert database.get_chat_page("chat_old", "page_1")["snapshot_id"] == "snap_new"
        assert database.get_chat_page("chat_other", "page_1")["snapshot_id"] == "snap_new"
        assert database.get_chat_page("chat_current", "page_1")["page_context_id"] == "ctx_new"
    finally:
        database.close()


class FakeVectorStore:
    """模拟 Qdrant collection 的最小行为。"""

    QDRANT_VECTOR_SIZE = 3

    def __init__(self, existing_snapshot_ids: set[str] | None = None, fail_delete: bool = False) -> None:
        """初始化已存在快照集合和删除失败开关。"""
        self.existing_snapshot_ids = set(existing_snapshot_ids or set())
        self.fail_delete = fail_delete
        self.upserted_points: list[dict] = []
        self.deleted_snapshot_ids: list[str] = []
        self.searched_snapshot_ids: list[str] = []

    def ensure_collection(self, vector_size: int | None = None) -> None:
        """测试中无需真正创建 collection。"""
        return None

    def snapshot_exists(self, snapshot_id: str, embedding_model: str, chunker_version: str) -> bool:
        """按内存集合判断快照是否存在。"""
        return snapshot_id in self.existing_snapshot_ids

    def upsert_chunk_points(self, points: list[dict]) -> None:
        """记录写入点，并把对应 snapshot 标记为存在。"""
        self.upserted_points.extend(points)
        for point in points:
            self.existing_snapshot_ids.add(point["payload"]["snapshot_id"])

    def delete_snapshots_data(self, snapshot_ids: list[str]) -> None:
        """记录删除的 snapshot，必要时模拟删除失败。"""
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.deleted_snapshot_ids.extend(snapshot_ids)
        self.existing_snapshot_ids.difference_update(snapshot_ids)

    def search_chunks(
            self,
            query_vector: list[float],
            snapshot_ids: list[str],
            embedding_model: str,
            chunker_version: str,
            top_k: int = 10,
    ) -> list[dict]:
        """记录搜索范围，并返回固定命中。"""
        self.searched_snapshot_ids = list(snapshot_ids)
        return [
            {
                "score": 0.9,
                "payload": {
                    "url": "https://example.com/page",
                    "title": "Page",
                    "content": "new content",
                    "chunk_id": "chunk_new_000000",
                    "snapshot_id": snapshot_ids[0],
                    "page_id": "page_1",
                },
            }
        ]


class FakeDB:
    """模拟 page_retrieval 依赖的 SQLite 方法。"""

    def __init__(self, latest_snapshot_id: str | None = None, snapshot_ids: list[str] | None = None) -> None:
        """初始化 latest snapshot、快照列表和 chat 绑定。"""
        self.latest_snapshot_id = latest_snapshot_id
        self.snapshot_ids = list(snapshot_ids or [])
        self.chat_pages: dict[tuple[str, str], str] = {}
        self.replace_calls: list[dict] = []

    def upsert_chat(self, chat_id: str) -> None:
        """测试中无需真实写 chat。"""
        return None

    def get_latest_snapshot(self, page_id: str) -> dict | None:
        """返回当前逻辑页面的 latest snapshot。"""
        if not self.latest_snapshot_id:
            return None
        return {"snapshot_id": self.latest_snapshot_id}

    def upsert_chat_page(self, chat_id: str, page_id: str, snapshot_id: str, page_context_id: str = "") -> None:
        """记录某个 chat 对某个 page 的 snapshot 绑定。"""
        self.chat_pages[(chat_id, page_id)] = snapshot_id

    def replace_latest_snapshot_for_page(
            self,
            chat_id: str,
            page_id: str,
            canonical_url: str,
            title: str,
            latest_snapshot_id: str,
            content_hash: str,
            url: str,
            chunker_version: str,
            embedding_model: str,
            page_context_id: str = "",
    ) -> list[str]:
        """模拟替换页面 latest snapshot，并返回被替换的旧快照。"""
        old_snapshot_ids = [snapshot_id for snapshot_id in self.snapshot_ids if snapshot_id != latest_snapshot_id]
        if latest_snapshot_id not in self.snapshot_ids:
            self.snapshot_ids.append(latest_snapshot_id)
        self.latest_snapshot_id = latest_snapshot_id

        for key in list(self.chat_pages):
            if key[1] == page_id:
                self.chat_pages[key] = latest_snapshot_id
        self.chat_pages[(chat_id, page_id)] = latest_snapshot_id
        self.replace_calls.append(
            {
                "chat_id": chat_id,
                "page_id": page_id,
                "snapshot_id": latest_snapshot_id,
                "old_snapshot_ids": old_snapshot_ids,
            }
        )
        return old_snapshot_ids

    def list_chat_snapshot_ids(self, chat_id: str) -> list[str]:
        """列出某个 chat 当前绑定的 snapshot_id。"""
        return [snapshot_id for (bound_chat_id, _), snapshot_id in self.chat_pages.items() if bound_chat_id == chat_id]


def patch_page_retrieval(pr, fake_db: FakeDB, fake_vector_store: FakeVectorStore) -> None:
    """把 page_retrieval 的外部依赖替换为可控 fake。"""
    pr.db = fake_db
    pr.vector_store = fake_vector_store
    pr.build_page_identity = lambda url, cleaned_text: {
        "canonical_url": url,
        "page_id": "page_1",
        "content_hash": f"content_{cleaned_text}",
        "snapshot_id": f"snap_{cleaned_text}",
    }
    pr.chunk_text = lambda text, chunk_size, overlap: [
        {"chunk_index": 0, "start": 0, "end": len(text), "content": text}
    ]
    pr.embed_texts = lambda texts, model: [[1.0, 0.0, 0.0] for _ in texts]
    pr.embed_text = lambda text, model: [1.0, 0.0, 0.0]


def test_normal_send_prefers_latest_snapshot() -> None:
    """验证普通发送优先复用 page.latest_snapshot_id。"""
    import tools.page_retrieval as pr

    fake_db = FakeDB(latest_snapshot_id="snap_latest", snapshot_ids=["snap_latest"])
    fake_vector_store = FakeVectorStore({"snap_latest", "snap_current"})
    patch_page_retrieval(pr, fake_db, fake_vector_store)

    result = pr.index_or_reuse_page(
        chat_id="chat_current",
        page_context_id="ctx",
        current_page={"url": "https://example.com/page", "title": "Page", "content": "current"},
    )

    assert result["reuse_reason"] == "latest_snapshot"
    assert fake_db.chat_pages[("chat_current", "page_1")] == "snap_latest"
    assert fake_vector_store.upserted_points == []


def test_force_refresh_writes_new_snapshot_and_replaces_all_chat_bindings() -> None:
    """验证强制刷新会写入新快照并替换所有旧 chat 绑定。"""
    import tools.page_retrieval as pr

    fake_db = FakeDB(latest_snapshot_id="snap_old", snapshot_ids=["snap_old"])
    fake_db.chat_pages[("chat_old", "page_1")] = "snap_old"
    fake_vector_store = FakeVectorStore({"snap_old"})
    patch_page_retrieval(pr, fake_db, fake_vector_store)

    result = pr.index_or_reuse_page(
        chat_id="chat_current",
        page_context_id="ctx_new",
        current_page={"url": "https://example.com/page", "title": "Page", "content": "new"},
        force_refresh=True,
    )

    assert result["reuse_reason"] == "force_refresh"
    assert result["indexed_from_cache"] is False
    assert len(fake_vector_store.upserted_points) == 1
    assert fake_db.chat_pages[("chat_old", "page_1")] == "snap_new"
    assert fake_db.chat_pages[("chat_current", "page_1")] == "snap_new"
    assert fake_vector_store.deleted_snapshot_ids == ["snap_old"]
    assert result["deleted_snapshot_ids"] == ["snap_old"]


def test_force_refresh_reuses_existing_snapshot_without_reupsert() -> None:
    """验证强制刷新遇到已索引新快照时只切换 DB 指针。"""
    import tools.page_retrieval as pr

    fake_db = FakeDB(latest_snapshot_id="snap_old", snapshot_ids=["snap_old", "snap_new"])
    fake_db.chat_pages[("chat_old", "page_1")] = "snap_old"
    fake_vector_store = FakeVectorStore({"snap_old", "snap_new"})
    patch_page_retrieval(pr, fake_db, fake_vector_store)

    result = pr.index_or_reuse_page(
        chat_id="chat_current",
        page_context_id="ctx_new",
        current_page={"url": "https://example.com/page", "title": "Page", "content": "new"},
        force_refresh=True,
    )

    assert result["reuse_reason"] == "force_refresh_existing_snapshot"
    assert result["indexed_from_cache"] is True
    assert fake_vector_store.upserted_points == []
    assert fake_db.chat_pages[("chat_old", "page_1")] == "snap_new"
    assert fake_vector_store.deleted_snapshot_ids == ["snap_old"]


def test_force_refresh_cleanup_error_does_not_block_new_snapshot() -> None:
    """验证旧向量清理失败不会阻断新快照启用。"""
    import tools.page_retrieval as pr

    fake_db = FakeDB(latest_snapshot_id="snap_old", snapshot_ids=["snap_old"])
    fake_db.chat_pages[("chat_old", "page_1")] = "snap_old"
    fake_vector_store = FakeVectorStore({"snap_old"}, fail_delete=True)
    patch_page_retrieval(pr, fake_db, fake_vector_store)

    result = pr.index_or_reuse_page(
        chat_id="chat_current",
        page_context_id="ctx_new",
        current_page={"url": "https://example.com/page", "title": "Page", "content": "new"},
        force_refresh=True,
    )

    assert result["reuse_reason"] == "force_refresh"
    assert fake_db.chat_pages[("chat_old", "page_1")] == "snap_new"
    assert fake_db.chat_pages[("chat_current", "page_1")] == "snap_new"
    assert result["deleted_snapshot_ids"] == []
    assert result["vector_cleanup_error"] == "delete failed"


def test_old_chat_retrieves_new_snapshot_after_refresh() -> None:
    """验证旧 chat 在刷新后会从新的 snapshot 中召回。"""
    import tools.page_retrieval as pr

    fake_db = FakeDB(latest_snapshot_id="snap_new", snapshot_ids=["snap_new"])
    fake_db.chat_pages[("chat_old", "page_1")] = "snap_new"
    fake_vector_store = FakeVectorStore({"snap_new"})
    patch_page_retrieval(pr, fake_db, fake_vector_store)

    sources, stats = pr.retrieve_page_context("chat_old", "question", top_k=3)

    assert fake_vector_store.searched_snapshot_ids == ["snap_new"]
    assert stats["snapshot_count"] == 1
    assert sources[0]["snapshot_id"] == "snap_new"


def test_refresh_snapshot_api_forces_refresh() -> None:
    """验证页面刷新 API 会无视请求体并强制刷新。"""
    import api.pages as pages

    calls: list[dict] = []
    original_index_or_reuse_page = pages.index_or_reuse_page

    def fake_index_or_reuse_page(chat_id, page_context_id, current_page, force_refresh=False):
        """捕获 API 传入 index_or_reuse_page 的参数。"""
        calls.append(
            {
                "chat_id": chat_id,
                "page_context_id": page_context_id,
                "current_page": current_page,
                "force_refresh": force_refresh,
            }
        )
        return {
            "page_id": "page_1",
            "snapshot_id": "snap_new",
            "canonical_url": "https://example.com/page",
            "content_hash": "content_new",
            "reuse_reason": "force_refresh",
            "indexed_from_cache": False,
        }

    pages.index_or_reuse_page = fake_index_or_reuse_page
    try:
        result = pages.refresh_snapshot(
            pages.RefreshSnapshotRequest(
                chat_id=" chat_current ",
                page_context_id="ctx_new",
                current_page=pages.CurrentPage(
                    url="https://example.com/page",
                    title="Page",
                    content="new",
                ),
                force_refresh=False,
            )
        )
    finally:
        pages.index_or_reuse_page = original_index_or_reuse_page

    assert result["reuse_reason"] == "force_refresh"
    assert calls[0]["chat_id"] == "chat_current"
    assert calls[0]["page_context_id"] == "ctx_new"
    assert calls[0]["force_refresh"] is True


if __name__ == "__main__":
    test_db_replace_latest_snapshot_for_page()
    test_normal_send_prefers_latest_snapshot()
    test_force_refresh_writes_new_snapshot_and_replaces_all_chat_bindings()
    test_force_refresh_reuses_existing_snapshot_without_reupsert()
    test_force_refresh_cleanup_error_does_not_block_new_snapshot()
    test_old_chat_retrieves_new_snapshot_after_refresh()
    test_refresh_snapshot_api_forces_refresh()
    print("PASS rag refresh flow")
