"""有状态聊天请求链路测试。"""

import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import api.chat as chat


class FakeDB:
    """记录聊天链路写库调用的轻量假数据库。"""

    def __init__(self) -> None:
        """初始化所有调用记录容器。"""
        self.created_turns = []
        self.messages = []
        self.completed_turns = []
        self.failed_turns = []
        self.summaries = []

    def list_chat_messages(self, chat_id: str) -> list[dict[str, str]]:
        """返回一组固定历史消息，验证历史只进上下文不进搜索词。"""
        return [
            {"role": "user", "content": "历史用户问题"},
            {"role": "assistant", "content": "历史助手回答 SHOULD_NOT_BE_IN_SEARCH"},
        ]

    def next_turn_index(self, chat_id: str) -> int:
        """模拟下一轮 turn_index。"""
        return 2

    def create_chat_turn(self, **kwargs) -> None:
        """记录 create_chat_turn 调用参数。"""
        self.created_turns.append(kwargs)

    def insert_chat_message(self, **kwargs) -> None:
        """记录 insert_chat_message 调用参数。"""
        self.messages.append(kwargs)

    def complete_chat_turn(self, **kwargs) -> None:
        """记录 complete_chat_turn 调用参数。"""
        self.completed_turns.append(kwargs)

    def fail_chat_turn(self, *args, **kwargs) -> None:
        """记录 fail_chat_turn 调用参数。"""
        self.failed_turns.append((args, kwargs))

    def insert_turn_summary(self, **kwargs) -> None:
        """记录 turn 摘要写入参数。"""
        self.summaries.append(kwargs)


def test_stateful_message_uses_db_history_and_current_search_query() -> None:
    """验证有状态构造会加载 DB 历史，并只用当前检索词做联网搜索。"""
    original_db = chat.db
    original_search_web = chat.search_web
    original_fetch_url = chat.fetch_url
    original_retrieve_web_context = chat.retrieve_web_context
    original_retrieve_memory_context = chat.retrieve_memory_context
    original_plan_current_turn = chat.plan_current_turn
    fake_db = FakeDB()

    def fake_search_web(query: str, top_k: int) -> dict:
        """断言搜索词没有混入历史助手回答。"""
        assert "SHOULD_NOT_BE_IN_SEARCH" not in query
        assert "当前检索" in query
        return {
            "results": [
                {"title": "Result", "url": "https://example.com/a", "snippet": "snippet"}
            ],
            "unresponsive_engines": [],
        }

    def fake_fetch_url(url: str) -> dict:
        """返回固定抓取正文。"""
        return {
            "title": "Fetched",
            "final_url": url,
            "content": "fetched content",
            "content_length": 15,
        }

    def fake_retrieve_web_context(query: str, results: list[dict], top_k_results: int, top_k_chunks: int) -> dict:
        """返回固定联网召回结果。"""
        return {
            "results": [
                {
                    **results[0],
                    "matches": [
                        {
                            "title": "Fetched",
                            "url": "https://example.com/a",
                            "preview": "preview",
                            "content": "matched content",
                            "score": 0.9,
                            "source_key": "web:0:0",
                        }
                    ],
                }
            ],
            "retrieved_page_count": 1,
            "retrieved_chunk_count": 1,
        }

    chat.db = fake_db
    chat.search_web = fake_search_web
    chat.fetch_url = fake_fetch_url
    chat.retrieve_web_context = fake_retrieve_web_context
    def fake_retrieve_memory_context(**kwargs):
        """断言记忆召回使用规划后的 information_need。"""
        assert kwargs["query_text"] == "PLANNED_INFORMATION_NEED"
        return [], {"memory_retrieved_count": 0, "memory_ids": [], "memory_types": []}

    chat.retrieve_memory_context = fake_retrieve_memory_context
    chat.plan_current_turn = lambda **kwargs: (
        {
            "information_need": "PLANNED_INFORMATION_NEED",
            "answer_constraints": "ANSWER_CONSTRAINT",
            "memory_candidate_hint": True,
        },
        {
            "query_planner_used": True,
            "query_planner_error": "",
            "planned_information_need": "PLANNED_INFORMATION_NEED",
            "answer_constraints_length": len("ANSWER_CONSTRAINT"),
            "memory_candidate_hint": True,
        },
    )
    try:
        item = chat.Chat(
            model="test-model",
            chat_id="chat_stateful",
            current_turn=chat.CurrentTurn(task_type="chat", query_text="当前问题"),
            context_options=chat.ContextOptions(use_web_search=True, web_search_query="当前检索"),
        )
        resolved = chat.resolve_chat_request(item)
        messages, sources, task_type, stats, turn_state = chat.build_stateful_message(item, resolved)
    finally:
        chat.db = original_db
        chat.search_web = original_search_web
        chat.fetch_url = original_fetch_url
        chat.retrieve_web_context = original_retrieve_web_context
        chat.retrieve_memory_context = original_retrieve_memory_context
        chat.plan_current_turn = original_plan_current_turn

    assert task_type == "chat"
    assert messages[0]["role"] == "system"
    assert messages[-1] == {"role": "user", "content": "当前问题"}
    assert any(message["content"] == "历史助手回答 SHOULD_NOT_BE_IN_SEARCH" for message in messages)
    assert len(sources) == 1
    assert stats["history_from_db"] is True
    assert stats["loaded_history_message_count"] == 2
    assert stats["saved_user_message"] is True
    assert stats["query_planner_used"] is True
    assert stats["planned_information_need"] == "PLANNED_INFORMATION_NEED"
    assert stats["answer_constraints_length"] == len("ANSWER_CONSTRAINT")
    assert stats["memory_candidate_hint"] is True
    assert any("ANSWER_CONSTRAINT" in message["content"] for message in messages)
    assert stats["web_search_query"] == "当前检索"
    assert fake_db.created_turns[0]["turn_index"] == 2
    assert fake_db.created_turns[0]["web_search_query"] == "当前检索"
    assert fake_db.messages[0]["role"] == "user"
    assert turn_state["chat_id"] == "chat_stateful"


def test_stateful_query_planner_removes_answer_constraints_from_web_search() -> None:
    """验证查询规划器会把回答风格约束从联网搜索词中剥离。"""
    original_db = chat.db
    original_search_web = chat.search_web
    original_fetch_url = chat.fetch_url
    original_retrieve_web_context = chat.retrieve_web_context
    original_retrieve_memory_context = chat.retrieve_memory_context
    original_plan_current_turn = chat.plan_current_turn
    fake_db = FakeDB()

    def fake_search_web(query: str, top_k: int) -> dict:
        """断言联网搜索只包含信息需求。"""
        assert query == "Qwen3 Embedding 8B 4B comparison pros cons"
        assert "professional" not in query
        assert "comprehensive" not in query
        return {
            "results": [
                {"title": "Result", "url": "https://example.com/qwen", "snippet": "snippet"}
            ],
            "unresponsive_engines": [],
        }

    def fake_fetch_url(url: str) -> dict:
        """返回固定抓取正文。"""
        return {
            "title": "Fetched",
            "final_url": url,
            "content": "fetched content",
            "content_length": 15,
        }

    def fake_retrieve_web_context(query: str, results: list[dict], top_k_results: int, top_k_chunks: int) -> dict:
        """断言召回阶段也使用规划后的搜索词。"""
        assert query == "Qwen3 Embedding 8B 4B comparison pros cons"
        return {
            "results": [
                {
                    **results[0],
                    "matches": [
                        {
                            "title": "Fetched",
                            "url": "https://example.com/qwen",
                            "preview": "preview",
                            "content": "matched content",
                            "score": 0.9,
                            "source_key": "web:0:0",
                        }
                    ],
                }
            ],
            "retrieved_page_count": 1,
            "retrieved_chunk_count": 1,
        }

    chat.db = fake_db
    chat.search_web = fake_search_web
    chat.fetch_url = fake_fetch_url
    chat.retrieve_web_context = fake_retrieve_web_context
    chat.retrieve_memory_context = lambda **kwargs: (
        [],
        {"memory_retrieved_count": 0, "memory_ids": [], "memory_types": []},
    )
    chat.plan_current_turn = lambda **kwargs: (
        {
            "information_need": "Qwen3 Embedding 8B 4B comparison pros cons",
            "answer_constraints": "professional comprehensive answer",
            "memory_candidate_hint": False,
        },
        {
            "query_planner_used": True,
            "query_planner_error": "",
            "planned_information_need": "Qwen3 Embedding 8B 4B comparison pros cons",
            "answer_constraints_length": len("professional comprehensive answer"),
            "memory_candidate_hint": False,
        },
    )
    try:
        item = chat.Chat(
            model="test-model",
            chat_id="chat_stateful",
            current_turn=chat.CurrentTurn(
                task_type="chat",
                query_text=(
                    "Qwen3 Embedding 8B and 4B difference? "
                    "Please answer professionally and comprehensively."
                ),
            ),
            context_options=chat.ContextOptions(
                use_web_search=True,
                web_search_query=(
                    "Qwen3 Embedding 8B and 4B difference? "
                    "Please answer professionally and comprehensively."
                ),
            ),
        )
        resolved = chat.resolve_chat_request(item)
        messages, sources, task_type, stats, turn_state = chat.build_stateful_message(item, resolved)
    finally:
        chat.db = original_db
        chat.search_web = original_search_web
        chat.fetch_url = original_fetch_url
        chat.retrieve_web_context = original_retrieve_web_context
        chat.retrieve_memory_context = original_retrieve_memory_context
        chat.plan_current_turn = original_plan_current_turn

    assert task_type == "chat"
    assert len(sources) == 1
    assert stats["web_search_query"] == "Qwen3 Embedding 8B 4B comparison pros cons"
    assert stats["web_search_query_auto_ignored"] is True
    assert stats["planned_information_need"] == "Qwen3 Embedding 8B 4B comparison pros cons"
    assert any("professional comprehensive answer" in message["content"] for message in messages)
    assert fake_db.created_turns[0]["query_text"].endswith("professionally and comprehensively.")
    assert fake_db.created_turns[0]["web_search_query"] == "Qwen3 Embedding 8B 4B comparison pros cons"
    assert turn_state["user_content"].endswith("professionally and comprehensively.")


def test_persist_successful_turn_saves_clean_content_and_summary() -> None:
    """验证成功 turn 会保存去引用正文、展示正文、sources 和摘要。"""
    original_db = chat.db
    original_create_memory_extraction_job = chat.create_memory_extraction_job
    original_start_memory_writer = chat.start_memory_writer
    original_update_chat_summary_rule = chat.update_chat_summary_rule
    fake_db = FakeDB()
    chat.db = fake_db
    chat.create_memory_extraction_job = lambda turn_state, final_text, sources: "memjob_test"
    chat.start_memory_writer = lambda job_id: None
    chat.update_chat_summary_rule = lambda **kwargs: None
    try:
        turn_state = {
            "turn_id": "turn_1",
            "chat_id": "chat_1",
            "user_content": "什么是 RAG？",
            "page_context_id": "",
            "page_url": "",
            "page_title": "",
        }
        final_trace = {
            "status": "ok",
            "retrieval_query": "",
            "web_search_query": "RAG 定义",
            "snapshot_id": "",
        }
        chat.persist_successful_turn(
            turn_state,
            "RAG 是检索增强生成。 [S1]",
            [{"source_id": "S1", "url": "https://example.com", "title": "Example"}],
            [{"source_kind": "web_search"}],
            final_trace,
        )
    finally:
        chat.db = original_db
        chat.create_memory_extraction_job = original_create_memory_extraction_job
        chat.start_memory_writer = original_start_memory_writer
        chat.update_chat_summary_rule = original_update_chat_summary_rule

    assistant_message = fake_db.messages[0]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "RAG 是检索增强生成。"
    assert assistant_message["display_content"] == "RAG 是检索增强生成。 [S1]"
    assert json.loads(assistant_message["sources_json"])[0]["source_id"] == "S1"
    assert fake_db.completed_turns[0]["web_search_query"] == "RAG 定义"
    assert json.loads(fake_db.completed_turns[0]["source_kinds_json"]) == ["web_search"]
    assert fake_db.summaries[0]["title"] == "什么是 RAG？"


def test_rule_summary_title_prefers_planned_information_need() -> None:
    """验证聊天标题优先使用规划后的信息需求，并正确处理翻译任务。"""
    title, _ = chat.build_rule_summary(
        "请专业全面回答：Qwen3 Embedding 8B和4B有什么区别？",
        "answer",
        task_type="chat",
        query_text="请专业全面回答：Qwen3 Embedding 8B和4B有什么区别？",
        planned_information_need="Qwen3 Embedding 8B和4B的区别",
    )
    translate_title, _ = chat.build_rule_summary(
        "任务：translate\n待翻译文本：Retrieval-Augmented Generation improves grounding.\n补充要求：专业自然",
        "answer",
        task_type="translate",
        query_text="专业自然",
        focus_text="Retrieval-Augmented Generation improves grounding.",
    )

    assert title == "Qwen3 Embedding 8B和4B的区别"
    assert translate_title.startswith("翻译：Retrieval-Augmented Generation")
    assert "补充要求" not in translate_title


if __name__ == "__main__":
    test_stateful_message_uses_db_history_and_current_search_query()
    test_stateful_query_planner_removes_answer_constraints_from_web_search()
    test_persist_successful_turn_saves_clean_content_and_summary()
    test_rule_summary_title_prefers_planned_information_need()
    print("PASS stateful chat flow")
