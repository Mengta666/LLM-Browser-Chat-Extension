"""SQLite 数据访问层。

当前后端只使用一个本地 SQLite 文件保存 chat、page、snapshot 与 chat-page 绑定。
向量数据本体不放在 SQLite 中，只在这里保存可追踪的业务指针。
"""
from typing import Optional, Dict, Any, List
from pathlib import Path

import datetime
import sqlite3

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "browser_agent.sqlite3"


class Database:
    """封装 SQLite 连接和当前 MVP 需要的表操作。"""

    def __init__(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        # row_factory 让查询结果可按列名访问，避免调用方依赖 SELECT 字段顺序。
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

    def init_db(self) -> None:
        """初始化数据库表结构。当前只在手工执行 __main__ 时调用。"""
        with self.conn:
            # chats 只记录对话身份和更新时间，不保存消息正文。
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            # pages 保存逻辑页面；latest_snapshot_id 指向该 URL 当前最新索引版本。
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    page_id TEXT PRIMARY KEY,
                    canonical_url TEXT NOT NULL UNIQUE,
                    latest_snapshot_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            # page_snapshots 保存每次正文版本；旧版本可停用但不删除记录。
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS page_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    chunker_version TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
            """)

            # chat_pages 表示某个 chat 当前应检索哪个页面快照。
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_pages (
                    chat_id TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    page_context_id TEXT NOT NULL DEFAULT '',
                    first_used_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, page_id)
                );
            """)

            # 索引用于加速按 page/chat/snapshot 查找绑定关系。
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_page_snapshots_page_id ON page_snapshots(page_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_pages_chat_id ON chat_pages(chat_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_pages_snapshot_id ON chat_pages(snapshot_id);")

    def close(self) -> None:
        self.conn.close()

    def _now(self):
        return datetime.datetime.now().isoformat()

    def upsert_chat(self, chat_id: str) -> None:
        """插入或更新聊天记录"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chats (chat_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (chat_id) DO UPDATE SET updated_at = excluded.updated_at;
            """, (chat_id, now, now))

    def get_page(self, page_id: str) -> Optional[dict]:
        """根据 page_id 获取页面记录"""
        cursor = self.cursor.execute("SELECT * FROM pages WHERE page_id = ?", (page_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_page(self, page_id: str, canonical_url: str, title: str,
                    latest_snapshot_id: Optional[str]) -> None:
        """插入或更新页面记录，并强制设置最新 snapshot 指针。"""
        if not latest_snapshot_id:
            raise ValueError("upsert_page must set latest_snapshot_id")
        with self.conn:
            self.conn.execute("""
            INSERT INTO pages (page_id, canonical_url, title, latest_snapshot_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (page_id) DO UPDATE SET
                title = excluded.title,
                latest_snapshot_id = excluded.latest_snapshot_id,
                updated_at = excluded.updated_at;
            """, (page_id, canonical_url, title, latest_snapshot_id, self._now(), self._now()))

    def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        """获取快照详情"""
        cursor = self.conn.execute("SELECT * FROM page_snapshots WHERE snapshot_id = ?", (snapshot_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_latest_snapshot(self, page_id: str) -> Optional[Dict[str, Any]]:
        """获取某个页面的最新快照"""
        cursor = self.conn.execute("""
                                   SELECT s.*
                                   FROM page_snapshots s
                                        JOIN pages p ON s.snapshot_id = p.latest_snapshot_id
                                   WHERE p.page_id = ?
                                   """, (page_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_snapshot(self, snapshot_id: str, page_id: str, content_hash: str, title: str, url: str,
                        chunker_version: str, embedding_model: str) -> None:
        """保存快照版本"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                              INSERT INTO page_snapshots (snapshot_id, page_id, content_hash, title, url,
                                                          chunker_version, embedding_model, created_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                              ON CONFLICT(snapshot_id) DO NOTHING
                              """,
                              (snapshot_id, page_id, content_hash, title, url, chunker_version, embedding_model, now))

    def list_page_snapshot_ids(self, page_id: str) -> List[str]:
        """列出某个页面的全部快照 ID。"""
        cursor = self.conn.execute(
            "SELECT snapshot_id FROM page_snapshots WHERE page_id = ? ORDER BY created_at",
            (page_id,),
        )
        return [row["snapshot_id"] for row in cursor.fetchall()]

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
    ) -> List[str]:
        """将同一页面的检索指针整体切换到最新快照。

        这个方法是“刷新快照”的 DB 原子操作：
        1. 记录旧 snapshot，供外层在 DB 切换成功后清理 Qdrant。
        2. 插入或激活新 snapshot。
        3. 更新 pages.latest_snapshot_id。
        4. 把该 page 下所有历史 chat 绑定切到新 snapshot。
        5. 当前 chat 的 page_context_id 使用本次请求传入的新值。
        """
        if not latest_snapshot_id:
            raise ValueError("replace_latest_snapshot_for_page must set latest_snapshot_id")

        now = self._now()
        with self.conn:
            # 先收集旧 snapshot，确保返回值对应本次替换前的状态。
            cursor = self.conn.execute(
                "SELECT snapshot_id FROM page_snapshots WHERE page_id = ?",
                (page_id,),
            )
            old_snapshot_ids = [
                row["snapshot_id"]
                for row in cursor.fetchall()
                if row["snapshot_id"] != latest_snapshot_id
            ]

            self.conn.execute("""
                              INSERT INTO page_snapshots (snapshot_id, page_id, content_hash, title, url,
                                                          chunker_version, embedding_model, created_at, is_active)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                              ON CONFLICT(snapshot_id) DO UPDATE SET title           = excluded.title,
                                                                      url             = excluded.url,
                                                                      chunker_version = excluded.chunker_version,
                                                                      embedding_model = excluded.embedding_model,
                                                                      is_active       = 1
                              """, (
                                  latest_snapshot_id,
                                  page_id,
                                  content_hash,
                                  title,
                                  url,
                                  chunker_version,
                                  embedding_model,
                                  now,
                              ))
            # latest_snapshot_id 是后续普通发送和新 chat 复用页面索引的入口。
            self.conn.execute("""
                              INSERT INTO pages (page_id, canonical_url, title, latest_snapshot_id, created_at,
                                                 updated_at)
                              VALUES (?, ?, ?, ?, ?, ?)
                              ON CONFLICT(page_id) DO UPDATE SET title              = excluded.title,
                                                                  latest_snapshot_id = excluded.latest_snapshot_id,
                                                                  updated_at         = excluded.updated_at
                              """, (page_id, canonical_url, title, latest_snapshot_id, now, now))
            # 保留旧快照元数据，但标记为不再参与当前最新索引语义。
            self.conn.execute(
                "UPDATE page_snapshots SET is_active = 0 WHERE page_id = ? AND snapshot_id != ?",
                (page_id, latest_snapshot_id),
            )
            # 旧 chat 再次启用时，也应该基于该 URL 的最新网页数据回答。
            self.conn.execute(
                "UPDATE chat_pages SET snapshot_id = ? WHERE page_id = ?",
                (latest_snapshot_id, page_id),
            )
            # 当前 chat 可能是第一次使用该页面，也可能只是刷新 page_context_id。
            self.conn.execute("""
                              INSERT INTO chat_pages (chat_id, page_id, snapshot_id, page_context_id, first_used_at,
                                                      last_used_at)
                              VALUES (?, ?, ?, ?, ?, ?)
                              ON CONFLICT(chat_id, page_id) DO UPDATE SET snapshot_id     = excluded.snapshot_id,
                                                                          page_context_id = excluded.page_context_id,
                                                                          last_used_at    = excluded.last_used_at
                              """, (chat_id, page_id, latest_snapshot_id, page_context_id, now, now))

        return old_snapshot_ids

    def get_chat_page(self, chat_id: str, page_id: str) -> Optional[Dict[str, Any]]:
        """查询某个对话绑定了哪个页面版本"""
        cursor = self.conn.execute("SELECT * FROM chat_pages WHERE chat_id = ? AND page_id = ?", (chat_id, page_id))
        row = cursor.fetchone()
        return dict(row) if row else None

    def upsert_chat_page(self, chat_id: str, page_id: str, snapshot_id: str, page_context_id: str = "") -> None:
        """绑定对话与页面版本"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                              INSERT INTO chat_pages (chat_id, page_id, snapshot_id, page_context_id, first_used_at,
                                                      last_used_at)
                              VALUES (?, ?, ?, ?, ?, ?)
                              ON CONFLICT(chat_id, page_id) DO UPDATE SET snapshot_id     = excluded.snapshot_id,
                                                                          page_context_id = excluded.page_context_id,
                                                                          last_used_at    = excluded.last_used_at
                              """, (chat_id, page_id, snapshot_id, page_context_id, now, now))

    def list_chat_snapshot_ids(self, chat_id: str) -> List[str]:
        """列出当前对话绑定的所有快照 ID (用于 Qdrant 过滤)"""
        cursor = self.conn.execute("SELECT snapshot_id FROM chat_pages WHERE chat_id = ?", (chat_id,))
        return [row["snapshot_id"] for row in cursor.fetchall()]


# 实例化单例，方便在其他模块直接 import db
db = Database()

if __name__ == '__main__':
    db.init_db()
    print("Database initialized successfully.")
