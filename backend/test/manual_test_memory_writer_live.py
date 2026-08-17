"""运行单个真实 Memory Writer 类型归类用例。

脚本每次只接受一个 --case。它使用内存 SQLite 和空实现向量函数，
不会修改本地 Browser Agent 数据库或 Qdrant collection。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from storage import db as db_module  # noqa: E402
import memory.store as memory_store  # noqa: E402


CASES = {
    "user_profile": "你以后回答问题都需要从专业角度、全面地回答。",
    "project_state": "当前项目是 Browser Agent，正在关注 Sprint 4 memory。",
    "task_state": "下一步要补 writer skill 文档。",
    "procedural_feedback": "不要为特例写特例代码；先结合代码再回答。",
    "episodic_lesson": "web source 没展示是引用过滤导致。",
    "external_knowledge_ref": (
        "LangMem 文档可作为 memory policy 设计参考："
        "https://langchain-ai.github.io/langmem/concepts/conceptual_guide/"
    ),
}


def make_memory_database() -> db_module.Database:
    """创建一份隔离的内存数据库供单个 live 用例使用。"""
    database = object.__new__(db_module.Database)
    database.conn = sqlite3.connect(":memory:", check_same_thread=False)
    database.conn.row_factory = sqlite3.Row
    database.cursor = database.conn.cursor()
    database.init_db()
    return database


def run_case(memory_type: str, model: str) -> dict:
    """运行指定 memory_type 的真实 writer 抽取与校验流程。"""
    text = CASES[memory_type]
    database = make_memory_database()
    original_db = memory_store.db
    original_search = memory_store.search_memories
    original_upsert = memory_store.upsert_memory
    original_delete = memory_store.delete_memory_vector

    memory_store.db = database
    memory_store.search_memories = lambda query_text, filters, top_k=5: []
    memory_store.upsert_memory = lambda memory_item: None
    memory_store.delete_memory_vector = lambda memory_id: None
    try:
        chat_id = f"chat_taxonomy_{memory_type}"
        turn_id = f"turn_taxonomy_{memory_type}"
        job_id = f"memjob_taxonomy_{memory_type}"
        database.create_chat_turn(
            turn_id=turn_id,
            chat_id=chat_id,
            turn_index=1,
            task_type="chat",
            query_text=text,
        )
        database.insert_chat_message(
            message_id=f"msg_taxonomy_user_{memory_type}",
            chat_id=chat_id,
            turn_id=turn_id,
            role="user",
            content=text,
            display_content=text,
        )
        database.insert_chat_message(
            message_id=f"msg_taxonomy_assistant_{memory_type}",
            chat_id=chat_id,
            turn_id=turn_id,
            role="assistant",
            content="收到。",
            display_content="收到。",
        )
        database.complete_chat_turn(turn_id=turn_id)
        database.create_memory_extraction_job(
            job_id=job_id,
            chat_id=chat_id,
            turn_id=turn_id,
            input_json=json.dumps({"model": model}, ensure_ascii=False),
        )
        output = memory_store.run_memory_extraction_job(job_id)
    finally:
        memory_store.db = original_db
        memory_store.search_memories = original_search
        memory_store.upsert_memory = original_upsert
        memory_store.delete_memory_vector = original_delete
        database.close()

    active_decisions = [
        decision
        for decision in output.get("decisions", [])
        if decision.get("action") != "noop"
    ]
    observed_types = [decision.get("memory_type") for decision in active_decisions]
    return {
        "case": memory_type,
        "input": text,
        "observed_types": observed_types,
        "passed": memory_type in observed_types,
        "decisions": output.get("decisions", []),
        "applied": output.get("applied", []),
        "validation_warnings": output.get("validation_warnings", []),
    }


def main() -> None:
    """命令行入口，执行一个 memory writer live 用例并输出 JSON。"""
    parser = argparse.ArgumentParser(description="Run one live Memory Writer taxonomy case.")
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    parser.add_argument("--model", default="cyankiwi/gemma-4-31B-it-AWQ-4bit")
    args = parser.parse_args()

    result = run_case(args.case, args.model)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
