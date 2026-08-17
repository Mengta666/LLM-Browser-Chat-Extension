"""记忆 MVP 回归测试。"""

import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from storage import db as db_module
from memory import policy_v2
import memory.store as memory_store
from api import memory as memory_api


def make_memory_database() -> db_module.Database:
    """创建隔离的内存数据库供记忆测试使用。"""
    database = object.__new__(db_module.Database)
    database.conn = sqlite3.connect(":memory:", check_same_thread=False)
    database.conn.row_factory = sqlite3.Row
    database.cursor = database.conn.cursor()
    database.init_db()
    return database


def test_failed_turn_is_not_replayed() -> None:
    """验证失败 turn 不会进入后续聊天历史回放。"""
    database = make_memory_database()
    try:
        database.create_chat_turn(
            turn_id="turn_complete",
            chat_id="chat_1",
            turn_index=1,
            task_type="chat",
            query_text="complete user",
        )
        database.insert_chat_message(
            message_id="msg_user_complete",
            chat_id="chat_1",
            turn_id="turn_complete",
            role="user",
            content="complete user",
            display_content="complete user",
        )
        database.insert_chat_message(
            message_id="msg_assistant_complete",
            chat_id="chat_1",
            turn_id="turn_complete",
            role="assistant",
            content="complete assistant",
            display_content="complete assistant",
        )
        database.complete_chat_turn(turn_id="turn_complete")

        database.create_chat_turn(
            turn_id="turn_failed",
            chat_id="chat_1",
            turn_index=2,
            task_type="chat",
            query_text="failed user",
        )
        database.insert_chat_message(
            message_id="msg_user_failed",
            chat_id="chat_1",
            turn_id="turn_failed",
            role="user",
            content="failed user",
            display_content="failed user",
        )
        database.fail_chat_turn("turn_failed", "upstream_chat", "boom")

        messages = database.list_chat_messages("chat_1")
    finally:
        database.close()

    assert messages == [
        {"role": "user", "content": "complete user"},
        {"role": "assistant", "content": "complete assistant"},
    ]


class FakeMemoryDB:
    """模拟记忆召回所需的最小 DB 接口。"""

    def __init__(self) -> None:
        """初始化固定记忆行和使用记录。"""
        self.used_ids: list[str] = []
        self.last_memory_types: list[str] = []
        self.rows = [
            {
                "memory_id": "mem_user",
                "user_id": "local",
                "memory_type": "user_profile",
                "scope": "user",
                "content": "用户偏好简洁直接的中文回答。",
                "evidence": "",
                "mode_affinity_json": '["chat","translate"]',
                "tags_json": '["style"]',
                "source_message_ids_json": "[]",
                "importance": 0.8,
                "confidence": 0.9,
                "stability": 0.9,
                "status": "active",
                "updated_at": "2026-06-22T00:00:00",
            },
            {
                "memory_id": "mem_project",
                "user_id": "local",
                "memory_type": "project_state",
                "scope": "user",
                "content": "当前项目正在实现浏览器智能体 memory MVP。",
                "evidence": "",
                "mode_affinity_json": '["planning"]',
                "tags_json": '["project"]',
                "source_message_ids_json": "[]",
                "importance": 0.7,
                "confidence": 0.8,
                "stability": 0.7,
                "status": "active",
                "updated_at": "2026-06-22T00:00:00",
            },
            {
                "memory_id": "mem_proc",
                "user_id": "local",
                "memory_type": "procedural_feedback",
                "scope": "user",
                "content": "代码设计必须采用通用方案，不为单个样例写特例。",
                "evidence": "",
                "mode_affinity_json": '["coding","review"]',
                "tags_json": '["design"]',
                "source_message_ids_json": "[]",
                "importance": 0.95,
                "confidence": 0.95,
                "stability": 0.95,
                "status": "active",
                "updated_at": "2026-06-22T00:00:00",
            },
        ]

    def list_memory_items(
            self,
            status="active",
            user_id="local",
            memory_types=None,
            scope="",
            scope_chat_id="",
            task_statuses=None,
            limit=100,
    ):
        """按测试传入的类型、scope 和状态过滤记忆行。"""
        self.last_memory_types = list(memory_types or [])
        return [
            row for row in self.rows
            if row["status"] == status and row["memory_type"] in self.last_memory_types
            and (not scope or row.get("scope", "user") == scope)
            and (not scope_chat_id or row.get("scope_chat_id", "") == scope_chat_id)
            and (not task_statuses or row.get("task_status", "") in task_statuses)
        ][:limit]

    def list_memory_items_by_ids(self, memory_ids):
        """按 memory_id 返回匹配的记忆行。"""
        by_id = {row["memory_id"]: row for row in self.rows}
        return [
            by_id[memory_id]
            for memory_id in memory_ids
            if memory_id in by_id and by_id[memory_id]["memory_type"] in self.last_memory_types
        ]

    def mark_memory_used(self, memory_ids):
        """记录被召回链路标记为已使用的记忆。"""
        self.used_ids.extend(memory_ids)


def test_translate_memory_retrieval_excludes_project_state() -> None:
    """验证翻译任务不会召回 project_state 记忆。"""
    original_db = memory_store.db
    original_search_memories = memory_store.search_memories
    fake_db = FakeMemoryDB()

    def fake_search_memories(query_text, filters, top_k=5):
        """模拟向量检索，并确认过滤条件排除了 project_state。"""
        assert "project_state" not in filters["memory_types"]
        return [
            {"memory_id": "mem_project", "score": 0.99},
            {"memory_id": "mem_user", "score": 0.88},
            {"memory_id": "mem_proc", "score": 0.77},
        ]

    memory_store.db = fake_db
    memory_store.search_memories = fake_search_memories
    try:
        messages, stats = memory_store.retrieve_memory_context(
            query_text="翻译这段话",
            focus_text="hello",
            task_type="translate",
            use_current_page=False,
            use_web_search=False,
        )
    finally:
        memory_store.db = original_db
        memory_store.search_memories = original_search_memories

    injected = "\n".join(message["content"] for message in messages)
    assert "用户偏好简洁直接" in injected
    assert "通用方案" in injected
    assert "memory MVP" not in injected
    assert stats["memory_mode"] == "translate"
    assert stats["memory_types"] == ["user_profile", "procedural_feedback"]
    assert fake_db.used_ids == ["mem_user", "mem_proc"]


def test_memory_policy_classifies_six_types() -> None:
    """验证新版策略支持六类记忆类型。"""
    cases = [
        ("以后回答要专业全面", "你以后回答问题都需要从专业角度、全面地回答。", "user_profile", ["answer_style"]),
        ("不要为特例写特例代码", "不要为特例写特例代码；先结合代码再回答。", "procedural_feedback", ["code_design_rule"]),
        ("当前项目", "当前项目是 Browser Agent，关注 Sprint 4 memory。", "project_state", ["progress"]),
        ("下一步", "下一步要补 writer skill 文档。", "task_state", ["next_step"]),
        ("web source 没展示", "web source 没展示是引用过滤导致。", "episodic_lesson", ["citation_issue"]),
        ("LangMem 文档", "LangMem 文档可作为 memory policy 设计参考。", "external_knowledge_ref", ["doc_reference"]),
    ]

    for evidence, content, expected_type, tags in cases:
        decision = policy_v2.normalize_decision({
            "action": "insert",
            "memory_type": expected_type,
            "content": content,
            "evidence": evidence,
            "classification_reason": "test",
            "mode_affinity": [],
            "tags": tags,
            "importance": 2,
            "confidence": -1,
            "stability": 0.8,
            "target_memory_id": "",
            "related_memory_ids": [],
        })
        assert decision["memory_type"] == expected_type
        assert decision["action"] == "insert"
        assert decision["tags"]
        assert decision["importance"] == 1.0
        assert decision["confidence"] == 0.0


def test_memory_policy_keeps_llm_type_without_keyword_coercion() -> None:
    """验证策略不会只因关键词就强行改写模型给出的类型。"""
    decision = policy_v2.normalize_decision({
        "action": "insert",
        "memory_type": "procedural_feedback",
        "content": "下一步回答代码问题时，也要先结合代码再回答。",
        "evidence": "不要只看日志，回答代码问题时先结合代码。",
        "classification_reason": "用户给出 Agent 工作流程要求。",
        "mode_affinity": [],
        "tags": ["workflow_rule"],
        "importance": 0.8,
        "confidence": 1.0,
        "stability": 0.9,
        "target_memory_id": "",
        "related_memory_ids": [],
    })

    assert decision["memory_type"] == "procedural_feedback"
    assert "memory_type_corrected_by_tag_taxonomy" not in decision["validation_warnings"]


def test_memory_policy_corrects_type_only_from_tag_taxonomy() -> None:
    """验证标签唯一指向其他类型时才修正 memory_type。"""
    decision = policy_v2.normalize_decision({
        "action": "insert",
        "memory_type": "procedural_feedback",
        "content": "下一步要补 writer skill 文档。",
        "evidence": "下一步要补 writer skill 文档。",
        "classification_reason": "用户明确给出当前待办。",
        "mode_affinity": [],
        "tags": ["next_step", "todo"],
        "importance": 0.7,
        "confidence": 1.0,
        "stability": 0.5,
        "target_memory_id": "",
        "related_memory_ids": [],
    })

    assert decision["memory_type"] == "task_state"
    assert decision["tags"] == ["next_step", "todo"]
    assert "memory_type_corrected_by_tag_taxonomy" in decision["validation_warnings"]


def test_memory_policy_noops_on_conflicting_tag_taxonomy() -> None:
    """验证冲突标签会让写入决策保守 noop。"""
    decision = policy_v2.normalize_decision({
        "action": "insert",
        "memory_type": "user_profile",
        "content": "用户希望专业全面回答，同时下一步补 writer skill 文档。",
        "evidence": "以后回答要专业全面。下一步补 writer skill 文档。",
        "classification_reason": "模型输出了混合分类。",
        "mode_affinity": [],
        "tags": ["answer_style", "next_step"],
        "importance": 0.7,
        "confidence": 0.8,
        "stability": 0.6,
        "target_memory_id": "",
        "related_memory_ids": [],
    })

    assert decision["action"] == "noop"
    assert decision["memory_type"] == "user_profile"
    assert "memory_type_conflict_noop" in decision["validation_warnings"]


def test_memory_policy_rejects_invalid_decisions() -> None:
    """验证非法类型和缺少证据的写入会被拒绝。"""
    unknown_type = policy_v2.normalize_decision({
        "action": "insert",
        "memory_type": "unknown",
        "content": "valid content",
        "evidence": "valid evidence",
        "classification_reason": "test",
    })
    no_evidence = policy_v2.normalize_decision({
        "action": "insert",
        "memory_type": "user_profile",
        "content": "用户偏好中文回答。",
        "evidence": "",
        "classification_reason": "test",
    })

    assert unknown_type["action"] == "noop"
    assert "unknown_memory_type" in unknown_type["validation_warnings"]
    assert no_evidence["action"] == "noop"
    assert "empty_evidence" in no_evidence["validation_warnings"]


def test_memory_policy_sanitizes_internal_ids_from_display_reason() -> None:
    """验证面向展示的分类原因会隐藏内部 ID。"""
    decision = policy_v2.normalize_decision({
        "action": "insert",
        "memory_type": "user_profile",
        "content": "用户偏好专业全面的回答。",
        "evidence": "用户要求以后专业全面回答。",
        "classification_reason": (
            "候选记忆与旧记忆 mem_9294747641f74d9fa25b2258495f0570 "
            "以及 chat_98f331f4-b63b-4a0c-bae9-0d147ff0a17a 内容一致。"
        ),
        "mode_affinity": [],
        "tags": ["answer_style"],
        "importance": 0.7,
        "confidence": 0.9,
        "stability": 0.9,
        "target_memory_id": "",
        "related_memory_ids": [],
    })
    row = policy_v2.normalize_memory_row({
        "memory_id": "mem_visible_action_id",
        "memory_type": "user_profile",
        "content": "用户偏好专业全面的回答。",
        "classification_reason": "由 turn_abc123 中的长期偏好抽取。",
        "mode_affinity_json": "[]",
        "tags_json": "[]",
        "source_message_ids_json": "[]",
    })

    assert "mem_" not in decision["classification_reason"]
    assert "chat_" not in decision["classification_reason"]
    assert "内部记录" in decision["classification_reason"]
    assert "turn_" not in row["classification_reason"]
    assert "内部记录" in row["classification_reason"]


def test_memory_writer_prunes_user_profile_to_user_evidence() -> None:
    """验证用户画像只保留当前用户证据支持的内容。"""
    decision = policy_v2.normalize_decision({
        "action": "update",
        "memory_type": "user_profile",
        "content": (
            "用户要求在回答技术问题时默认使用中文，且偏好专业、全面的回答风格。"
            "具体要求包括：遵循“底层 -> 架构 -> 实现 -> 优化”的递进逻辑，"
            "提供生产级代码（杜绝特例代码，包含错误处理和类型定义），"
            "并包含多方案对比、复杂度分析与最佳实践。"
            "在处理对比任务时，要求深度聚合搜索信息并详细分析优缺点。"
        ),
        "evidence": "用户要求：请专业全面回答，并深度聚合搜索到的信息，给出对比和优缺点。",
        "classification_reason": "用户明确提出长期回答风格偏好。",
        "mode_affinity": ["chat"],
        "tags": ["language_preference", "answer_style", "detail_level"],
        "importance": 0.9,
        "confidence": 1.0,
        "stability": 0.9,
        "target_memory_id": "mem_old_profile",
        "related_memory_ids": [],
    })
    turn_payload = {
        "query_text": "Qwen3 Embedding 8B和4B有什么区别？请专业全面回答，并深度聚合搜索到的信息，给出对比和优缺点。",
        "focus_text": "",
        "user_message": "Qwen3 Embedding 8B和4B有什么区别？请专业全面回答，并深度聚合搜索到的信息，给出对比和优缺点。",
    }
    related = {
        "0": [{
            "memory_id": "mem_old_profile",
            "memory_type": "user_profile",
            "content": "用户要求在回答技术问题时默认使用中文，且偏好专业、全面的回答风格。",
            "evidence": "用户说：以后回答技术问题默认使用中文，并且要专业全面。",
        }]
    }

    constrained = memory_store._constrain_user_evidenced_decision(decision, turn_payload, related)

    assert constrained["action"] == "update"
    assert "默认使用中文" in constrained["content"]
    assert "专业" in constrained["content"]
    assert "全面" in constrained["content"]
    assert "深度聚合" in constrained["content"]
    assert "优缺点" in constrained["content"]
    assert "底层" not in constrained["content"]
    assert "生产级" not in constrained["content"]
    assert "错误处理" not in constrained["content"]
    assert "复杂度" not in constrained["content"]
    assert "content_pruned_to_user_evidence" in constrained["validation_warnings"]


def test_memory_writer_converts_noop_to_cleanup_update() -> None:
    """验证 noop 可被转换成清理旧记忆的 update。"""
    decision = policy_v2.normalize_decision({
        "action": "noop",
        "memory_type": "user_profile",
        "content": "回答技术问题时，默认使用中文，且风格要求专业、全面。",
        "evidence": "用户明确要求：以后回答技术问题默认使用中文，并且要专业全面。",
        "classification_reason": "旧记忆已记录该偏好，无需重复插入。",
        "mode_affinity": ["chat"],
        "tags": ["language_preference", "answer_style", "detail_level"],
        "importance": 0.9,
        "confidence": 1.0,
        "stability": 0.9,
        "target_memory_id": "mem_old_profile",
        "related_memory_ids": [],
    })
    turn_payload = {
        "query_text": "以后回答技术问题默认使用中文，并且要专业全面。",
        "focus_text": "",
        "user_message": "以后回答技术问题默认使用中文，并且要专业全面。",
    }
    related = {
        "0": [{
            "memory_id": "mem_old_profile",
            "memory_type": "user_profile",
            "content": (
                "用户要求在回答技术问题时默认使用中文，且偏好专业、全面的回答风格。"
                "具体要求包括：遵循“底层 -> 架构 -> 实现 -> 优化”的递进逻辑，"
                "提供生产级代码（杜绝特例代码，包含错误处理和类型定义），"
                "并包含多方案对比、复杂度分析与最佳实践。"
            ),
            "evidence": "用户说：以后回答技术问题默认使用中文，并且要专业全面。",
        }]
    }

    constrained = memory_store._constrain_user_evidenced_decision(decision, turn_payload, related)

    assert constrained["action"] == "update"
    assert constrained["target_memory_id"] == "mem_old_profile"
    assert "默认使用中文" in constrained["content"]
    assert "专业" in constrained["content"]
    assert "全面" in constrained["content"]
    assert "底层" not in constrained["content"]
    assert "生产级" not in constrained["content"]
    assert "noop_converted_to_update_for_memory_cleanup" in constrained["validation_warnings"]


def test_memory_writer_update_preserves_related_evidence() -> None:
    """验证更新旧记忆时保留已有相关证据。"""
    decision = policy_v2.normalize_decision({
        "action": "update",
        "memory_type": "user_profile",
        "content": "用户要求在回答技术问题时默认使用中文，且偏好专业、全面的回答风格；在处理对比任务时，要求深度聚合搜索信息并详细分析优缺点。",
        "evidence": "以后回答技术问题默认使用中文，并且要专业全面。",
        "classification_reason": "用户再次确认已有偏好。",
        "mode_affinity": ["chat"],
        "tags": ["language_preference", "answer_style", "detail_level"],
        "importance": 0.9,
        "confidence": 1.0,
        "stability": 0.9,
        "target_memory_id": "mem_old_profile",
        "related_memory_ids": [],
    })
    turn_payload = {
        "query_text": "以后回答技术问题默认使用中文，并且要专业全面。",
        "focus_text": "",
        "user_message": "以后回答技术问题默认使用中文，并且要专业全面。",
    }
    related = {
        "0": [{
            "memory_id": "mem_old_profile",
            "memory_type": "user_profile",
            "content": decision["content"],
            "evidence": "用户在当前轮次要求：请专业全面回答，并深度聚合搜索到的信息，给出对比和优缺点。",
        }]
    }

    constrained = memory_store._constrain_user_evidenced_decision(decision, turn_payload, related)

    assert constrained["action"] == "update"
    assert "深度聚合搜索" in constrained["evidence"]
    assert "默认使用中文" in constrained["evidence"]
    assert "related_evidence_preserved" in constrained["validation_warnings"]


def test_memory_writer_payload_excludes_assistant_answer_and_chat_summary() -> None:
    """验证 writer 输入不会把助手最终答案和摘要当成用户证据。"""
    database = make_memory_database()
    original_db = memory_store.db
    memory_store.db = database
    try:
        database.create_chat_turn(
            turn_id="turn_payload",
            chat_id="chat_payload",
            turn_index=1,
            task_type="chat",
            query_text="以后回答技术问题默认使用中文，并且要专业全面。",
        )
        database.insert_chat_message(
            message_id="msg_payload_user",
            chat_id="chat_payload",
            turn_id="turn_payload",
            role="user",
            content="以后回答技术问题默认使用中文，并且要专业全面。",
            display_content="以后回答技术问题默认使用中文，并且要专业全面。",
        )
        database.insert_chat_message(
            message_id="msg_payload_assistant",
            chat_id="chat_payload",
            turn_id="turn_payload",
            role="assistant",
            content="我将按底层、架构、实现、优化展开。",
            display_content="我将按底层、架构、实现、优化展开。",
        )
        database.complete_chat_turn("turn_payload")
        database.upsert_chat_summary(
            chat_id="chat_payload",
            summary="助手：我将按底层、架构、实现、优化展开。",
            source_turn_index=1,
        )

        payload = memory_store._turn_payload_for_writer(
            {"job_id": "job_payload"},
            database.get_chat_turn("turn_payload"),
        )
    finally:
        memory_store.db = original_db
        database.close()

    assert payload["assistant_final_answer"] == "我将按底层、架构、实现、优化展开。"
    assert "chat_summary" not in payload
    assert payload["user_message"] == "以后回答技术问题默认使用中文，并且要专业全面。"

    decision = policy_v2.normalize_decision({
        "action": "insert",
        "memory_type": "user_profile",
        "content": "用户偏好底层、架构、实现、优化的回答框架。",
        "evidence": "我将按底层、架构、实现、优化展开。",
        "classification_reason": "assistant wording must not become user profile evidence.",
        "mode_affinity": [],
        "tags": ["answer_style"],
        "importance": 0.7,
        "confidence": 0.9,
        "stability": 0.9,
        "target_memory_id": "",
        "related_memory_ids": [],
    })
    constrained = memory_store._constrain_user_evidenced_decision(decision, payload, {})
    assert constrained["action"] == "noop"
    assert "evidence_not_supported_by_current_user_text" in constrained["validation_warnings"]


def test_memory_db_stores_policy_fields() -> None:
    """验证记忆表会保存策略版本、分类原因和 scope 等字段。"""
    database = make_memory_database()
    try:
        database.insert_memory_item(
            memory_id="mem_policy",
            memory_type="user_profile",
            content="用户希望回答专业全面。",
            evidence="以后回答要专业全面",
            classification_reason="长期回答风格偏好。",
            policy_version="memory_writer_skill_v1",
            tags_json='["answer_style","detail_level"]',
        )
        row = database.get_memory_item("mem_policy")
    finally:
        database.close()

    assert row["classification_reason"] == "长期回答风格偏好。"
    assert row["policy_version"] == "memory_writer_skill_v1"


def test_patch_memory_updates_extended_fields() -> None:
    """验证 patch_memory 可更新扩展字段并刷新输出结构。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_upsert = memory_store.upsert_memory
    original_delete = memory_store.delete_memory_vector
    upserted: list[str] = []
    memory_store.db = database
    memory_store.upsert_memory = lambda item: upserted.append(item["memory_id"])
    memory_store.delete_memory_vector = lambda memory_id: None
    try:
        database.insert_memory_item(
            memory_id="mem_patch",
            memory_type="user_profile",
            content="old",
            evidence="old evidence",
            classification_reason="old reason",
        )
        memory = memory_store.patch_memory(
            memory_id="mem_patch",
            content="new content",
            evidence="new evidence",
            classification_reason="new reason",
            tags=["answer_style"],
            importance=0.9,
            confidence=1.0,
            stability=0.8,
        )
    finally:
        memory_store.db = original_db
        memory_store.upsert_memory = original_upsert
        memory_store.delete_memory_vector = original_delete
        database.close()

    assert memory["content"] == "new content"
    assert memory["evidence"] == "new evidence"
    assert memory["classification_reason"] == "new reason"
    assert memory["importance"] == 0.9
    assert memory["confidence"] == 1.0
    assert memory["stability"] == 0.8
    assert memory["tags"] == ["answer_style"]
    assert upserted == ["mem_patch"]


def test_memory_update_decision_refreshes_source_fields() -> None:
    """验证 update 决策会刷新来源 turn/message 和策略字段。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_upsert = memory_store.upsert_memory
    original_delete = memory_store.delete_memory_vector
    memory_store.db = database
    memory_store.upsert_memory = lambda item: None
    memory_store.delete_memory_vector = lambda memory_id: None
    try:
        database.insert_memory_item(
            memory_id="mem_source",
            memory_type="user_profile",
            content="old",
            evidence="old evidence",
            classification_reason="old reason",
        )
        applied = memory_store._apply_decision(
            {
                "action": "update",
                "memory_type": "user_profile",
                "content": "new",
                "evidence": "new evidence",
                "classification_reason": "new reason",
                "mode_affinity": ["chat"],
                "tags": ["answer_style"],
                "importance": 0.9,
                "confidence": 1.0,
                "stability": 0.9,
                "target_memory_id": "mem_source",
                "related_memory_ids": [],
            },
            {
                "chat_id": "chat_new",
                "turn_id": "turn_new",
                "source_message_ids": ["msg_user"],
            },
        )
        row = database.get_memory_item("mem_source")
    finally:
        memory_store.db = original_db
        memory_store.upsert_memory = original_upsert
        memory_store.delete_memory_vector = original_delete
        database.close()

    assert applied == {"action": "update", "memory_id": "mem_source"}
    assert row["source_chat_id"] == "chat_new"
    assert row["source_turn_id"] == "turn_new"
    assert row["source_message_ids_json"] == '["msg_user"]'


def test_task_state_insert_is_chat_scoped() -> None:
    """验证 task_state 插入会绑定当前 chat scope。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_upsert = memory_store.upsert_memory
    original_delete = memory_store.delete_memory_vector
    upserted: list[str] = []
    memory_store.db = database
    memory_store.upsert_memory = lambda item: upserted.append(item["memory_id"])
    memory_store.delete_memory_vector = lambda memory_id: None
    try:
        applied = memory_store._apply_decision(
            {
                "action": "insert",
                "memory_type": "task_state",
                "content": "下一步补 Memory Job 调试面板。",
                "evidence": "下一步补 Memory Job 调试面板。",
                "classification_reason": "当前 chat 内的短期待办。",
                "mode_affinity": [],
                "tags": ["next_step", "todo"],
                "importance": 0.7,
                "confidence": 1.0,
                "stability": 0.5,
                "target_memory_id": "",
                "related_memory_ids": [],
                "task_status": "open",
                "task_updated_by": "user",
            },
            {
                "chat_id": "chat_task_a",
                "turn_id": "turn_task_a",
                "source_message_ids": ["msg_user"],
            },
        )
        row = database.get_memory_item(applied["memory_id"])
    finally:
        memory_store.db = original_db
        memory_store.upsert_memory = original_upsert
        memory_store.delete_memory_vector = original_delete
        database.close()

    assert applied["action"] == "insert"
    assert row["scope"] == "chat"
    assert row["scope_chat_id"] == "chat_task_a"
    assert row["task_status"] == "open"
    assert row["task_updated_by"] == "user"
    assert upserted == [applied["memory_id"]]


def test_task_state_retrieval_is_chat_scoped_and_active_only() -> None:
    """验证 task_state 召回只读取当前 chat 的活跃任务。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_search_memories = memory_store.search_memories
    memory_store.db = database
    memory_store.search_memories = lambda query_text, filters, top_k=5: []
    try:
        database.insert_memory_item(
            memory_id="mem_user_profile",
            memory_type="user_profile",
            content="用户偏好中文回答。",
            evidence="用户说默认中文。",
            classification_reason="长期偏好。",
            tags_json='["language_preference"]',
        )
        database.insert_memory_item(
            memory_id="mem_task_a_open",
            memory_type="task_state",
            scope="chat",
            scope_chat_id="chat_a",
            content="当前任务是补 Memory Job 调试面板。",
            evidence="下一步补 Memory Job 调试面板。",
            classification_reason="当前任务。",
            tags_json='["todo"]',
            task_status="open",
            task_updated_by="user",
        )
        database.insert_memory_item(
            memory_id="mem_task_a_done",
            memory_type="task_state",
            scope="chat",
            scope_chat_id="chat_a",
            content="旧任务已完成。",
            evidence="assistant 表示任务已完成。",
            classification_reason="已完成任务。",
            tags_json='["todo"]',
            task_status="done",
            task_updated_by="assistant",
        )
        database.insert_memory_item(
            memory_id="mem_task_b_open",
            memory_type="task_state",
            scope="chat",
            scope_chat_id="chat_b",
            content="另一个 chat 的任务。",
            evidence="chat b task。",
            classification_reason="其他 chat 任务。",
            tags_json='["todo"]',
            task_status="open",
            task_updated_by="user",
        )
        messages, stats = memory_store.retrieve_memory_context(
            query_text="",
            focus_text="",
            task_type="chat",
            use_current_page=False,
            use_web_search=False,
            chat_id="chat_a",
        )
    finally:
        memory_store.db = original_db
        memory_store.search_memories = original_search_memories
        database.close()

    injected = "\n".join(message["content"] for message in messages)
    assert "补 Memory Job 调试面板" in injected
    assert "任务状态：open" in injected
    assert "用户偏好中文回答" in injected
    assert "旧任务已完成" not in injected
    assert "另一个 chat 的任务" not in injected
    assert stats["memory_types"] == ["task_state", "user_profile"]


def test_task_state_cross_chat_update_noops() -> None:
    """验证 task_state 不能跨 chat 更新。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_upsert = memory_store.upsert_memory
    original_delete = memory_store.delete_memory_vector
    memory_store.db = database
    memory_store.upsert_memory = lambda item: None
    memory_store.delete_memory_vector = lambda memory_id: None
    try:
        database.insert_memory_item(
            memory_id="mem_task_chat_a",
            memory_type="task_state",
            scope="chat",
            scope_chat_id="chat_a",
            content="chat a task",
            evidence="chat a evidence",
            classification_reason="task",
            task_status="open",
            task_updated_by="user",
        )
        applied = memory_store._apply_decision(
            {
                "action": "update",
                "memory_type": "task_state",
                "content": "chat b update",
                "evidence": "chat b evidence",
                "classification_reason": "cross chat should fail",
                "mode_affinity": [],
                "tags": ["todo"],
                "importance": 0.7,
                "confidence": 1.0,
                "stability": 0.5,
                "target_memory_id": "mem_task_chat_a",
                "related_memory_ids": [],
                "task_status": "reopened",
                "task_updated_by": "user",
            },
            {
                "chat_id": "chat_b",
                "turn_id": "turn_b",
                "source_message_ids": ["msg_b"],
            },
        )
        row = database.get_memory_item("mem_task_chat_a")
    finally:
        memory_store.db = original_db
        memory_store.upsert_memory = original_upsert
        memory_store.delete_memory_vector = original_delete
        database.close()

    assert applied == {"action": "noop", "reason": "task_state_cross_chat_target"}
    assert row["content"] == "chat a task"
    assert row["task_status"] == "open"


def test_chat_turn_stores_plan_execution_metadata() -> None:
    """验证 chat_turn 可保存计划自动执行来源字段。"""
    database = make_memory_database()
    try:
        database.create_chat_turn(
            turn_id="turn_plan_exec",
            chat_id="chat_plan_exec",
            turn_index=1,
            task_type="chat",
            query_text="execute plan",
            origin="plan_auto_execution",
            synthetic_user=True,
            plan_id="plan_exec",
        )
        row = database.get_chat_turn("turn_plan_exec")
    finally:
        database.close()

    assert row["origin"] == "plan_auto_execution"
    assert row["synthetic_user"] == 1
    assert row["plan_id"] == "plan_exec"


def test_synthetic_plan_execution_writer_skips_memory_decisions() -> None:
    """验证计划自动执行产生的合成 turn 会跳过 writer 决策。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_call_writer = memory_store._call_writer_json
    memory_store.db = database

    def fail_if_called(*args, **kwargs):
        """如果 writer 被错误调用则让测试失败。"""
        raise AssertionError("writer model should not be called for synthetic plan execution")

    memory_store._call_writer_json = fail_if_called
    try:
        database.create_chat_turn(
            turn_id="turn_synthetic_plan",
            chat_id="chat_synthetic_plan",
            turn_index=1,
            task_type="chat",
            query_text="execute approved plan",
            origin="plan_auto_execution",
            synthetic_user=True,
            plan_id="plan_synthetic",
        )
        database.insert_chat_message(
            message_id="msg_synthetic_user",
            chat_id="chat_synthetic_plan",
            turn_id="turn_synthetic_plan",
            role="user",
            content="execute approved plan",
            display_content="execute approved plan",
        )
        database.insert_chat_message(
            message_id="msg_synthetic_assistant",
            chat_id="chat_synthetic_plan",
            turn_id="turn_synthetic_plan",
            role="assistant",
            content="completed report",
            display_content="completed report",
        )
        database.complete_chat_turn("turn_synthetic_plan")
        database.create_memory_extraction_job(
            job_id="job_synthetic_plan",
            chat_id="chat_synthetic_plan",
            turn_id="turn_synthetic_plan",
        )

        output = memory_store.run_memory_extraction_job("job_synthetic_plan")
        job = database.get_memory_extraction_job("job_synthetic_plan")
        memories = database.list_memory_items(status="active", limit=10)
    finally:
        memory_store.db = original_db
        memory_store._call_writer_json = original_call_writer
        database.close()

    assert output["applied"] == [{"action": "noop", "reason": "skipped_by_origin"}]
    assert output["validation_warnings"] == ["skipped_by_origin"]
    assert output["plan_id"] == "plan_synthetic"
    assert job["status"] == "complete"
    assert memories == []


def test_task_state_update_preserves_existing_plan_id_when_turn_has_none() -> None:
    """验证无 plan_id 的后续更新不会清空旧 task_state 的 plan_id。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_upsert = memory_store.upsert_memory
    original_delete = memory_store.delete_memory_vector
    memory_store.db = database
    memory_store.upsert_memory = lambda item: None
    memory_store.delete_memory_vector = lambda memory_id: None
    try:
        database.insert_memory_item(
            memory_id="mem_task_plan",
            memory_type="task_state",
            scope="chat",
            scope_chat_id="chat_plan",
            content="old task",
            evidence="old evidence",
            classification_reason="task",
            task_status="open",
            task_updated_by="user",
            plan_id="plan_existing",
        )
        applied = memory_store._apply_decision(
            {
                "action": "update",
                "memory_type": "task_state",
                "content": "updated task",
                "evidence": "updated evidence",
                "classification_reason": "task update",
                "mode_affinity": [],
                "tags": ["todo"],
                "importance": 0.7,
                "confidence": 1.0,
                "stability": 0.5,
                "target_memory_id": "mem_task_plan",
                "related_memory_ids": [],
                "task_status": "done",
                "task_updated_by": "assistant",
            },
            {
                "chat_id": "chat_plan",
                "turn_id": "turn_plan",
                "source_message_ids": ["msg_plan"],
                "plan_id": "",
            },
        )
        row = database.get_memory_item("mem_task_plan")
    finally:
        memory_store.db = original_db
        memory_store.upsert_memory = original_upsert
        memory_store.delete_memory_vector = original_delete
        database.close()

    assert applied == {"action": "update", "memory_id": "mem_task_plan"}
    assert row["plan_id"] == "plan_existing"
    assert row["task_status"] == "done"


def test_task_state_update_rejects_cross_plan_target() -> None:
    """验证计划执行不能更新其他计划绑定的 task_state。"""
    database = make_memory_database()
    original_db = memory_store.db
    original_upsert = memory_store.upsert_memory
    original_delete = memory_store.delete_memory_vector
    memory_store.db = database
    memory_store.upsert_memory = lambda item: None
    memory_store.delete_memory_vector = lambda memory_id: None
    try:
        database.insert_memory_item(
            memory_id="mem_task_plan_a",
            memory_type="task_state",
            scope="chat",
            scope_chat_id="chat_plan",
            content="plan a task",
            evidence="plan a evidence",
            classification_reason="task",
            task_status="open",
            task_updated_by="user",
            plan_id="plan_a",
        )
        applied = memory_store._apply_decision(
            {
                "action": "update",
                "memory_type": "task_state",
                "content": "plan b task",
                "evidence": "plan b evidence",
                "classification_reason": "cross plan should fail",
                "mode_affinity": [],
                "tags": ["todo"],
                "importance": 0.7,
                "confidence": 1.0,
                "stability": 0.5,
                "target_memory_id": "mem_task_plan_a",
                "related_memory_ids": [],
                "task_status": "done",
                "task_updated_by": "assistant",
            },
            {
                "chat_id": "chat_plan",
                "turn_id": "turn_plan_b",
                "source_message_ids": ["msg_plan_b"],
                "plan_id": "plan_b",
            },
        )
        row = database.get_memory_item("mem_task_plan_a")
    finally:
        memory_store.db = original_db
        memory_store.upsert_memory = original_upsert
        memory_store.delete_memory_vector = original_delete
        database.close()

    assert applied == {"action": "noop", "reason": "task_state_cross_plan_target"}
    assert row["content"] == "plan a task"
    assert row["task_status"] == "open"


def test_task_state_can_use_assistant_evidence_and_reopen() -> None:
    """验证 task_state 可使用助手证据完成，也可被用户证据重新打开。"""
    done_decision = policy_v2.normalize_decision({
        "action": "update",
        "memory_type": "task_state",
        "content": "任务已完成。",
        "evidence": "已完成 Memory Job 调试面板。",
        "classification_reason": "assistant 标记任务完成。",
        "mode_affinity": [],
        "tags": ["todo"],
        "importance": 0.7,
        "confidence": 1.0,
        "stability": 0.5,
        "target_memory_id": "mem_task",
        "related_memory_ids": [],
        "task_status": "done",
        "task_updated_by": "assistant",
    })
    done_payload = {
        "chat_id": "chat_task",
        "query_text": "",
        "focus_text": "",
        "user_message": "",
        "assistant_final_answer": "已完成 Memory Job 调试面板。",
    }
    related = {
        "0": [{
            "memory_id": "mem_task",
            "memory_type": "task_state",
            "scope": "chat",
            "scope_chat_id": "chat_task",
            "content": "下一步补 Memory Job 调试面板。",
            "evidence": "下一步补 Memory Job 调试面板。",
        }]
    }

    constrained_done = memory_store._constrain_task_state_decision(done_decision, done_payload, related)

    reopen_decision = policy_v2.normalize_decision({
        "action": "update",
        "memory_type": "task_state",
        "content": "Memory Job 调试面板没有完成，需要继续修。",
        "evidence": "不对，这个还没完成，继续修。",
        "classification_reason": "用户纠正 assistant 的完成判断。",
        "mode_affinity": [],
        "tags": ["todo"],
        "importance": 0.8,
        "confidence": 1.0,
        "stability": 0.5,
        "target_memory_id": "mem_task",
        "related_memory_ids": [],
        "task_status": "reopened",
        "task_updated_by": "user",
    })
    reopen_payload = {
        "chat_id": "chat_task",
        "query_text": "不对，这个还没完成，继续修。",
        "focus_text": "",
        "user_message": "不对，这个还没完成，继续修。",
        "assistant_final_answer": "",
    }
    constrained_reopen = memory_store._constrain_task_state_decision(reopen_decision, reopen_payload, related)

    assert constrained_done["action"] == "update"
    assert constrained_done["task_status"] == "done"
    assert "related_evidence_preserved" in constrained_done["validation_warnings"]
    assert constrained_reopen["action"] == "update"
    assert constrained_reopen["task_status"] == "reopened"
    assert "还没完成" in constrained_reopen["evidence"]


def test_latest_memory_jobs_by_turn_ids() -> None:
    """验证可按 turn_id 批量读取最新 memory writer job。"""
    database = make_memory_database()
    try:
        database.create_memory_extraction_job(
            job_id="job_old",
            chat_id="chat_jobs",
            turn_id="turn_jobs",
        )
        database.create_memory_extraction_job(
            job_id="job_new",
            chat_id="chat_jobs",
            turn_id="turn_jobs",
        )
        jobs = database.list_latest_memory_jobs_by_turn_ids(["turn_jobs"])
    finally:
        database.close()

    assert jobs["turn_jobs"]["job_id"] == "job_new"


def test_memory_api_include_debug_returns_latest_job_summary() -> None:
    """验证记忆 API 调试模式返回最近 job 摘要。"""
    database = make_memory_database()
    original_db = memory_api.db
    memory_api.db = database
    try:
        database.insert_memory_item(
            memory_id="mem_debug",
            memory_type="user_profile",
            content="用户偏好中文回答。",
            evidence="用户说默认中文。",
            classification_reason="长期偏好。",
            source_turn_id="turn_debug",
        )
        database.create_memory_extraction_job(
            job_id="job_debug",
            chat_id="chat_debug",
            turn_id="turn_debug",
        )
        database.update_memory_extraction_job(
            "job_debug",
            status="complete",
            output_json=policy_v2.json_dumps({
                "decisions": [{"action": "insert", "memory_type": "user_profile"}],
                "applied": [{"action": "insert", "memory_id": "mem_debug"}],
                "validation_warnings": ["sample_warning"],
            }),
            completed=True,
        )
        result = memory_api.list_memories(
            status="active",
            memory_type="",
            limit=100,
            include_debug=True,
        )
    finally:
        memory_api.db = original_db
        database.close()

    latest_job = result["memories"][0]["debug"]["latest_job"]
    assert latest_job["job_id"] == "job_debug"
    assert latest_job["status"] == "complete"
    assert latest_job["validation_warnings"] == ["sample_warning"]
    assert latest_job["applied"] == [{"action": "insert", "memory_id": "mem_debug"}]


def test_chat_history_uses_first_title_and_soft_delete_hides_chat() -> None:
    """验证聊天历史使用首个标题，软删除后不再返回。"""
    database = make_memory_database()
    try:
        database.create_chat_turn(
            turn_id="turn_1",
            chat_id="chat_history",
            turn_index=1,
            task_type="chat",
            query_text="first",
        )
        database.insert_chat_message(
            message_id="msg_1",
            chat_id="chat_history",
            turn_id="turn_1",
            role="user",
            content="first",
            display_content="first",
        )
        database.complete_chat_turn(turn_id="turn_1")
        database.insert_turn_summary(
            summary_id="summary_1",
            chat_id="chat_history",
            turn_id="turn_1",
            title="First title",
            summary="First summary",
        )

        database.create_chat_turn(
            turn_id="turn_2",
            chat_id="chat_history",
            turn_index=2,
            task_type="chat",
            query_text="second",
        )
        database.insert_chat_message(
            message_id="msg_2",
            chat_id="chat_history",
            turn_id="turn_2",
            role="user",
            content="second",
            display_content="second",
        )
        database.complete_chat_turn(turn_id="turn_2")
        database.insert_turn_summary(
            summary_id="summary_2",
            chat_id="chat_history",
            turn_id="turn_2",
            title="Second title",
            summary="Second summary",
        )

        chats = database.list_chats_with_latest_summary()
        deleted = database.soft_delete_chat("chat_history")
        chats_after_delete = database.list_chats_with_latest_summary()
        replay_messages = database.list_chat_messages("chat_history")
        display_messages = database.list_chat_display_messages("chat_history")
        deleted_again = database.soft_delete_chat("chat_history")
    finally:
        database.close()

    assert chats[0]["title"] == "First title"
    assert chats[0]["latest_summary"] == "Second summary"
    assert chats[0]["turn_count"] == 2
    assert deleted is True
    assert chats_after_delete == []
    assert replay_messages == []
    assert display_messages == []
    assert deleted_again is False


def test_memory_retrieval_mapping_includes_new_types() -> None:
    """验证不同任务模式的记忆类型映射包含新增类型。"""
    assert "procedural_feedback" in policy_v2.memory_types_for_mode("web_search")
    assert "external_knowledge_ref" in policy_v2.memory_types_for_mode("web_search")
    assert "project_state" not in policy_v2.memory_types_for_mode("translate")
    assert "episodic_lesson" in policy_v2.memory_types_for_mode("page_rag")


if __name__ == "__main__":
    test_failed_turn_is_not_replayed()
    test_translate_memory_retrieval_excludes_project_state()
    test_memory_policy_classifies_six_types()
    test_memory_policy_keeps_llm_type_without_keyword_coercion()
    test_memory_policy_corrects_type_only_from_tag_taxonomy()
    test_memory_policy_noops_on_conflicting_tag_taxonomy()
    test_memory_policy_rejects_invalid_decisions()
    test_memory_policy_sanitizes_internal_ids_from_display_reason()
    test_memory_writer_prunes_user_profile_to_user_evidence()
    test_memory_writer_converts_noop_to_cleanup_update()
    test_memory_writer_update_preserves_related_evidence()
    test_memory_writer_payload_excludes_assistant_answer_and_chat_summary()
    test_memory_db_stores_policy_fields()
    test_patch_memory_updates_extended_fields()
    test_memory_update_decision_refreshes_source_fields()
    test_task_state_insert_is_chat_scoped()
    test_task_state_retrieval_is_chat_scoped_and_active_only()
    test_task_state_cross_chat_update_noops()
    test_chat_turn_stores_plan_execution_metadata()
    test_synthetic_plan_execution_writer_skips_memory_decisions()
    test_task_state_update_preserves_existing_plan_id_when_turn_has_none()
    test_task_state_update_rejects_cross_plan_target()
    test_task_state_can_use_assistant_evidence_and_reopen()
    test_latest_memory_jobs_by_turn_ids()
    test_memory_api_include_debug_returns_latest_job_summary()
    test_chat_history_uses_first_title_and_soft_delete_hides_chat()
    test_memory_retrieval_mapping_includes_new_types()
    print("PASS memory flow")
