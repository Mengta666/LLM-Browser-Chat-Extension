"""查询规划器。

尽力把当前用户输入拆成“用于检索的事实需求”和“只影响回答方式的约束”，
避免把“请专业全面回答”这类风格要求带进联网搜索或向量召回 query。
"""
import json
import os
import re
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv
from openai import OpenAI

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
QUERY_PLANNER_MODEL = os.getenv("QUERY_PLANNER_MODEL", "")
QUERY_PLANNER_ENABLED = os.getenv("QUERY_PLANNER_ENABLED", "1") == "1"
QUERY_PLANNER_MAX_INPUT_CHARS = int(os.getenv("QUERY_PLANNER_MAX_INPUT_CHARS", "2000"))


class QueryPlan(TypedDict):
    """当前轮查询规划结果。"""

    information_need: str
    answer_constraints: str
    memory_candidate_hint: bool


def build_default_query_plan(query_text: str) -> QueryPlan:
    """当规划模型不可用时使用的保守回退计划。"""
    return {
        "information_need": str(query_text or "").strip(),
        "answer_constraints": "",
        "memory_candidate_hint": False,
    }


def _fallback_information_need(task_type: str, query_text: str, focus_text: str) -> str:
    """根据任务类型生成无需模型参与的检索需求。"""
    query = str(query_text or "").strip()
    focus = str(focus_text or "").strip()
    if task_type == "chat":
        return query
    return "\n".join(part for part in [focus, query] if part).strip()


def _extract_first_json_object(text: str) -> dict[str, Any]:
    """从模型输出中容错抽取第一个 JSON 对象。"""
    decoder = json.JSONDecoder()
    value = str(text or "").strip()
    if not value:
        return {}
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    for index, char in enumerate(value):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_dumps(value: Any) -> str:
    """序列化成紧凑 UTF-8 JSON，便于嵌入 prompt。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _truncate(value: str, limit: int) -> str:
    """限制进入规划 prompt 的单字段长度。"""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _normalize_bool(value: Any) -> bool:
    """兼容字符串和布尔值两类模型输出。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _normalize_plan(raw: dict[str, Any], fallback: QueryPlan) -> QueryPlan:
    """校验规划结果，缺少核心字段时回退到默认 information_need。"""
    information_need = str(raw.get("information_need") or "").strip()
    answer_constraints = str(raw.get("answer_constraints") or "").strip()
    if not information_need:
        information_need = fallback["information_need"]
    return {
        "information_need": information_need,
        "answer_constraints": answer_constraints,
        "memory_candidate_hint": _normalize_bool(raw.get("memory_candidate_hint")),
    }


def _build_planner_messages(
        task_type: str,
        query_text: str,
        focus_text: str,
        use_current_page: bool,
        use_web_search: bool,
        explicit_web_search_query: str,
) -> list[dict[str, str]]:
    """构造查询规划模型的输入消息。"""
    payload = {
        "task_type": task_type,
        "query_text": _truncate(query_text, QUERY_PLANNER_MAX_INPUT_CHARS),
        "focus_text": _truncate(focus_text, QUERY_PLANNER_MAX_INPUT_CHARS),
        "use_current_page": use_current_page,
        "use_web_search": use_web_search,
        "explicit_web_search_query": _truncate(explicit_web_search_query, QUERY_PLANNER_MAX_INPUT_CHARS),
    }
    return [
        {
            "role": "system",
            "content": (
                "You split the current user turn into retrieval intent and answer constraints. "
                "Return strict JSON only with keys: information_need, answer_constraints, memory_candidate_hint. "
                "information_need is the factual/search need, including entities, comparison targets, dimensions, "
                "definitions, principles, pros/cons, versions, and required objects. "
                "answer_constraints are response style, language, depth, structure, citation, aggregation, "
                "or long-term preference requirements such as professional, comprehensive, concise, in Chinese, "
                "or future/default behavior. "
                "Do not answer the user. Do not include answer constraints in information_need. "
                "Set memory_candidate_hint true only when the user states a durable future/default preference."
            ),
        },
        {
            "role": "user",
            "content": (
                "Split this turn. Example: "
                '{"query_text":"Qwen3 Embedding 8B和Qwen3 Embedding 4B区别呢？'
                '你需要从专业角度、深度聚合搜索到的信息，给出全面的回答 对比 优缺点",'
                '"focus_text":"","task_type":"chat"} -> '
                '{"information_need":"Qwen3 Embedding 8B和Qwen3 Embedding 4B区别 对比 优缺点",'
                '"answer_constraints":"从专业角度、深度聚合搜索到的信息，给出全面的回答",'
                '"memory_candidate_hint":false}\n\n'
                f"Input: {_json_dumps(payload)}"
            ),
        },
    ]


def _call_planner_json(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    """调用规划模型并解析 JSON 输出。"""
    if not OPENAI_API_KEY or not MODEL_BASE_URL:
        raise RuntimeError("MODEL_BASE_URL or OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=MODEL_BASE_URL)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=False,
    )
    content = response.choices[0].message.content if response.choices else ""
    return _extract_first_json_object(content or "")


def plan_current_turn(
        *,
        model: str,
        task_type: str,
        query_text: str,
        focus_text: str = "",
        use_current_page: bool = False,
        use_web_search: bool = False,
        explicit_web_search_query: str = "",
) -> tuple[QueryPlan, dict[str, Any]]:
    """返回当前轮查询规划结果以及可写入 trace 的统计信息。"""
    fallback = build_default_query_plan(_fallback_information_need(task_type, query_text, focus_text))
    base_stats = {
        "query_planner_used": False,
        "query_planner_error": "",
        "planned_information_need": fallback["information_need"],
        "answer_constraints_length": 0,
        "memory_candidate_hint": False,
    }

    resolved_model = QUERY_PLANNER_MODEL or str(model or "").strip()
    if not QUERY_PLANNER_ENABLED or not resolved_model:
        return fallback, base_stats

    try:
        messages = _build_planner_messages(
            task_type=task_type,
            query_text=query_text,
            focus_text=focus_text,
            use_current_page=use_current_page,
            use_web_search=use_web_search,
            explicit_web_search_query=explicit_web_search_query,
        )
        raw_plan = _call_planner_json(resolved_model, messages)
        if not raw_plan:
            raise ValueError("empty planner JSON")
        plan = _normalize_plan(raw_plan, fallback)
        return plan, {
            "query_planner_used": True,
            "query_planner_error": "",
            "planned_information_need": plan["information_need"],
            "answer_constraints_length": len(plan["answer_constraints"]),
            "memory_candidate_hint": plan["memory_candidate_hint"],
        }
    except Exception as exc:
        return fallback, {
            **base_stats,
            "query_planner_error": str(exc),
        }
