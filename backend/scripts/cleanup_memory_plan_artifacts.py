"""清理计划记忆边界明确前产生的历史脏数据。

默认只做 dry-run；传入 --apply 后才会修改本地 SQLite/Qdrant 状态。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from memory.store import delete_memory, patch_memory  # noqa: E402
from storage.db import db  # noqa: E402


TERMINAL_TASK_STATUSES = {"done", "cancelled"}
COMPACT_DONE_EVIDENCE = "计划已完成：历史终态任务由清理脚本压缩 evidence；后续以 plan/steps 状态为准。"
COMPACT_CANCELLED_EVIDENCE = "计划已取消：历史终态任务由清理脚本压缩 evidence；后续以 plan/steps 状态为准。"


def fetch_rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """执行只读 SQL 并返回普通字典列表。"""
    cursor = db.conn.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def find_misclassified_rag_project_memories() -> list[dict[str, Any]]:
    """找出把 RAG/GraphRAG 外部知识误写成 project_state 的记忆。"""
    return fetch_rows(
        """
        SELECT memory_id, memory_type, content, evidence, source_chat_id, source_turn_id, updated_at
        FROM memory_items
        WHERE status = 'active'
          AND memory_type = 'project_state'
          AND (
            (content LIKE '%RAG%' AND content LIKE '%GraphRAG%')
            OR (evidence LIKE '%RAG%' AND evidence LIKE '%GraphRAG%')
          )
        ORDER BY updated_at DESC
        """
    )


def find_terminal_task_memories() -> list[dict[str, Any]]:
    """找出已完成/已取消但证据过长或缺少 plan_id 的 task_state。"""
    return fetch_rows(
        """
        SELECT m.memory_id, m.scope_chat_id, m.task_status, m.task_updated_by, m.plan_id,
               m.source_chat_id, m.source_turn_id, length(m.evidence) AS evidence_len,
               m.updated_at, p.plan_id AS linked_plan_id, p.status AS linked_plan_status
        FROM memory_items m
        LEFT JOIN chat_plans p ON p.task_memory_id = m.memory_id
        WHERE m.status = 'active'
          AND m.memory_type = 'task_state'
          AND (
            (
              m.task_status IN ('done', 'cancelled')
              AND (m.plan_id = '' OR length(m.evidence) > 300)
            )
            OR (
              p.status IN ('done', 'cancelled')
              AND (
                m.plan_id = ''
                OR length(m.evidence) > 300
                OR m.task_status != CASE WHEN p.status = 'done' THEN 'done' ELSE 'cancelled' END
              )
            )
          )
        ORDER BY m.updated_at DESC
        """
    )


def find_plan_for_task_memory(memory: dict[str, Any]) -> dict[str, Any] | None:
    """根据 task_state 反查最可能关联的计划。"""
    memory_id = str(memory.get("memory_id") or "")
    if memory_id:
        row = db.conn.execute(
            "SELECT * FROM chat_plans WHERE task_memory_id = ? ORDER BY updated_at DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
        if row:
            return dict(row)

    chat_id = str(memory.get("scope_chat_id") or memory.get("source_chat_id") or "")
    if not chat_id:
        return None
    row = db.conn.execute(
        """
        SELECT *
        FROM chat_plans
        WHERE chat_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (chat_id,),
    ).fetchone()
    return dict(row) if row else None


def build_task_patch(memory: dict[str, Any]) -> dict[str, Any]:
    """为一条终态 task_state 构造压缩 evidence 和状态修正补丁。"""
    plan = find_plan_for_task_memory(memory)
    status = str(memory.get("linked_plan_status") or memory.get("task_status") or "")
    evidence = COMPACT_DONE_EVIDENCE if status == "done" else COMPACT_CANCELLED_EVIDENCE
    patch = {"evidence": evidence}
    if status == "done":
        patch["task_status"] = "done"
        patch["task_updated_by"] = "assistant"
    elif status == "cancelled":
        patch["task_status"] = "cancelled"
        patch["task_updated_by"] = "user"
    if plan and not str(memory.get("plan_id") or "").strip():
        patch["plan_id"] = plan["plan_id"]
    return patch


def summarize_task_patches(task_memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成 dry-run 输出中的 task_state 修正预览。"""
    result = []
    for memory in task_memories:
        patch = build_task_patch(memory)
        result.append({
            "memory_id": memory["memory_id"],
            "task_status": memory["task_status"],
            "linked_plan_status": memory.get("linked_plan_status", ""),
            "current_plan_id": memory.get("plan_id", ""),
            "patched_plan_id": patch.get("plan_id", ""),
            "patched_task_status": patch.get("task_status", ""),
            "current_evidence_len": memory.get("evidence_len", 0),
            "patched_evidence": patch["evidence"],
        })
    return result


def apply_cleanup(project_memories: list[dict[str, Any]], task_memories: list[dict[str, Any]]) -> dict[str, Any]:
    """删除误分类项目记忆，并修正终态 task_state。"""
    deleted_project_ids = []
    patched_task_ids = []
    for memory in project_memories:
        delete_memory(memory["memory_id"])
        deleted_project_ids.append(memory["memory_id"])
    for memory in task_memories:
        patch = build_task_patch(memory)
        patch_memory(
            memory["memory_id"],
            evidence=patch["evidence"],
            plan_id=patch.get("plan_id"),
            task_status=patch.get("task_status"),
            task_updated_by=patch.get("task_updated_by"),
        )
        patched_task_ids.append(memory["memory_id"])
    return {
        "deleted_project_memory_ids": deleted_project_ids,
        "patched_task_memory_ids": patched_task_ids,
    }


def main() -> None:
    """脚本入口，输出预览并在 --apply 时执行清理。"""
    parser = argparse.ArgumentParser(description="Clean plan-related memory artifacts.")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Omit for dry-run.")
    args = parser.parse_args()

    project_memories = find_misclassified_rag_project_memories()
    task_memories = find_terminal_task_memories()
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "misclassified_project_memories": [
            {
                "memory_id": row["memory_id"],
                "source_chat_id": row.get("source_chat_id", ""),
                "content": str(row.get("content") or "")[:160],
            }
            for row in project_memories
        ],
        "terminal_task_memory_patches": summarize_task_patches(task_memories),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not args.apply:
        return

    result = apply_cleanup(project_memories, task_memories)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
