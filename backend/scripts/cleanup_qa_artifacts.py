"""清理本地 QA 运行残留。

默认只做 dry-run；传入 --apply 后才会修改本地 SQLite 和记忆向量。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from memory.store import patch_memory  # noqa: E402
from memory.store import delete_memory  # noqa: E402
from storage.db import db  # noqa: E402


QA_CHAT_PREFIXES = ("chat_qa_", "chat_taxonomy_")
QA_JOB_PREFIXES = ("memjob_taxonomy_",)
USER_PROFILE_CONTENT = (
    "用户要求在回答技术问题时默认使用中文，且偏好专业、全面的回答风格；"
    "在处理对比类问题时，要求深度聚合搜索信息并分析优缺点。"
)
USER_PROFILE_EVIDENCE = (
    "以后回答技术问题默认使用中文，并且要专业全面。\n"
    "请专业全面回答，并深度聚合搜索到的信息，给出对比和优缺点。"
)
USER_PROFILE_REASON = "用户明确表达的长期回答偏好，已移除未由用户原文支持的助手扩写。"
USER_PROFILE_TAGS = ["language_preference", "answer_style", "detail_level"]


def fetch_rows(sql: str, params: tuple = ()) -> list[dict]:
    """执行只读查询并把 sqlite3.Row 转成普通字典。"""
    cursor = db.conn.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def qa_like_clause(column: str, prefixes: tuple[str, ...]) -> tuple[str, list[str]]:
    """为 QA 专用 ID 前缀构造 LIKE 条件。"""
    clauses = [f"{column} LIKE ?" for _ in prefixes]
    return " OR ".join(clauses), [f"{prefix}%" for prefix in prefixes]


def find_qa_chats() -> list[dict]:
    """找出仍在历史列表中的 QA 对话。"""
    clause, params = qa_like_clause("chat_id", QA_CHAT_PREFIXES)
    return fetch_rows(
        f"""
        SELECT chat_id, status, created_at, updated_at
        FROM chats
        WHERE ({clause})
          AND status != 'deleted'
        ORDER BY created_at
        """,
        tuple(params),
    )


def find_stale_qa_jobs() -> list[dict]:
    """找出 QA 运行后还停留在 pending/processing 的记忆任务。"""
    chat_clause, chat_params = qa_like_clause("chat_id", QA_CHAT_PREFIXES)
    job_clause, job_params = qa_like_clause("job_id", QA_JOB_PREFIXES)
    return fetch_rows(
        f"""
        SELECT job_id, chat_id, turn_id, status, created_at, updated_at
        FROM memory_extraction_jobs
        WHERE status IN ('pending', 'processing')
          AND ({chat_clause} OR {job_clause})
        ORDER BY created_at
        """,
        tuple(chat_params + job_params),
    )


def find_qa_memories() -> list[dict]:
    """找出由 QA 对话写入的 active 记忆。"""
    clause, params = qa_like_clause("source_chat_id", QA_CHAT_PREFIXES)
    return fetch_rows(
        f"""
        SELECT memory_id, memory_type, source_chat_id, status, created_at, updated_at
        FROM memory_items
        WHERE status = 'active'
          AND ({clause})
        ORDER BY created_at
        """,
        tuple(params),
    )


def find_user_profile(memory_id: str) -> dict | None:
    """查找需要修正的 user_profile 记忆。"""
    if memory_id:
        return db.get_memory_item(memory_id)
    rows = db.list_memory_items(status="active", memory_types=["user_profile"], limit=10)
    return rows[0] if rows else None


def apply_cleanup(chats: list[dict], jobs: list[dict], memories: list[dict]) -> None:
    """把 QA 对话隐藏、卡住的 job 标失败，并删除 QA 记忆。"""
    for chat in chats:
        db.soft_delete_chat(chat["chat_id"])
    for job in jobs:
        db.update_memory_extraction_job(
            job["job_id"],
            status="failed",
            output_json="{}",
            error_message="manually failed stale QA job during cleanup",
            completed=True,
        )
    for memory in memories:
        delete_memory(memory["memory_id"])


def apply_user_profile_fix(memory_id: str) -> dict:
    """把长期回答偏好记忆修正为人工确认过的文本。"""
    memory = find_user_profile(memory_id)
    if not memory:
        raise RuntimeError("active user_profile memory not found")
    return patch_memory(
        memory_id=memory["memory_id"],
        content=USER_PROFILE_CONTENT,
        evidence=USER_PROFILE_EVIDENCE,
        classification_reason=USER_PROFILE_REASON,
        tags=USER_PROFILE_TAGS,
        importance=0.9,
        confidence=1.0,
        stability=0.9,
        status="active",
    )


def main() -> None:
    """脚本入口，先打印预览，再按 --apply 决定是否落地。"""
    parser = argparse.ArgumentParser(description="Clean local Browser Agent QA artifacts.")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Omit for dry-run.")
    parser.add_argument("--fix-user-profile", action="store_true", help="Patch active user_profile to the approved text.")
    parser.add_argument("--memory-id", default="", help="Specific user_profile memory_id to patch.")
    args = parser.parse_args()

    chats = find_qa_chats()
    jobs = find_stale_qa_jobs()
    memories = find_qa_memories()
    memory = find_user_profile(args.memory_id) if args.fix_user_profile else None

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "qa_chats": [{"chat_id": row["chat_id"], "status": row["status"]} for row in chats],
        "stale_qa_jobs": [{"job_id": row["job_id"], "status": row["status"]} for row in jobs],
        "qa_memories": [
            {
                "memory_id": row["memory_id"],
                "memory_type": row["memory_type"],
                "source_chat_id": row["source_chat_id"],
                "status": row["status"],
            }
            for row in memories
        ],
        "user_profile_target": memory["memory_id"] if memory else "",
        "fix_user_profile": bool(args.fix_user_profile),
    }, ensure_ascii=False, indent=2))

    if not args.apply:
        return

    apply_cleanup(chats, jobs, memories)
    patched_memory = apply_user_profile_fix(args.memory_id) if args.fix_user_profile else None
    print(json.dumps({
        "cleaned_chat_count": len(chats),
        "failed_job_count": len(jobs),
        "deleted_memory_count": len(memories),
        "patched_memory_id": patched_memory.get("memory_id", "") if patched_memory else "",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
