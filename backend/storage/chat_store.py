"""会话历史存储(SQLite)。

存 chat 的完整对话消息,供"会话列表 + 续谈"使用。与长期记忆(Qdrant,全局跨会话)
是正交两层:这里按 chat_id 存"这次聊了什么",记忆存"用户是谁"。

单进程、单连接 + 锁。服务端模式以历史为事实源，写入失败不能确认回答已保存；
旧客户端模式保留其兼容入口。

三张表:
- chat_sessions:会话身份(chat_id, title, 时间戳, 软删标记)
- chat_messages:消息正文(message_id, chat_id, role, content, 时间戳)
- chat_turns:请求幂等、尝试次数和持久化状态
"""

import sqlite3
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


# 独立库文件(与 agent_memory.sqlite3 分开,互不干扰)
_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "chat_history.sqlite3"

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

# 会话标题自动取首条 user 消息前 N 字
_TITLE_MAX_LEN = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    """惰性打开连接并建表。单连接 + 锁。"""
    global _conn
    with _lock:
        if _conn is None:
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                columns = {row[1] for row in conn.execute('PRAGMA table_info(chat_messages)')}
                if columns and 'seq' not in columns:
                    backup = _DB_PATH.with_name(f'{_DB_PATH.stem}.migration-{uuid4().hex[:8]}.sqlite3')
                    with sqlite3.connect(str(backup)) as target:
                        conn.backup(target)
                _init_schema(conn)
                # 单进程启动恢复：旧进程留下的请求不再具有提交权。
                with conn:
                    conn.execute("UPDATE chat_turns SET status='interrupted', error_code='backend_restarted' WHERE status='running'")
                    conn.execute("UPDATE chat_sessions SET active_request_id='' WHERE active_request_id!=''")
            except Exception:
                conn.close()
                raise
            _conn = conn
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id     TEXT PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                deleted_at  TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                message_id  TEXT PRIMARY KEY,
                chat_id     TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_chat "
            "ON chat_messages(chat_id, created_at);"
        )
        # 上下文压缩:摘要字段(兼容存量库,列已存在则忽略)
        for col, typedef in [
            ("summary",            "TEXT NOT NULL DEFAULT ''"),
            ("summary_msg_count",  "INTEGER NOT NULL DEFAULT 0"),
            ("summary_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE chat_sessions ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # duplicate column — 存量库已有

        # 工具调用步骤(assistant 消息专用,JSON 字符串)
        try:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN tools TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        for table, fields in {
            'chat_sessions': {
                'context_mode': "TEXT NOT NULL DEFAULT 'client'",
                'last_seq': 'INTEGER NOT NULL DEFAULT 0',
                'active_request_id': "TEXT NOT NULL DEFAULT ''",
                'context_summary': "TEXT NOT NULL DEFAULT ''",
                'summary_upto_seq': 'INTEGER NOT NULL DEFAULT 0',
                'summary_version': 'INTEGER NOT NULL DEFAULT 0',
            },
            'chat_messages': {'seq': 'INTEGER', 'request_id': "TEXT NOT NULL DEFAULT ''"},
        }.items():
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            for name, definition in fields.items():
                if name not in columns:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        pending = conn.execute('SELECT DISTINCT chat_id FROM chat_messages WHERE seq IS NULL').fetchall()
        for row in pending:
            chat_id = row['chat_id']
            last = conn.execute('SELECT COALESCE(MAX(seq),0) FROM chat_messages WHERE chat_id=?', (chat_id,)).fetchone()[0]
            missing = conn.execute('SELECT message_id FROM chat_messages WHERE chat_id=? AND seq IS NULL ORDER BY created_at,rowid', (chat_id,)).fetchall()
            conn.executemany('UPDATE chat_messages SET seq=? WHERE message_id=?', [(last+i+1, message['message_id']) for i, message in enumerate(missing)])
        conn.execute('UPDATE chat_sessions SET last_seq=COALESCE((SELECT MAX(seq) FROM chat_messages WHERE chat_messages.chat_id=chat_sessions.chat_id),0)')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_message_seq ON chat_messages(chat_id,seq)')
        conn.execute("""CREATE TABLE IF NOT EXISTS chat_turns (
            chat_id TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
            user_seq INTEGER NOT NULL, assistant_seq INTEGER,
            error_code TEXT NOT NULL DEFAULT '', request_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(chat_id,request_id))""")
        if 'request_json' not in {row[1] for row in conn.execute('PRAGMA table_info(chat_turns)')}:
            conn.execute("ALTER TABLE chat_turns ADD COLUMN request_json TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in conn.execute('PRAGMA table_info(chat_turns)')}
        for name in ('finish_reason', 'continuation_of'):
            if name not in columns:
                conn.execute(f"ALTER TABLE chat_turns ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_role ON chat_messages(chat_id,request_id,role) WHERE request_id!=''")


def ensure_session(chat_id: str, first_user_text: str = "") -> bool:
    """会话不存在则新建(标题暂取首条 user 消息前 N 字);存在则刷新 updated_at。

    返回 True 表示本次是**新建**(供调用方决定是否触发 LLM 起标题),False 表示已存在。
    """
    if not chat_id:
        return False
    conn = _get_conn()
    now = _now_iso()
    title = str(first_user_text or "").strip().replace("\n", " ")[:_TITLE_MAX_LEN]
    with _lock, conn:
        row = conn.execute(
            "SELECT chat_id, title FROM chat_sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO chat_sessions (chat_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, title, now, now),
            )
            return True
        # 已存在:刷新时间;若旧标题为空且这次有文本,补上标题
        if not row["title"] and title:
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ?, title = ? WHERE chat_id = ?",
                (now, title, chat_id),
            )
        else:
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE chat_id = ?",
                (now, chat_id),
            )
        return False


def set_title(chat_id: str, title: str) -> bool:
    """设置会话标题(LLM 自动命名用,不改 updated_at 以免打乱列表排序)。空标题忽略。"""
    if not chat_id:
        return False
    new_title = str(title or "").strip().replace("\n", " ")[:_TITLE_MAX_LEN]
    if not new_title:
        return False
    conn = _get_conn()
    with _lock, conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE chat_id = ? AND deleted_at = ''",
            (new_title, chat_id),
        )
        return cur.rowcount > 0


def add_message(chat_id: str, role: str, content: str, tools: str = "") -> None:
    """追加一条消息。role ∈ user/assistant。空 chat_id/content 忽略。

    tools: assistant 消息的工具调用步骤(JSON 字符串,user 消息传空)。
    """
    if not chat_id or not str(content or "").strip():
        return
    conn = _get_conn()
    with _lock, conn:
        session = conn.execute('SELECT context_mode FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
        if session and session['context_mode'] == 'server':
            raise SessionError('server_context_required')
        seq = conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM chat_messages WHERE chat_id=?', (chat_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO chat_messages (message_id, chat_id, role, content, tools, created_at,seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid4().hex, chat_id, role, content, tools, _now_iso(), seq),
        )
        conn.execute('UPDATE chat_sessions SET last_seq=? WHERE chat_id=?', (seq, chat_id))


def list_sessions(limit: int = 100) -> list[dict[str, Any]]:
    """列出未删除会话,按 updated_at 倒序(最近在前)。"""
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT chat_id, title, created_at, updated_at FROM chat_sessions "
            "WHERE deleted_at = '' ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_messages(chat_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """取某会话的消息,按时间正序(供续谈重建对话)。"""
    if not chat_id:
        return []
    conn = _get_conn()
    with _lock:
        sql = 'SELECT message_id,seq,request_id,role,content,tools,created_at FROM chat_messages WHERE chat_id=? ORDER BY seq'
        rows = conn.execute(sql + (' LIMIT ?' if limit is not None else ''),
                            (chat_id, max(1, int(limit))) if limit is not None else (chat_id,)).fetchall()
    return [dict(r) for r in rows]


def rename_session(chat_id: str, title: str) -> bool:
    """重命名会话。会话不存在返回 False。"""
    if not chat_id:
        return False
    conn = _get_conn()
    new_title = str(title or "").strip().replace("\n", " ")[:_TITLE_MAX_LEN]
    with _lock, conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = ? "
            "WHERE chat_id = ? AND deleted_at = ''",
            (new_title, _now_iso(), chat_id),
        )
        return cur.rowcount > 0


def soft_delete(chat_id: str) -> bool:
    """软删会话(标记 deleted_at,消息正文保留可回溯)。会话不存在返回 False。"""
    if not chat_id:
        return False
    conn = _get_conn()
    with _lock, conn:
        session = conn.execute('SELECT active_request_id FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
        if session and session['active_request_id']:
            raise SessionError('chat_busy')
        cur = conn.execute(
            "UPDATE chat_sessions SET deleted_at = ?, updated_at = ? "
            "WHERE chat_id = ? AND deleted_at = ''",
            (_now_iso(), _now_iso(), chat_id),
        )
        return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 上下文压缩(Context Compaction)
# ═══════════════════════════════════════════════════════════════════════════════

def get_summary(chat_id: str) -> dict[str, Any]:
    """读取会话摘要。返回 {summary, msg_count, updated_at}。会话不存在返回全空/零。"""
    if not chat_id:
        return {"summary": "", "msg_count": 0, "updated_at": ""}
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT summary, summary_msg_count, summary_updated_at "
            "FROM chat_sessions WHERE chat_id = ?", (chat_id,),
        ).fetchone()
    if row is None:
        return {"summary": "", "msg_count": 0, "updated_at": ""}
    return {
        "summary": row["summary"] or "",
        "msg_count": int(row["summary_msg_count"] or 0),
        "updated_at": row["summary_updated_at"] or "",
    }


def set_summary(chat_id: str, summary: str, msg_count: int) -> bool:
    """更新会话摘要(覆盖式)。summary_updated_at 自动设为当前时间。"""
    if not chat_id:
        return False
    conn = _get_conn()
    with _lock, conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET summary = ?, summary_msg_count = ?, "
            "summary_updated_at = ? WHERE chat_id = ?",
            (summary, max(0, int(msg_count)), _now_iso(), chat_id),
        )
        return cur.rowcount > 0


def count_messages(chat_id: str) -> int:
    """统计某会话的消息总数。"""
    if not chat_id:
        return 0
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM chat_messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    return int(row["cnt"]) if row else 0


def get_messages_after(chat_id: str, offset: int, limit: int = 500) -> list[dict[str, Any]]:
    """取摘要覆盖点之后的原文(按时间正序)。offset=0 等同 get_messages。"""
    if not chat_id:
        return []
    conn = _get_conn()
    with _lock:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_messages "
            "WHERE chat_id = ? ORDER BY seq LIMIT ? OFFSET ?",
            (chat_id, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
    return [dict(r) for r in rows]


class SessionError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code = code
        self.status = status


def get_session(chat_id: str) -> dict[str, Any] | None:
    conn = _get_conn()
    with _lock:
        row = conn.execute('SELECT * FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
    return dict(row) if row else None


def begin_turn(chat_id: str, request_id: str, request_hash: str, expected_last_seq: int, content: str, request_json: str = '', continuation_of: str = '') -> dict[str, Any]:
    conn = _get_conn()
    now = _now_iso()
    with _lock, conn:
        conn.execute('BEGIN IMMEDIATE')
        session = conn.execute('SELECT * FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
        if session and session['deleted_at']:
            raise SessionError('session_deleted', 404)
        previous = conn.execute('SELECT * FROM chat_turns WHERE chat_id=? AND request_id=?', (chat_id, request_id)).fetchone()
        if previous:
            if previous['request_hash'] != request_hash:
                raise SessionError('request_conflict')
            if previous['status'] in ('completed', 'partial'):
                return dict(previous)
            if previous['status'] == 'running':
                raise SessionError('request_in_progress')
            if session['active_request_id']:
                raise SessionError('chat_busy')
            if session['last_seq'] != previous['user_seq']:
                raise SessionError('retry_stale')
            conn.execute("UPDATE chat_turns SET status='running',attempt=attempt+1,error_code='',updated_at=? WHERE chat_id=? AND request_id=?", (now, chat_id, request_id))
        else:
            if session and session['active_request_id']:
                raise SessionError('chat_busy')
            last_seq = session['last_seq'] if session else 0
            if last_seq != expected_last_seq:
                raise SessionError('history_conflict')
            if continuation_of:
                parent = conn.execute('SELECT * FROM chat_turns WHERE chat_id=? AND request_id=?', (chat_id, continuation_of)).fetchone()
                if not parent or parent['status'] != 'partial' or not parent['request_json']:
                    raise SessionError('continuation_unavailable')
                if parent['assistant_seq'] != last_seq:
                    raise SessionError('continuation_stale')
            if session and session['summary_msg_count'] > conn.execute('SELECT COUNT(*) FROM chat_messages WHERE chat_id=?', (chat_id,)).fetchone()[0]:
                raise SessionError('legacy_history_incomplete')
            if not session:
                conn.execute('INSERT INTO chat_sessions(chat_id,title,created_at,updated_at) VALUES(?,?,?,?)', (chat_id, content[:_TITLE_MAX_LEN], now, now))
            user_seq = last_seq + 1
            conn.execute("INSERT INTO chat_messages(message_id,chat_id,role,content,created_at,seq,request_id) VALUES(?,?,'user',?,?,?,?)", (uuid4().hex, chat_id, content, now, user_seq, request_id))
            conn.execute("INSERT INTO chat_turns(chat_id,request_id,request_hash,status,user_seq,created_at,updated_at,request_json,continuation_of) VALUES(?,?,?,'running',?,?,?,?,?)", (chat_id, request_id, request_hash, user_seq, now, now, request_json, continuation_of))
            conn.execute('UPDATE chat_sessions SET last_seq=? WHERE chat_id=?', (user_seq, chat_id))
        conn.execute("UPDATE chat_sessions SET context_mode='server',active_request_id=?,updated_at=? WHERE chat_id=?", (request_id, now, chat_id))
        return dict(conn.execute('SELECT * FROM chat_turns WHERE chat_id=? AND request_id=?', (chat_id, request_id)).fetchone())


def complete_turn(chat_id: str, request_id: str, attempt: int, content: str, tools: list, finish_reason: str = 'stop') -> dict[str, Any]:
    if finish_reason not in ('stop', 'length') or not content.strip():
        raise ValueError('invalid_answer')
    status = 'partial' if finish_reason == 'length' else 'completed'
    conn = _get_conn()
    now = _now_iso()
    with _lock, conn:
        conn.execute('BEGIN IMMEDIATE')
        turn = conn.execute('SELECT * FROM chat_turns WHERE chat_id=? AND request_id=?', (chat_id, request_id)).fetchone()
        session = conn.execute('SELECT * FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
        if not turn or turn['status'] != 'running' or turn['attempt'] != attempt or session['active_request_id'] != request_id or session['deleted_at']:
            raise SessionError('stale_attempt')
        seq = session['last_seq'] + 1
        conn.execute("INSERT INTO chat_messages(message_id,chat_id,role,content,tools,created_at,seq,request_id) VALUES(?,?,'assistant',?,?,?,?,?)", (uuid4().hex, chat_id, content, json.dumps(tools, ensure_ascii=False), now, seq, request_id))
        conn.execute("UPDATE chat_turns SET status=?,finish_reason=?,assistant_seq=?,updated_at=? WHERE chat_id=? AND request_id=?", (status, finish_reason, seq, now, chat_id, request_id))
        conn.execute("UPDATE chat_sessions SET last_seq=?,active_request_id='',updated_at=? WHERE chat_id=?", (seq, now, chat_id))
    return get_request(chat_id, request_id)


def fail_turn(chat_id: str, request_id: str, attempt: int, code: str, interrupted: bool = False) -> None:
    conn = _get_conn()
    with _lock, conn:
        cur = conn.execute("UPDATE chat_turns SET status=?,error_code=?,updated_at=? WHERE chat_id=? AND request_id=? AND attempt=? AND status='running'", ('interrupted' if interrupted else 'failed', code, _now_iso(), chat_id, request_id, attempt))
        if cur.rowcount:
            conn.execute("UPDATE chat_sessions SET active_request_id='' WHERE chat_id=? AND active_request_id=?", (chat_id, request_id))


def get_request(chat_id: str, request_id: str) -> dict[str, Any] | None:
    conn = _get_conn()
    with _lock:
        row = conn.execute('SELECT * FROM chat_turns WHERE chat_id=? AND request_id=?', (chat_id, request_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result.pop('request_hash')
        result['retry_request'] = json.loads(result.pop('request_json') or 'null')
        session = conn.execute('SELECT last_seq,active_request_id,deleted_at FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
        result['last_seq'] = session['last_seq']
        result['can_retry'] = row['status'] in ('failed', 'interrupted') and session['last_seq'] == row['user_seq'] and not session['active_request_id']
        result['can_continue'] = bool(row['status'] == 'partial' and result['retry_request']
                                      and session['last_seq'] == row['assistant_seq']
                                      and not session['active_request_id'] and not session['deleted_at'])
        result['messages'] = [dict(message) for message in conn.execute('SELECT message_id,seq,role,content,tools FROM chat_messages WHERE chat_id=? AND request_id=? ORDER BY seq', (chat_id, request_id))]
        return result


def context_snapshot(chat_id: str) -> dict[str, Any]:
    conn = _get_conn()
    with _lock:
        session = dict(conn.execute('SELECT * FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone())
        rows = conn.execute("""SELECT m.*,COALESCE(t.status,'completed') AS status FROM chat_messages m LEFT JOIN chat_turns t
            ON m.chat_id=t.chat_id AND m.request_id=t.request_id
            WHERE m.chat_id=? AND m.seq>? AND (m.request_id='' OR t.status IN ('completed','partial')) ORDER BY m.seq""", (chat_id, session['summary_upto_seq'])).fetchall()
        return {'summary': session['context_summary'], 'upto_seq': session['summary_upto_seq'],
                'version': session['summary_version'], 'messages': [dict(row) for row in rows]}


def publish_summary(chat_id: str, text: str, upto_seq: int, previous_version: int) -> bool:
    conn = _get_conn()
    with _lock, conn:
        cur = conn.execute("""UPDATE chat_sessions SET context_summary=?,summary_upto_seq=?,summary_version=summary_version+1
            WHERE chat_id=? AND summary_version=? AND summary_upto_seq<? AND last_seq>=? AND deleted_at=''""",
            (text, upto_seq, chat_id, previous_version, upto_seq, upto_seq))
        return cur.rowcount == 1


def message_page(chat_id: str, before_seq: int | None, limit: int) -> dict[str, Any]:
    conn = _get_conn()
    with _lock:
        session = conn.execute('SELECT * FROM chat_sessions WHERE chat_id=?', (chat_id,)).fetchone()
        if session and session['deleted_at']:
            raise SessionError('session_deleted', 404)
        rows = conn.execute("""SELECT m.*,COALESCE(t.status,'completed') AS status,t.error_code,t.finish_reason,t.continuation_of,
            (t.status='partial' AND t.request_json!='') AS continuation_available
            FROM chat_messages m LEFT JOIN chat_turns t ON m.chat_id=t.chat_id AND m.request_id=t.request_id
            WHERE m.chat_id=? AND (? IS NULL OR m.seq<?) ORDER BY m.seq DESC LIMIT ?""",
            (chat_id, before_seq, before_seq, limit + 1)).fetchall()
        messages = [dict(row) for row in reversed(rows[:limit])]
        for message in messages:
            message['can_continue'] = bool(message.pop('continuation_available') and message['role'] == 'assistant'
                                           and session and message['seq'] == session['last_seq']
                                           and not session['active_request_id'])
        return {'chat_id': chat_id, 'messages': messages, 'count': len(messages),
                'last_seq': session['last_seq'] if session else 0,
                'context_mode': session['context_mode'] if session else 'client',
                'active_request_id': session['active_request_id'] if session else '',
                'has_more': len(rows) > limit, 'before_seq': messages[0]['seq'] if len(rows) > limit else None,
                'total': conn.execute('SELECT COUNT(*) FROM chat_messages WHERE chat_id=?', (chat_id,)).fetchone()[0]}
