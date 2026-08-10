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
        """打开 SQLite 连接，并使用 Row 让结果可按列名访问。"""
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
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL DEFAULT ''
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

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_turns (
                    turn_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    task_type TEXT NOT NULL DEFAULT 'chat',
                    query_text TEXT NOT NULL DEFAULT '',
                    focus_text TEXT NOT NULL DEFAULT '',
                    use_current_page INTEGER NOT NULL DEFAULT 0,
                    use_web_search INTEGER NOT NULL DEFAULT 0,
                    force_refresh_page INTEGER NOT NULL DEFAULT 0,
                    retrieval_query TEXT NOT NULL DEFAULT '',
                    web_search_query TEXT NOT NULL DEFAULT '',
                    page_context_id TEXT NOT NULL DEFAULT '',
                    page_snapshot_id TEXT NOT NULL DEFAULT '',
                    page_url TEXT NOT NULL DEFAULT '',
                    page_title TEXT NOT NULL DEFAULT '',
                    source_kinds_json TEXT NOT NULL DEFAULT '[]',
                    origin TEXT NOT NULL DEFAULT 'user',
                    synthetic_user INTEGER NOT NULL DEFAULT 0,
                    plan_id TEXT NOT NULL DEFAULT '',
                    trace_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_stage TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    display_content TEXT NOT NULL,
                    content_format TEXT NOT NULL DEFAULT 'text',
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'complete',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_turn_summaries (
                    summary_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    summary_version TEXT NOT NULL DEFAULT 'rule_v1',
                    status TEXT NOT NULL DEFAULT 'complete',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_summaries (
                    chat_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    key_points_json TEXT NOT NULL DEFAULT '[]',
                    open_questions_json TEXT NOT NULL DEFAULT '[]',
                    summary_version TEXT NOT NULL DEFAULT 'rule_v1',
                    source_turn_index INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'complete',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_items (
                    memory_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    memory_type TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'user',
                    scope_chat_id TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    classification_reason TEXT NOT NULL DEFAULT '',
                    policy_version TEXT NOT NULL DEFAULT 'memory_writer_skill_v1',
                    mode_affinity_json TEXT NOT NULL DEFAULT '[]',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source_chat_id TEXT NOT NULL DEFAULT '',
                    source_turn_id TEXT NOT NULL DEFAULT '',
                    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    stability REAL NOT NULL DEFAULT 0.5,
                    status TEXT NOT NULL DEFAULT 'active',
                    supersedes_memory_id TEXT NOT NULL DEFAULT '',
                    superseded_by_memory_id TEXT NOT NULL DEFAULT '',
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_to TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL DEFAULT '',
                    task_status TEXT NOT NULL DEFAULT '',
                    task_updated_by TEXT NOT NULL DEFAULT '',
                    plan_id TEXT NOT NULL DEFAULT ''
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_plans (
                    plan_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT 'local',
                    title TEXT NOT NULL DEFAULT '',
                    objective TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    current_revision_id TEXT NOT NULL DEFAULT '',
                    approved_revision_id TEXT NOT NULL DEFAULT '',
                    task_memory_id TEXT NOT NULL DEFAULT '',
                    created_turn_id TEXT NOT NULL DEFAULT '',
                    approved_turn_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_plan_revisions (
                    revision_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    revision_index INTEGER NOT NULL,
                    user_request TEXT NOT NULL DEFAULT '',
                    user_feedback TEXT NOT NULL DEFAULT '',
                    plan_markdown TEXT NOT NULL DEFAULT '',
                    checklist_json TEXT NOT NULL DEFAULT '[]',
                    risks_json TEXT NOT NULL DEFAULT '[]',
                    assumptions_json TEXT NOT NULL DEFAULT '[]',
                    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
                    open_questions_json TEXT NOT NULL DEFAULT '[]',
                    change_summary TEXT NOT NULL DEFAULT '',
                    source_turn_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_plan_steps (
                    step_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_by TEXT NOT NULL DEFAULT '',
                    source_turn_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_plan_events (
                    event_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL DEFAULT '',
                    step_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_extraction_jobs (
                    job_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
            """)

            # 索引用于加速按 page/chat/snapshot 查找绑定关系。
            existing_memory_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(memory_items)").fetchall()
            }
            existing_chat_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(chats)").fetchall()
            }
            existing_chat_turn_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(chat_turns)").fetchall()
            }
            if "status" not in existing_chat_columns:
                self.conn.execute("ALTER TABLE chats ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if "deleted_at" not in existing_chat_columns:
                self.conn.execute("ALTER TABLE chats ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
            if "classification_reason" not in existing_memory_columns:
                self.conn.execute("ALTER TABLE memory_items ADD COLUMN classification_reason TEXT NOT NULL DEFAULT ''")
            if "policy_version" not in existing_memory_columns:
                self.conn.execute(
                    "ALTER TABLE memory_items ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'memory_writer_skill_v1'"
                )
            if "scope_chat_id" not in existing_memory_columns:
                self.conn.execute("ALTER TABLE memory_items ADD COLUMN scope_chat_id TEXT NOT NULL DEFAULT ''")
            if "task_status" not in existing_memory_columns:
                self.conn.execute("ALTER TABLE memory_items ADD COLUMN task_status TEXT NOT NULL DEFAULT ''")
            if "task_updated_by" not in existing_memory_columns:
                self.conn.execute("ALTER TABLE memory_items ADD COLUMN task_updated_by TEXT NOT NULL DEFAULT ''")
            if "plan_id" not in existing_memory_columns:
                self.conn.execute("ALTER TABLE memory_items ADD COLUMN plan_id TEXT NOT NULL DEFAULT ''")
            if "origin" not in existing_chat_turn_columns:
                self.conn.execute("ALTER TABLE chat_turns ADD COLUMN origin TEXT NOT NULL DEFAULT 'user'")
            if "synthetic_user" not in existing_chat_turn_columns:
                self.conn.execute("ALTER TABLE chat_turns ADD COLUMN synthetic_user INTEGER NOT NULL DEFAULT 0")
            if "plan_id" not in existing_chat_turn_columns:
                self.conn.execute("ALTER TABLE chat_turns ADD COLUMN plan_id TEXT NOT NULL DEFAULT ''")

            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_page_snapshots_page_id ON page_snapshots(page_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_pages_chat_id ON chat_pages(chat_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_pages_snapshot_id ON chat_pages(snapshot_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_chat_index ON chat_turns(chat_id, turn_index);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_plan ON chat_turns(plan_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_created ON chat_messages(chat_id, created_at);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_turn_role ON chat_messages(turn_id, role);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turn_summaries_chat_created ON chat_turn_summaries(chat_id, created_at);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_user_status ON memory_items(user_id, status);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_status_type ON memory_items(status, memory_type);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_scope_chat ON memory_items(scope, scope_chat_id, status);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_task_status ON memory_items(memory_type, scope_chat_id, task_status);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_plan ON memory_items(plan_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_items_source_turn ON memory_items(source_turn_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_extraction_jobs_turn ON memory_extraction_jobs(turn_id);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_extraction_jobs_status ON memory_extraction_jobs(status);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_plans_chat_status ON chat_plans(chat_id, status);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_plan_revisions_plan_index ON chat_plan_revisions(plan_id, revision_index);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_plan_steps_plan_status ON chat_plan_steps(plan_id, status);")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_plan_events_plan ON chat_plan_events(plan_id, created_at);")

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        self.conn.close()

    def _now(self):
        """返回当前本地时间的 ISO 字符串。"""
        return datetime.datetime.now().isoformat()

    def upsert_chat(self, chat_id: str) -> None:
        """插入或更新聊天记录"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chats (chat_id, status, created_at, updated_at, deleted_at)
                VALUES (?, 'active', ?, ?, '')
                ON CONFLICT (chat_id) DO UPDATE SET
                    status = 'active',
                    updated_at = excluded.updated_at,
                    deleted_at = '';
            """, (chat_id, now, now))

    def next_turn_index(self, chat_id: str) -> int:
        """返回某个 chat 的下一轮序号。"""
        cursor = self.conn.execute(
            "SELECT COALESCE(MAX(turn_index), 0) + 1 AS next_index FROM chat_turns WHERE chat_id = ?",
            (chat_id,),
        )
        row = cursor.fetchone()
        return int(row["next_index"] if row else 1)

    def create_chat_turn(
            self,
            turn_id: str,
            chat_id: str,
            turn_index: int,
            task_type: str,
            query_text: str = "",
            focus_text: str = "",
            use_current_page: bool = False,
            use_web_search: bool = False,
            force_refresh_page: bool = False,
            retrieval_query: str = "",
            web_search_query: str = "",
            page_context_id: str = "",
            page_url: str = "",
            page_title: str = "",
            origin: str = "user",
            synthetic_user: bool = False,
            plan_id: str = "",
    ) -> None:
        """创建一轮 pending 状态的聊天记录。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chats (chat_id, status, created_at, updated_at, deleted_at)
                VALUES (?, 'active', ?, ?, '')
                ON CONFLICT (chat_id) DO UPDATE SET
                    status = 'active',
                    updated_at = excluded.updated_at,
                    deleted_at = '';
            """, (chat_id, now, now))
            self.conn.execute("""
                INSERT INTO chat_turns (
                    turn_id, chat_id, turn_index, task_type, query_text, focus_text,
                    use_current_page, use_web_search, force_refresh_page,
                    retrieval_query, web_search_query, page_context_id, page_url, page_title,
                    origin, synthetic_user, plan_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                turn_id,
                chat_id,
                turn_index,
                task_type,
                query_text,
                focus_text,
                int(use_current_page),
                int(use_web_search),
                int(force_refresh_page),
                retrieval_query,
                web_search_query,
                page_context_id,
                page_url,
                page_title,
                origin or "user",
                int(bool(synthetic_user)),
                plan_id,
                now,
                now,
            ))

    def complete_chat_turn(
            self,
            turn_id: str,
            retrieval_query: str = "",
            web_search_query: str = "",
            page_context_id: str = "",
            page_snapshot_id: str = "",
            page_url: str = "",
            page_title: str = "",
            source_kinds_json: str = "[]",
            trace_json: str = "{}",
    ) -> None:
        """把一轮聊天标记为完成，并保存运行元数据。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                UPDATE chat_turns
                SET status = 'complete',
                    retrieval_query = ?,
                    web_search_query = ?,
                    page_context_id = ?,
                    page_snapshot_id = ?,
                    page_url = ?,
                    page_title = ?,
                    source_kinds_json = ?,
                    trace_json = ?,
                    error_stage = '',
                    error_message = '',
                    updated_at = ?,
                    completed_at = ?
                WHERE turn_id = ?
            """, (
                retrieval_query,
                web_search_query,
                page_context_id,
                page_snapshot_id,
                page_url,
                page_title,
                source_kinds_json,
                trace_json,
                now,
                now,
                turn_id,
            ))
            self.conn.execute("""
                UPDATE chats
                SET updated_at = ?
                WHERE chat_id = (SELECT chat_id FROM chat_turns WHERE turn_id = ?)
            """, (now, turn_id))

    def fail_chat_turn(self, turn_id: str, error_stage: str, error_message: str, trace_json: str = "{}") -> None:
        """把一轮聊天标记为失败。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                UPDATE chat_turns
                SET status = 'failed',
                    error_stage = ?,
                    error_message = ?,
                    trace_json = ?,
                    updated_at = ?,
                    completed_at = ?
                WHERE turn_id = ?
            """, (error_stage, error_message, trace_json, now, now, turn_id))
            self.conn.execute("""
                UPDATE chats
                SET updated_at = ?
                WHERE chat_id = (SELECT chat_id FROM chat_turns WHERE turn_id = ?)
            """, (now, turn_id))

    def insert_chat_message(
            self,
            message_id: str,
            chat_id: str,
            turn_id: str,
            role: str,
            content: str,
            display_content: str,
            content_format: str = "text",
            sources_json: str = "[]",
            status: str = "complete",
    ) -> None:
        """保存一条聊天消息。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chat_messages (
                    message_id, chat_id, turn_id, role, content, display_content,
                    content_format, sources_json, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id,
                chat_id,
                turn_id,
                role,
                content,
                display_content,
                content_format,
                sources_json,
                status,
                now,
                now,
            ))

    def list_chat_messages(self, chat_id: str) -> List[Dict[str, str]]:
        """读取模型回放用的完整历史消息。"""
        cursor = self.conn.execute("""
            SELECT m.role, m.content
            FROM chat_messages m
                 JOIN chat_turns t ON m.turn_id = t.turn_id
            WHERE m.chat_id = ?
              AND m.status = 'complete'
              AND t.status = 'complete'
              AND EXISTS (
                  SELECT 1
                  FROM chats c
                  WHERE c.chat_id = m.chat_id
                    AND c.status != 'deleted'
              )
              AND m.role IN ('user', 'assistant')
            ORDER BY t.turn_index, m.created_at, m.message_id
        """, (chat_id,))
        return [
            {"role": row["role"], "content": row["content"]}
            for row in cursor.fetchall()
        ]

    def insert_turn_summary(
            self,
            summary_id: str,
            chat_id: str,
            turn_id: str,
            title: str,
            summary: str,
            keywords_json: str = "[]",
            summary_version: str = "rule_v1",
            status: str = "complete",
    ) -> None:
        """保存或更新一轮聊天摘要。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chat_turn_summaries (
                    summary_id, chat_id, turn_id, title, summary, keywords_json,
                    summary_version, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    keywords_json = excluded.keywords_json,
                    summary_version = excluded.summary_version,
                    status = excluded.status,
                    updated_at = excluded.updated_at
            """, (
                summary_id,
                chat_id,
                turn_id,
                title,
                summary,
                keywords_json,
                summary_version,
                status,
                now,
                now,
            ))

    def list_chats_with_latest_summary(self) -> List[Dict[str, Any]]:
        """读取历史会话列表和每个会话的最新摘要。"""
        cursor = self.conn.execute("""
            SELECT
                c.chat_id,
                COALESCE((
                    SELECT s.title
                    FROM chat_turn_summaries s
                         JOIN chat_turns t ON t.turn_id = s.turn_id
                    WHERE s.chat_id = c.chat_id
                      AND s.status = 'complete'
                      AND t.status = 'complete'
                      AND s.title != ''
                    ORDER BY t.turn_index ASC, s.created_at ASC
                    LIMIT 1
                ), '') AS title,
                COALESCE((
                    SELECT s.summary
                    FROM chat_turn_summaries s
                         JOIN chat_turns t ON t.turn_id = s.turn_id
                    WHERE s.chat_id = c.chat_id
                      AND s.status = 'complete'
                      AND t.status = 'complete'
                    ORDER BY t.turn_index DESC, s.created_at DESC
                    LIMIT 1
                ), '') AS latest_summary,
                c.updated_at,
                (
                    SELECT COUNT(1)
                    FROM chat_turns t
                    WHERE t.chat_id = c.chat_id
                      AND t.status = 'complete'
                ) AS turn_count
            FROM chats c
            WHERE c.status != 'deleted'
            ORDER BY c.updated_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    def list_chat_display_messages(self, chat_id: str) -> List[Dict[str, Any]]:
        """读取前端展示历史用的消息。"""
        cursor = self.conn.execute("""
            SELECT message_id, chat_id, turn_id, role, display_content, sources_json,
                   content_format, status, created_at, updated_at
            FROM chat_messages
            WHERE chat_id = ?
              AND status = 'complete'
              AND EXISTS (
                  SELECT 1
                  FROM chat_turns t
                  WHERE t.turn_id = chat_messages.turn_id
                    AND t.status = 'complete'
              )
              AND EXISTS (
                  SELECT 1
                  FROM chats c
                  WHERE c.chat_id = chat_messages.chat_id
                    AND c.status != 'deleted'
              )
            ORDER BY created_at, message_id
        """, (chat_id,))
        return [dict(row) for row in cursor.fetchall()]

    def soft_delete_chat(self, chat_id: str) -> bool:
        """Soft-delete a chat and hide its turns, messages and summaries."""
        now = self._now()
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return False

        with self.conn:
            cursor = self.conn.execute("""
                UPDATE chats
                SET status = 'deleted',
                    updated_at = ?,
                    deleted_at = ?
                WHERE chat_id = ?
                  AND status != 'deleted'
            """, (now, now, normalized_chat_id))
            if cursor.rowcount == 0:
                return False

            self.conn.execute("""
                UPDATE chat_turns
                SET status = 'deleted',
                    updated_at = ?,
                    completed_at = CASE WHEN completed_at = '' THEN ? ELSE completed_at END
                WHERE chat_id = ?
                  AND status != 'deleted'
            """, (now, now, normalized_chat_id))
            self.conn.execute("""
                UPDATE chat_messages
                SET status = 'deleted',
                    updated_at = ?
                WHERE chat_id = ?
                  AND status != 'deleted'
            """, (now, normalized_chat_id))
            self.conn.execute("""
                UPDATE chat_turn_summaries
                SET status = 'deleted',
                    updated_at = ?
                WHERE chat_id = ?
                  AND status != 'deleted'
            """, (now, normalized_chat_id))
            self.conn.execute("""
                UPDATE chat_summaries
                SET status = 'deleted',
                    updated_at = ?
                WHERE chat_id = ?
                  AND status != 'deleted'
            """, (now, normalized_chat_id))
            self.conn.execute("DELETE FROM chat_pages WHERE chat_id = ?", (normalized_chat_id,))
        return True

    def get_chat_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        """读取单个 turn 的完整元数据。"""
        cursor = self.conn.execute("SELECT * FROM chat_turns WHERE turn_id = ?", (turn_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_turn_messages(self, turn_id: str) -> List[Dict[str, Any]]:
        """读取某一轮的消息，供 memory writer 使用。"""
        cursor = self.conn.execute("""
            SELECT *
            FROM chat_messages
            WHERE turn_id = ?
              AND status = 'complete'
            ORDER BY created_at, message_id
        """, (turn_id,))
        return [dict(row) for row in cursor.fetchall()]

    def upsert_chat_summary(
            self,
            chat_id: str,
            summary: str,
            key_points_json: str = "[]",
            open_questions_json: str = "[]",
            summary_version: str = "rule_v1",
            source_turn_index: int = 0,
            status: str = "complete",
    ) -> None:
        """保存会话级滚动摘要。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chat_summaries (
                    chat_id, summary, key_points_json, open_questions_json,
                    summary_version, source_turn_index, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    summary = excluded.summary,
                    key_points_json = excluded.key_points_json,
                    open_questions_json = excluded.open_questions_json,
                    summary_version = excluded.summary_version,
                    source_turn_index = excluded.source_turn_index,
                    status = excluded.status,
                    updated_at = excluded.updated_at
            """, (
                chat_id,
                summary,
                key_points_json,
                open_questions_json,
                summary_version,
                int(source_turn_index),
                status,
                now,
                now,
            ))

    def get_chat_summary(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """读取会话级滚动摘要。"""
        cursor = self.conn.execute("SELECT * FROM chat_summaries WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def create_memory_extraction_job(
            self,
            job_id: str,
            chat_id: str,
            turn_id: str,
            input_json: str = "{}",
            status: str = "pending",
    ) -> None:
        """创建一条 memory writer 调试 job。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO memory_extraction_jobs (
                    job_id, chat_id, turn_id, status, input_json, output_json,
                    error_message, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, '{}', '', ?, ?, '')
            """, (job_id, chat_id, turn_id, status, input_json, now, now))

    def update_memory_extraction_job(
            self,
            job_id: str,
            status: str,
            output_json: str = "{}",
            error_message: str = "",
            completed: bool = False,
    ) -> None:
        """更新 memory writer job 的状态和输出。"""
        now = self._now()
        completed_at = now if completed else ""
        with self.conn:
            self.conn.execute("""
                UPDATE memory_extraction_jobs
                SET status = ?,
                    output_json = ?,
                    error_message = ?,
                    updated_at = ?,
                    completed_at = CASE WHEN ? != '' THEN ? ELSE completed_at END
                WHERE job_id = ?
            """, (status, output_json, error_message, now, completed_at, completed_at, job_id))

    def get_memory_extraction_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """读取 memory writer job。"""
        cursor = self.conn.execute("SELECT * FROM memory_extraction_jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def insert_memory_item(
            self,
            memory_id: str,
            memory_type: str,
            content: str,
            user_id: str = "local",
            scope: str = "user",
            scope_chat_id: str = "",
            evidence: str = "",
            classification_reason: str = "",
            policy_version: str = "memory_writer_skill_v1",
            mode_affinity_json: str = "[]",
            tags_json: str = "[]",
            source_chat_id: str = "",
            source_turn_id: str = "",
            source_message_ids_json: str = "[]",
            importance: float = 0.5,
            confidence: float = 0.5,
            stability: float = 0.5,
            status: str = "active",
            supersedes_memory_id: str = "",
            superseded_by_memory_id: str = "",
            valid_from: str = "",
            valid_to: str = "",
            task_status: str = "",
            task_updated_by: str = "",
            plan_id: str = "",
    ) -> None:
        """新增或覆盖一条长期记忆。"""
        now = self._now()
        valid_from_value = valid_from or now
        with self.conn:
            self.conn.execute("""
                INSERT INTO memory_items (
                    memory_id, user_id, memory_type, scope, content, evidence,
                    scope_chat_id, classification_reason, policy_version,
                    mode_affinity_json, tags_json, source_chat_id, source_turn_id,
                    source_message_ids_json, importance, confidence, stability,
                    status, supersedes_memory_id, superseded_by_memory_id,
                    valid_from, valid_to, created_at, updated_at, last_used_at,
                    task_status, task_updated_by, plan_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    memory_type = excluded.memory_type,
                    scope = excluded.scope,
                    scope_chat_id = excluded.scope_chat_id,
                    content = excluded.content,
                    evidence = excluded.evidence,
                    classification_reason = excluded.classification_reason,
                    policy_version = excluded.policy_version,
                    mode_affinity_json = excluded.mode_affinity_json,
                    tags_json = excluded.tags_json,
                    source_chat_id = excluded.source_chat_id,
                    source_turn_id = excluded.source_turn_id,
                    source_message_ids_json = excluded.source_message_ids_json,
                    importance = excluded.importance,
                    confidence = excluded.confidence,
                    stability = excluded.stability,
                    status = excluded.status,
                    supersedes_memory_id = excluded.supersedes_memory_id,
                    superseded_by_memory_id = excluded.superseded_by_memory_id,
                    valid_from = excluded.valid_from,
                    valid_to = excluded.valid_to,
                    task_status = excluded.task_status,
                    task_updated_by = excluded.task_updated_by,
                    plan_id = excluded.plan_id,
                    updated_at = excluded.updated_at
            """, (
                memory_id,
                user_id,
                memory_type,
                scope,
                content,
                evidence,
                scope_chat_id,
                classification_reason,
                policy_version,
                mode_affinity_json,
                tags_json,
                source_chat_id,
                source_turn_id,
                source_message_ids_json,
                float(importance),
                float(confidence),
                float(stability),
                status,
                supersedes_memory_id,
                superseded_by_memory_id,
                valid_from_value,
                valid_to,
                now,
                now,
                task_status,
                task_updated_by,
                plan_id,
            ))

    def update_memory_item(self, memory_id: str, updates: Dict[str, Any]) -> None:
        """按白名单更新 memory_items 字段。"""
        allowed_fields = {
            "content",
            "scope",
            "scope_chat_id",
            "evidence",
            "classification_reason",
            "policy_version",
            "mode_affinity_json",
            "tags_json",
            "importance",
            "confidence",
            "stability",
            "status",
            "supersedes_memory_id",
            "superseded_by_memory_id",
            "source_chat_id",
            "source_turn_id",
            "source_message_ids_json",
            "valid_to",
            "last_used_at",
            "task_status",
            "task_updated_by",
            "plan_id",
        }
        fields = [field for field in updates if field in allowed_fields]
        if not fields:
            return

        now = self._now()
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = [updates[field] for field in fields]
        values.extend([now, memory_id])
        with self.conn:
            self.conn.execute(
                f"UPDATE memory_items SET {assignments}, updated_at = ? WHERE memory_id = ?",
                values,
            )

    def get_memory_item(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """读取单条 memory。"""
        cursor = self.conn.execute("SELECT * FROM memory_items WHERE memory_id = ?", (memory_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def list_memory_items(
            self,
            status: str = "active",
            user_id: str = "local",
            memory_types: Optional[List[str]] = None,
            scope: str = "",
            scope_chat_id: str = "",
            task_statuses: Optional[List[str]] = None,
            limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """按状态、用户和类型读取 memory。"""
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if memory_types:
            placeholders = ",".join("?" for _ in memory_types)
            clauses.append(f"memory_type IN ({placeholders})")
            params.extend(memory_types)
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if scope_chat_id:
            clauses.append("scope_chat_id = ?")
            params.append(scope_chat_id)
        if task_statuses:
            placeholders = ",".join("?" for _ in task_statuses)
            clauses.append(f"task_status IN ({placeholders})")
            params.extend(task_statuses)
        params.append(int(limit))
        cursor = self.conn.execute(f"""
            SELECT *
            FROM memory_items
            WHERE {' AND '.join(clauses)}
            ORDER BY importance DESC, confidence DESC, updated_at DESC
            LIMIT ?
        """, params)
        return [dict(row) for row in cursor.fetchall()]

    def list_memory_items_by_ids(self, memory_ids: List[str]) -> List[Dict[str, Any]]:
        """按 ID 批量读取 memory，并保持传入顺序。"""
        ids = [memory_id for memory_id in memory_ids if memory_id]
        if not ids:
            return []

        placeholders = ",".join("?" for _ in ids)
        cursor = self.conn.execute(
            f"SELECT * FROM memory_items WHERE memory_id IN ({placeholders})",
            ids,
        )
        rows = {row["memory_id"]: dict(row) for row in cursor.fetchall()}
        return [rows[memory_id] for memory_id in ids if memory_id in rows]

    def mark_memory_used(self, memory_ids: List[str]) -> None:
        """记录 memory 最近一次注入时间。"""
        ids = [memory_id for memory_id in memory_ids if memory_id]
        if not ids:
            return

        now = self._now()
        placeholders = ",".join("?" for _ in ids)
        with self.conn:
            self.conn.execute(
                f"UPDATE memory_items SET last_used_at = ?, updated_at = ? WHERE memory_id IN ({placeholders})",
                [now, now, *ids],
            )

    def soft_delete_memory_item(self, memory_id: str) -> None:
        """软删除 memory，向量索引由调用方同步删除。"""
        now = self._now()
        self.update_memory_item(memory_id, {
            "status": "deleted",
            "valid_to": now,
        })

    def list_latest_memory_jobs_by_turn_ids(self, turn_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """按 turn_id 批量读取最新 memory writer job。"""
        ids = [turn_id for turn_id in dict.fromkeys(turn_ids) if turn_id]
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)
        cursor = self.conn.execute(f"""
            SELECT *
            FROM memory_extraction_jobs
            WHERE turn_id IN ({placeholders})
            ORDER BY created_at DESC, job_id DESC
        """, ids)
        jobs: Dict[str, Dict[str, Any]] = {}
        for row in cursor.fetchall():
            turn_id = row["turn_id"]
            if turn_id not in jobs:
                jobs[turn_id] = dict(row)
        return jobs

    def get_active_plan(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """读取某个 chat 当前仍处于草稿、待修订或执行中的计划。"""
        cursor = self.conn.execute("""
            SELECT *
            FROM chat_plans
            WHERE chat_id = ?
              AND status IN ('draft', 'needs_revision', 'executing')
            ORDER BY updated_at DESC
            LIMIT 1
        """, (chat_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """按 plan_id 读取计划主表记录。"""
        cursor = self.conn.execute("SELECT * FROM chat_plans WHERE plan_id = ?", (plan_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def insert_chat_plan(
            self,
            plan_id: str,
            chat_id: str,
            title: str,
            objective: str,
            current_revision_id: str = "",
            created_turn_id: str = "",
            user_id: str = "local",
            status: str = "draft",
    ) -> None:
        """插入一条计划主表记录。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chat_plans (
                    plan_id, chat_id, user_id, title, objective, status,
                    current_revision_id, created_turn_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                plan_id,
                chat_id,
                user_id,
                title,
                objective,
                status,
                current_revision_id,
                created_turn_id,
                now,
                now,
            ))

    def update_chat_plan(self, plan_id: str, updates: Dict[str, Any]) -> None:
        """按白名单字段局部更新计划主表。"""
        allowed_fields = {
            "title",
            "objective",
            "status",
            "current_revision_id",
            "approved_revision_id",
            "task_memory_id",
            "approved_turn_id",
            "completed_at",
        }
        fields = [field for field in updates if field in allowed_fields]
        if not fields:
            return

        now = self._now()
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = [updates[field] for field in fields]
        values.extend([now, plan_id])
        with self.conn:
            self.conn.execute(
                f"UPDATE chat_plans SET {assignments}, updated_at = ? WHERE plan_id = ?",
                values,
            )

    def insert_plan_revision(
            self,
            revision_id: str,
            plan_id: str,
            revision_index: int,
            user_request: str = "",
            user_feedback: str = "",
            plan_markdown: str = "",
            checklist_json: str = "[]",
            risks_json: str = "[]",
            assumptions_json: str = "[]",
            acceptance_criteria_json: str = "[]",
            open_questions_json: str = "[]",
            change_summary: str = "",
            source_turn_id: str = "",
    ) -> None:
        """插入计划的一个版本快照。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chat_plan_revisions (
                    revision_id, plan_id, revision_index, user_request, user_feedback,
                    plan_markdown, checklist_json, risks_json, assumptions_json,
                    acceptance_criteria_json, open_questions_json, change_summary,
                    source_turn_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                revision_id,
                plan_id,
                int(revision_index),
                user_request,
                user_feedback,
                plan_markdown,
                checklist_json,
                risks_json,
                assumptions_json,
                acceptance_criteria_json,
                open_questions_json,
                change_summary,
                source_turn_id,
                now,
            ))

    def get_plan_revision(self, revision_id: str) -> Optional[Dict[str, Any]]:
        """按 revision_id 读取计划版本。"""
        cursor = self.conn.execute(
            "SELECT * FROM chat_plan_revisions WHERE revision_id = ?",
            (revision_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_current_plan_revision(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """读取计划当前版本；主表指针缺失时回退到最新 revision_index。"""
        plan = self.get_plan(plan_id)
        if plan and plan.get("current_revision_id"):
            return self.get_plan_revision(plan["current_revision_id"])
        cursor = self.conn.execute("""
            SELECT *
            FROM chat_plan_revisions
            WHERE plan_id = ?
            ORDER BY revision_index DESC
            LIMIT 1
        """, (plan_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def next_plan_revision_index(self, plan_id: str) -> int:
        """返回计划的下一个 revision_index。"""
        cursor = self.conn.execute(
            "SELECT COALESCE(MAX(revision_index), 0) + 1 AS next_index FROM chat_plan_revisions WHERE plan_id = ?",
            (plan_id,),
        )
        row = cursor.fetchone()
        return int(row["next_index"] if row else 1)

    def insert_plan_step(
            self,
            step_id: str,
            plan_id: str,
            revision_id: str,
            step_index: int,
            title: str,
            detail: str = "",
            status: str = "pending",
            updated_by: str = "",
            source_turn_id: str = "",
    ) -> None:
        """插入计划步骤，通常在批准计划时从 checklist 展开。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chat_plan_steps (
                    step_id, plan_id, revision_id, step_index, title, detail,
                    status, updated_by, source_turn_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                step_id,
                plan_id,
                revision_id,
                int(step_index),
                title,
                detail,
                status,
                updated_by,
                source_turn_id,
                now,
                now,
            ))

    def list_plan_steps(self, plan_id: str) -> List[Dict[str, Any]]:
        """列出计划当前版本对应的步骤。"""
        plan = self.get_plan(plan_id)
        revision_id = ""
        if plan:
            revision_id = plan.get("approved_revision_id") or plan.get("current_revision_id") or ""
        revision_clause = "AND revision_id = ?" if revision_id else ""
        params: list[Any] = [plan_id]
        if revision_id:
            params.append(revision_id)
        cursor = self.conn.execute("""
            SELECT *
            FROM chat_plan_steps
            WHERE plan_id = ?
            """ + revision_clause + """
            ORDER BY step_index, created_at
        """, params)
        return [dict(row) for row in cursor.fetchall()]

    def update_plan_steps_status(
            self,
            plan_id: str,
            status: str,
            updated_by: str = "system",
            revision_id: str = "",
    ) -> None:
        """批量更新计划步骤状态。"""
        now = self._now()
        clauses = ["plan_id = ?"]
        params: list[Any] = [status, updated_by, now, plan_id]
        if revision_id:
            clauses.append("revision_id = ?")
            params.append(revision_id)

        with self.conn:
            self.conn.execute(f"""
                UPDATE chat_plan_steps
                SET status = ?,
                    updated_by = ?,
                    updated_at = ?
                WHERE {" AND ".join(clauses)}
            """, params)

    def insert_plan_event(
            self,
            event_id: str,
            plan_id: str,
            chat_id: str,
            event_type: str,
            revision_id: str = "",
            step_id: str = "",
            turn_id: str = "",
            summary: str = "",
    ) -> None:
        """写入计划生命周期事件。"""
        now = self._now()
        with self.conn:
            self.conn.execute("""
                INSERT INTO chat_plan_events (
                    event_id, plan_id, revision_id, step_id, chat_id, turn_id,
                    event_type, summary, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id,
                plan_id,
                revision_id,
                step_id,
                chat_id,
                turn_id,
                event_type,
                summary,
                now,
            ))

    def list_plan_events(self, plan_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """按时间倒序列出计划事件。"""
        cursor = self.conn.execute("""
            SELECT *
            FROM chat_plan_events
            WHERE plan_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (plan_id, int(limit)))
        return [dict(row) for row in cursor.fetchall()]

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
