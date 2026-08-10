"""计划模式回归测试。"""

import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import HTTPException

import api.chat as chat
import api.plans as plans
from storage import db as db_module


def make_database() -> db_module.Database:
    """创建隔离的内存数据库。"""
    database = object.__new__(db_module.Database)
    database.conn = sqlite3.connect(":memory:", check_same_thread=False)
    database.conn.row_factory = sqlite3.Row
    database.cursor = database.conn.cursor()
    database.init_db()
    return database


def fake_plan_output(title: str = "测试计划") -> dict:
    """构造符合计划 API 约束的伪模型输出。"""
    return {
        "title": title,
        "objective": "补充计划模式",
        "plan_markdown": "## 测试计划\n\n1. 补表\n2. 接 API",
        "checklist": [
            {"title": "补表", "detail": "新增计划表"},
            {"title": "接 API", "detail": "新增 plans API"},
        ],
        "risks": ["模型输出 JSON 失败"],
        "assumptions": ["本地单用户"],
        "acceptance_criteria": ["approve 后创建 task_state"],
        "open_questions": [],
        "change_summary": "生成测试计划",
    }


def fake_create_manual_memory_factory(database: db_module.Database):
    """生成把 task_state 直接写入内存数据库的假 create_manual_memory。"""
    def fake_create_manual_memory(**kwargs):
        """模拟批准计划时创建任务状态记忆。"""
        memory_id = "mem_plan_task"
        database.insert_memory_item(
            memory_id=memory_id,
            memory_type=kwargs["memory_type"],
            scope="chat",
            scope_chat_id=kwargs["scope_chat_id"],
            content=kwargs["content"],
            evidence=kwargs["evidence"],
            tags_json='["todo","next_step"]',
            task_status=kwargs["task_status"],
            task_updated_by=kwargs["task_updated_by"],
            plan_id=kwargs["plan_id"],
        )
        return database.get_memory_item(memory_id)

    return fake_create_manual_memory


def with_plan_patches(database: db_module.Database):
    """记录计划测试会替换的全局依赖。"""
    return {
        "plans_db": plans.db,
        "chat_db": chat.db,
        "call_plan_model": plans._call_plan_model,
        "retrieve_memory_context": plans.retrieve_memory_context,
        "create_manual_memory": plans.create_manual_memory,
    }


def restore_plan_patches(originals):
    """恢复计划测试修改过的全局依赖。"""
    plans.db = originals["plans_db"]
    chat.db = originals["chat_db"]
    plans._call_plan_model = originals["call_plan_model"]
    plans.retrieve_memory_context = originals["retrieve_memory_context"]
    plans.create_manual_memory = originals["create_manual_memory"]


def install_plan_patches(database: db_module.Database):
    """安装内存数据库和伪模型输出依赖。"""
    plans.db = database
    chat.db = database
    plans._call_plan_model = lambda model, messages: fake_plan_output()
    plans.retrieve_memory_context = lambda **kwargs: ([], {})
    plans.create_manual_memory = fake_create_manual_memory_factory(database)


def test_create_plan_and_active_conflict() -> None:
    """验证创建计划会写入草稿，并阻止同一 chat 再创建活跃计划。"""
    database = make_database()
    originals = with_plan_patches(database)
    install_plan_patches(database)
    try:
        result = plans.create_plan(
            "chat_plan",
            plans.PlanCreateRequest(model="fake", objective="补充计划模式"),
        )
        plan = result["plan"]
        assert plan["status"] == "draft"
        assert plan["current_revision"]["revision_index"] == 1
        assert len(plan["current_revision"]["checklist"]) == 2
        assert result["display_message"] == "计划已生成，请在计划面板查看。"
        display_messages = database.list_chat_display_messages("chat_plan")
        assert len(display_messages) == 1
        assert display_messages[0]["role"] == "user"
        assert "## 测试计划" not in display_messages[0]["display_content"]

        try:
            plans.create_plan("chat_plan", plans.PlanCreateRequest(model="fake", objective="另一个计划"))
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("active plan conflict should raise 409")
    finally:
        restore_plan_patches(originals)
        database.close()


def test_create_plan_does_not_pass_existing_chat_history() -> None:
    """验证创建新计划时不会把旧聊天历史传给计划模型。"""
    database = make_database()
    originals = with_plan_patches(database)
    install_plan_patches(database)
    captured_messages = []
    try:
        database.upsert_chat("chat_plan")
        database.create_chat_turn(
            turn_id="old_turn",
            chat_id="chat_plan",
            turn_index=database.next_turn_index("chat_plan"),
            task_type="chat",
            query_text="旧聊天约束",
        )
        database.insert_chat_message(
            message_id="old_user_msg",
            chat_id="chat_plan",
            turn_id="old_turn",
            role="user",
            content="旧聊天约束：必须输出旧内容",
            display_content="旧聊天约束：必须输出旧内容",
        )
        database.insert_chat_message(
            message_id="old_assistant_msg",
            chat_id="chat_plan",
            turn_id="old_turn",
            role="assistant",
            content="旧回答内容",
            display_content="旧回答内容",
        )
        database.complete_chat_turn("old_turn")

        def fake_call_plan_model(model, messages):
            """捕获计划模型输入，返回固定计划。"""
            captured_messages.extend(messages)
            return fake_plan_output()

        plans._call_plan_model = fake_call_plan_model
        plans.create_plan(
            "chat_plan",
            plans.PlanCreateRequest(model="fake", objective="新计划目标"),
        )

        serialized_messages = "\n".join(message["content"] for message in captured_messages)
        assert "旧聊天约束" not in serialized_messages
        assert "旧回答内容" not in serialized_messages
        assert "计划目标：新计划目标" in serialized_messages
    finally:
        restore_plan_patches(originals)
        database.close()


def test_plan_output_requires_risks_and_acceptance_criteria() -> None:
    """验证计划模型输出必须包含风险和验收标准。"""
    valid = fake_plan_output()
    missing_risks = {**valid, "risks": []}
    missing_acceptance = {**valid, "acceptance_criteria": []}

    for payload, expected_error in [
        (missing_risks, "non-empty risks"),
        (missing_acceptance, "non-empty acceptance_criteria"),
    ]:
        try:
            plans._normalize_plan_output(payload, "fallback", "summary")
        except ValueError as exc:
            assert expected_error in str(exc)
        else:
            raise AssertionError(f"expected {expected_error} validation error")


def test_revise_approve_cancel_plan() -> None:
    """验证计划修订、批准、上下文注入和取消流程。"""
    database = make_database()
    originals = with_plan_patches(database)
    install_plan_patches(database)
    try:
        created = plans.create_plan(
            "chat_plan",
            plans.PlanCreateRequest(model="fake", objective="补充计划模式"),
        )
        plan_id = created["plan"]["plan_id"]

        revised = plans.revise_plan(
            plan_id,
            plans.PlanReviseRequest(model="fake", feedback="增加验收标准"),
        )
        assert revised["plan"]["status"] == "draft"
        assert revised["revision"]["revision_index"] == 2
        assert revised["display_message"] == "计划已更新，请在计划面板查看。"
        display_messages = database.list_chat_display_messages("chat_plan")
        assert [message["role"] for message in display_messages] == ["user", "user"]
        assert all("## 测试计划" not in message["display_content"] for message in display_messages)

        approved = plans.approve_plan(plan_id)
        approved_plan = approved["plan"]
        assert approved_plan["status"] == "executing"
        assert approved_plan["task_memory_id"] == "mem_plan_task"
        assert len(approved_plan["steps"]) == 2

        memory = database.get_memory_item("mem_plan_task")
        assert memory["memory_type"] == "task_state"
        assert memory["scope"] == "chat"
        assert memory["scope_chat_id"] == "chat_plan"
        assert memory["task_status"] == "open"
        assert memory["plan_id"] == plan_id
        assert "##" not in memory["evidence"]
        assert "plan_markdown" not in memory["evidence"]

        context_messages, stats = chat.build_active_plan_context_messages("chat_plan")
        assert stats["active_plan_id"] == plan_id
        assert "当前执行计划" in context_messages[0]["content"]
        assert "补表" in context_messages[0]["content"]

        cancelled = plans.cancel_plan(plan_id)
        assert cancelled["plan"]["status"] == "cancelled"
        assert database.get_memory_item("mem_plan_task")["task_status"] == "cancelled"
    finally:
        restore_plan_patches(originals)
        database.close()


def test_complete_plan_marks_done() -> None:
    """验证完成计划会同步关闭步骤和 task_state 记忆。"""
    database = make_database()
    originals = with_plan_patches(database)
    install_plan_patches(database)
    try:
        created = plans.create_plan(
            "chat_plan",
            plans.PlanCreateRequest(model="fake", objective="补充计划模式"),
        )
        plan_id = created["plan"]["plan_id"]
        approved = plans.approve_plan(plan_id)
        assert approved["plan"]["status"] == "executing"

        completed = plans.complete_plan(plan_id)
        completed_plan = completed["plan"]
        assert completed_plan["status"] == "done"
        assert completed_plan["completed_at"]
        assert all(step["status"] == "done" for step in completed_plan["steps"])
        assert all(step["updated_by"] == "assistant" for step in completed_plan["steps"])
        assert database.get_memory_item("mem_plan_task")["task_status"] == "done"
        assert database.get_memory_item("mem_plan_task")["task_updated_by"] == "assistant"
        assert database.get_memory_item("mem_plan_task")["plan_id"] == plan_id
        assert database.get_memory_item("mem_plan_task")["evidence"] == (
            "计划已完成：所有步骤已由自动执行链路输出结果，并由 complete API 标记完成。"
        )
        assert database.get_active_plan("chat_plan") is None
        database.update_memory_item("mem_plan_task", {
            "task_status": "open",
            "task_updated_by": "user",
            "evidence": "stale",
        })
        completed_again = plans.complete_plan(plan_id)
        assert completed_again["plan"]["status"] == "done"
        assert database.get_memory_item("mem_plan_task")["task_status"] == "done"
        assert database.get_memory_item("mem_plan_task")["evidence"] == (
            "计划已完成：所有步骤已由自动执行链路输出结果，并由 complete API 标记完成。"
        )
        events = database.list_plan_events(plan_id)
        assert any(event["event_type"] == "plan_completed" for event in events)
    finally:
        restore_plan_patches(originals)
        database.close()


if __name__ == "__main__":
    test_create_plan_and_active_conflict()
    test_create_plan_does_not_pass_existing_chat_history()
    test_plan_output_requires_risks_and_acceptance_criteria()
    test_revise_approve_cancel_plan()
    test_complete_plan_marks_done()
    print("PASS plan flow")
