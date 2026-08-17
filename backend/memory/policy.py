"""记忆读写策略模块。

后续用于判断哪些信息允许写入长期记忆、哪些信息只保留在短期会话里。
这里也会承载敏感信息过滤和用户确认策略。
"""
import json
from typing import Any

ENABLED_MEMORY_TYPES = {
    "user_profile",
    "project_state",
    "procedural_feedback",
}

RESERVED_MEMORY_TYPES = {
    "task_state",
    "episodic_lesson",
    "external_knowledge_ref",
}

ALLOWED_MEMORY_TYPES = ENABLED_MEMORY_TYPES | RESERVED_MEMORY_TYPES
ALLOWED_ACTIONS = {"insert", "update", "supersede", "noop"}

MEMORY_CONTEXT_PROMPT = """以下是用户长期记忆，只用于理解用户背景、偏好和历史决策。
如果与当前用户输入冲突，以当前用户输入为准。
不要把这些记忆当作外部事实来源引用。
除非用户询问历史，不要显式说“根据你的记忆”。
"""

WRITER_SYSTEM_PROMPT = """你是后台 Memory Writer，只负责把已经完成的一轮对话抽取为长期记忆决策。

硬规则：
- 只保存未来会影响回答、规划、代码实现或交互方式的信息。
- 只保存用户明确表达、用户确认的决策、或长期稳定重复模式。
- 不保存网页正文、搜索结果正文、助手未经确认的推测、临时问题、失败 turn、密钥和敏感凭证。
- 新信息与旧记忆冲突时用 update 或 supersede，不新增重复记忆。
- 不确定时输出 noop。

MVP 可写入的 memory_type 只有 user_profile、project_state、procedural_feedback。
必须只输出严格 JSON，不要输出 Markdown。
"""


def json_dumps(value: Any) -> str:
    """用紧凑 UTF-8 JSON 保存结构化字段。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_json_list(value: Any) -> list[Any]:
    """把可能来自 SQLite 的 JSON 文本解析成列表。"""
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
    """把模型给出的权重类字段限制在 0 到 1 之间。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def normalize_memory_row(row: dict[str, Any]) -> dict[str, Any]:
    """把数据库记忆行里的 JSON 文本字段展开成列表。"""
    item = dict(row)
    item["mode_affinity"] = [
        str(value) for value in parse_json_list(item.get("mode_affinity_json"))
        if str(value).strip()
    ]
    item["tags"] = [
        str(value) for value in parse_json_list(item.get("tags_json"))
        if str(value).strip()
    ]
    item["source_message_ids"] = [
        str(value) for value in parse_json_list(item.get("source_message_ids_json"))
        if str(value).strip()
    ]
    return item


def derive_memory_mode(task_type: str, use_current_page: bool, use_web_search: bool) -> str:
    """根据任务类型和上下文开关推导记忆召回模式。"""
    if use_current_page and use_web_search:
        return "combined"
    if use_current_page:
        return "page_rag"
    if use_web_search:
        return "web_search"
    if task_type == "translate":
        return "translate"
    if task_type == "explain":
        return "explain"
    return "chat"


def memory_types_for_mode(mode: str) -> list[str]:
    """返回某个召回模式允许注入的记忆类型。"""
    mapping = {
        "chat": ["user_profile", "project_state", "task_state"],
        "explain": ["user_profile", "project_state"],
        "translate": ["user_profile", "procedural_feedback"],
        "page_rag": ["project_state", "task_state"],
        "web_search": ["user_profile", "project_state"],
        "combined": ["project_state", "task_state", "procedural_feedback"],
    }
    return mapping.get(mode, mapping["chat"])


def normalize_decision(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并清洗 memory writer 输出的单条写入决策。"""
    action = str(raw.get("action") or "noop").strip()
    memory_type = str(raw.get("memory_type") or "").strip()
    if action not in ALLOWED_ACTIONS:
        action = "noop"
    if memory_type not in ENABLED_MEMORY_TYPES:
        memory_type = ""
        if action != "noop":
            action = "noop"

    mode_affinity = [
        str(value).strip()
        for value in parse_json_list(raw.get("mode_affinity"))
        if str(value).strip()
    ]
    tags = [
        str(value).strip()
        for value in parse_json_list(raw.get("tags"))
        if str(value).strip()
    ]
    related_memory_ids = [
        str(value).strip()
        for value in parse_json_list(raw.get("related_memory_ids"))
        if str(value).strip()
    ]

    return {
        "action": action,
        "memory_type": memory_type,
        "content": str(raw.get("content") or "").strip(),
        "evidence": str(raw.get("evidence") or "").strip(),
        "mode_affinity": mode_affinity,
        "tags": tags,
        "importance": clamp_float(raw.get("importance"), 0.5),
        "confidence": clamp_float(raw.get("confidence"), 0.5),
        "stability": clamp_float(raw.get("stability"), 0.5),
        "target_memory_id": str(raw.get("target_memory_id") or "").strip(),
        "related_memory_ids": related_memory_ids,
    }


def normalize_writer_output(value: Any) -> dict[str, Any]:
    """把 memory writer 原始输出标准化为 decisions 数组。"""
    if not isinstance(value, dict):
        return {"decisions": []}
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        return {"decisions": []}
    return {
        "decisions": [
            normalize_decision(item)
            for item in decisions
            if isinstance(item, dict)
        ]
    }


def build_memory_context_messages(memories: list[dict[str, Any]], max_chars: int) -> list[dict[str, str]]:
    """把检索到的记忆压缩成可注入模型上下文的 system 消息。"""
    if not memories:
        return []

    lines: list[str] = []
    used_chars = 0
    for index, memory in enumerate(memories, start=1):
        content = str(memory.get("content") or "").strip()
        if not content:
            continue
        memory_type = str(memory.get("memory_type") or "").strip()
        tags = ", ".join(memory.get("tags", []) or [])
        line = f"[M{index}] 类型：{memory_type}；内容：{content}"
        if tags:
            line += f"；标签：{tags}"
        if used_chars + len(line) > max_chars:
            break
        lines.append(line)
        used_chars += len(line)

    if not lines:
        return []

    return [
        {"role": "system", "content": MEMORY_CONTEXT_PROMPT.strip()},
        {"role": "system", "content": "长期记忆：\n" + "\n".join(lines)},
    ]
V2_POLICY_MARKER = True
