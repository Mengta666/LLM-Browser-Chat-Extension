"""通用工具函数集中处。

收敛此前散落在 chat / store / policy_v2 / query_planner / chats 等模块里
逐字重复的小工具，消除多份定义。注意：plans.py 里的 _extract_first_json_object
是严格解析（失败即抛异常），行为与此处不同，故未纳入，保留在 plans 内。
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4


def json_dumps(value: Any) -> str:
    """统一输出紧凑 UTF-8 JSON，便于写入 SQLite 文本字段。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def make_id(prefix: str) -> str:
    """生成带业务前缀的随机 ID。"""
    return f"{prefix}_{uuid4().hex}"


def safe_json_loads(value: Any, default: Any) -> Any:
    """宽松解析 JSON 字段，无法解析时返回默认值（兼容已是 dict/list 的入参）。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def parse_json_list(value: Any) -> list[Any]:
    """把列表或 JSON 字符串统一转换成列表，失败时返回空列表。"""
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def clamp_float(value: Any, default: float = 0.5) -> float:
    """把 importance/confidence/stability 等分值限制到 0 到 1。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def safe_float(value: Any, default: float = 0.0) -> float:
    """把外部搜索/召回分数转成可比较数值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_first_json_object(text: str) -> dict[str, Any]:
    """从模型自由输出中容错抽取第一个 JSON 对象，找不到时返回空 dict。"""
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
