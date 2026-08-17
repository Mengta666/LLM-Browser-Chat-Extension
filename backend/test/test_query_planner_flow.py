"""查询规划器回归测试。"""

import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from memory import query_planner


def test_query_planner_splits_information_need_and_answer_constraints() -> None:
    """验证规划器能把检索需求和回答风格约束拆开。"""
    original_call_planner_json = query_planner._call_planner_json
    original_enabled = query_planner.QUERY_PLANNER_ENABLED
    captured_messages = []

    def fake_call_planner_json(model, messages):
        """模拟规划模型返回拆分后的 JSON。"""
        captured_messages.extend(messages)
        return {
            "information_need": "Qwen3 Embedding 8B和Qwen3 Embedding 4B区别 对比 优缺点",
            "answer_constraints": "从专业角度、深度聚合搜索信息、全面回答",
            "memory_candidate_hint": False,
        }

    query_planner._call_planner_json = fake_call_planner_json
    query_planner.QUERY_PLANNER_ENABLED = True
    try:
        plan, stats = query_planner.plan_current_turn(
            model="test-model",
            task_type="chat",
            query_text=(
                "Qwen3 Embedding 8B和4B区别呢？"
                "你需要从专业角度、深度聚合搜索到的信息，给出全面的回答"
            ),
            focus_text="",
            use_web_search=True,
        )
    finally:
        query_planner._call_planner_json = original_call_planner_json
        query_planner.QUERY_PLANNER_ENABLED = original_enabled

    assert plan["information_need"] == "Qwen3 Embedding 8B和Qwen3 Embedding 4B区别 对比 优缺点"
    assert plan["answer_constraints"] == "从专业角度、深度聚合搜索信息、全面回答"
    assert plan["memory_candidate_hint"] is False
    assert stats["query_planner_used"] is True
    assert stats["query_planner_error"] == ""
    assert stats["answer_constraints_length"] == len(plan["answer_constraints"])
    assert "Do not include answer constraints in information_need" in captured_messages[0]["content"]


def test_query_planner_falls_back_when_model_returns_invalid_json() -> None:
    """验证模型返回无效 JSON 时会回退到默认检索需求。"""
    original_call_planner_json = query_planner._call_planner_json
    original_enabled = query_planner.QUERY_PLANNER_ENABLED

    query_planner._call_planner_json = lambda model, messages: {}
    query_planner.QUERY_PLANNER_ENABLED = True
    try:
        plan, stats = query_planner.plan_current_turn(
            model="test-model",
            task_type="explain",
            query_text="请专业解释",
            focus_text="Qwen3 Embedding 4B",
            use_current_page=True,
        )
    finally:
        query_planner._call_planner_json = original_call_planner_json
        query_planner.QUERY_PLANNER_ENABLED = original_enabled

    assert plan == {
        "information_need": "Qwen3 Embedding 4B\n请专业解释",
        "answer_constraints": "",
        "memory_candidate_hint": False,
    }
    assert stats["query_planner_used"] is False
    assert "empty planner JSON" in stats["query_planner_error"]


def test_query_planner_passes_explicit_web_search_query_to_prompt() -> None:
    """验证显式联网搜索词会进入规划 prompt。"""
    original_call_planner_json = query_planner._call_planner_json
    original_enabled = query_planner.QUERY_PLANNER_ENABLED
    captured_messages = []

    def fake_call_planner_json(model, messages):
        """模拟规划模型识别到长期偏好候选。"""
        captured_messages.extend(messages)
        return {
            "information_need": "raw information need",
            "answer_constraints": "answer style",
            "memory_candidate_hint": True,
        }

    query_planner._call_planner_json = fake_call_planner_json
    query_planner.QUERY_PLANNER_ENABLED = True
    try:
        plan, stats = query_planner.plan_current_turn(
            model="test-model",
            task_type="chat",
            query_text="raw question with future preference",
            focus_text="",
            use_web_search=True,
            explicit_web_search_query="explicit search query",
        )
    finally:
        query_planner._call_planner_json = original_call_planner_json
        query_planner.QUERY_PLANNER_ENABLED = original_enabled

    assert plan["memory_candidate_hint"] is True
    assert stats["query_planner_used"] is True
    assert "explicit search query" in captured_messages[1]["content"]


if __name__ == "__main__":
    test_query_planner_splits_information_need_and_answer_constraints()
    test_query_planner_falls_back_when_model_returns_invalid_json()
    test_query_planner_passes_explicit_web_search_query_to_prompt()
    print("PASS query planner flow")
