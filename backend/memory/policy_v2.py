"""运行时记忆策略。

该版本从 memory_writer skill 的 schema 和示例中加载可写类型、标签和状态约束，
再对模型输出做二次校验，避免无效类型、内部 ID 泄漏和跨范围 task_state 更新。
"""
import json
import re
from pathlib import Path
from typing import Any

from core.utils import clamp_float, json_dumps, parse_json_list

POLICY_VERSION = "memory_writer_skill_v1"
MEMORY_WRITER_SKILL_DIR = Path(__file__).resolve().parent / "skills" / "memory_writer"


INTERNAL_ID_PATTERN = re.compile(
    r"\b(?:mem|memjob|chat|turn|msg|summary|pagectx|page|snap|content)_[A-Za-z0-9-]+\b"
)


def sanitize_internal_references(text: Any) -> str:
    """移除展示原因中的内部业务 ID，避免暴露实现细节。"""
    value = str(text or "").strip()
    if not value:
        return ""
    value = INTERNAL_ID_PATTERN.sub("内部记录", value)
    value = re.sub(r"(旧记忆|记忆|memory)\s+内部记录", r"\1", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def load_memory_writer_schema() -> dict[str, Any]:
    """读取 memory_writer skill 的 schema，用作运行时策略来源。"""
    schema_path = MEMORY_WRITER_SKILL_DIR / "schema.json"
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_memory_writer_skill() -> str:
    """拼接 memory_writer skill 文档，作为 writer 模型 system prompt。"""
    parts: list[str] = []
    for filename in ["SKILL.md", "examples.md", "schema.json"]:
        path = MEMORY_WRITER_SKILL_DIR / filename
        try:
            parts.append(f"# {filename}\n{path.read_text(encoding='utf-8')}")
        except OSError:
            continue
    return "\n\n".join(parts).strip()


_SCHEMA = load_memory_writer_schema()
ENABLED_MEMORY_TYPES = set(_SCHEMA.get("memory_types") or [
    "user_profile",
    "project_state",
    "task_state",
    "procedural_feedback",
    "episodic_lesson",
    "external_knowledge_ref",
])
ALLOWED_MEMORY_TYPES = ENABLED_MEMORY_TYPES
ALLOWED_ACTIONS = set(_SCHEMA.get("actions") or ["insert", "update", "supersede", "noop"])
ALLOWED_TAGS_BY_TYPE = {
    memory_type: set(tags)
    for memory_type, tags in (_SCHEMA.get("tags_by_type") or {}).items()
}
TASK_STATUSES = set(_SCHEMA.get("task_statuses") or [
    "open",
    "in_progress",
    "blocked",
    "done",
    "reopened",
    "cancelled",
])
ACTIVE_TASK_STATUSES = {"open", "in_progress", "blocked", "reopened"}
TASK_UPDATED_BY_VALUES = set(_SCHEMA.get("task_updated_by_values") or ["user", "assistant", "system"])

WRITER_SYSTEM_PROMPT = load_memory_writer_skill() or "You are a Memory Writer. Output strict JSON only."
MEMORY_CONTEXT_PROMPT = """以下是用户长期记忆，只用于理解用户背景、偏好和历史决策。
如果与当前用户输入冲突，以当前用户输入为准。
不要把这些记忆当作外部事实来源引用。
除非用户询问历史，不要显式说“根据你的记忆”。
"""


def derive_memory_mode(task_type: str, use_current_page: bool, use_web_search: bool) -> str:
    """根据当前任务和上下文开关推导记忆召回模式。"""
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
    if task_type == "plan":
        return "plan"
    return "chat"


def memory_types_for_mode(mode: str) -> list[str]:
    """返回某个召回模式下允许参与上下文的记忆类型。"""
    mapping = {
        "chat": ["user_profile", "procedural_feedback", "project_state", "task_state"],
        "plan": ["user_profile", "procedural_feedback", "project_state", "task_state"],
        "explain": ["user_profile", "procedural_feedback", "project_state", "episodic_lesson"],
        "translate": ["user_profile", "procedural_feedback"],
        "page_rag": ["user_profile", "procedural_feedback", "project_state", "task_state", "episodic_lesson"],
        "web_search": ["user_profile", "procedural_feedback", "project_state", "external_knowledge_ref"],
        "combined": [
            "user_profile",
            "procedural_feedback",
            "project_state",
            "task_state",
            "episodic_lesson",
            "external_knowledge_ref",
        ],
    }
    return mapping.get(mode, mapping["chat"])


TAG_TO_MEMORY_TYPE = {
    tag: memory_type
    for memory_type, tags in ALLOWED_TAGS_BY_TYPE.items()
    for tag in tags
}


def _memory_types_implied_by_tags(tags: list[Any]) -> list[str]:
    """根据 schema 标签反推可能的记忆类型。"""
    implied: list[str] = []
    for value in tags:
        memory_type = TAG_TO_MEMORY_TYPE.get(str(value).strip())
        if memory_type and memory_type not in implied:
            implied.append(memory_type)
    return implied


def _resolve_memory_type(
        memory_type: str,
        raw_tags: list[Any],
        warnings: list[str],
) -> tuple[str, bool]:
    """除非标签唯一指向其他类型，否则保留模型给出的 memory_type。"""
    if memory_type not in ENABLED_MEMORY_TYPES:
        warnings.append("unknown_memory_type")
        return "", False

    implied_types = _memory_types_implied_by_tags(raw_tags)
    if not implied_types:
        return memory_type, True
    if len(implied_types) > 1:
        warnings.append("memory_type_conflict_noop")
        return memory_type, False
    implied_type = implied_types[0]
    if implied_type != memory_type:
        warnings.append("memory_type_corrected_by_tag_taxonomy")
        return implied_type, True
    return memory_type, True


def _normalize_tags(memory_type: str, tags: list[Any]) -> list[str]:
    """过滤掉当前记忆类型不允许的标签，并去重保序。"""
    allowed_tags = ALLOWED_TAGS_BY_TYPE.get(memory_type, set())
    normalized: list[str] = []
    for value in tags:
        tag = str(value).strip()
        if not tag:
            continue
        if allowed_tags and tag not in allowed_tags:
            continue
        if tag not in normalized:
            normalized.append(tag)
    return normalized


def _default_tags(memory_type: str) -> list[str]:
    """为缺少标签但可写的记忆补默认标签。"""
    defaults = {
        "user_profile": ["interaction_preference"],
        "project_state": ["progress"],
        "task_state": ["todo"],
        "procedural_feedback": ["workflow_rule"],
        "episodic_lesson": ["backend_issue"],
        "external_knowledge_ref": ["doc_reference"],
    }
    return defaults.get(memory_type, [])


def normalize_decision(raw: dict[str, Any]) -> dict[str, Any]:
    """校验单条 writer 决策，输出可直接落库的标准结构。"""
    warnings: list[str] = []
    action = str(raw.get("action") or "noop").strip()
    if action not in ALLOWED_ACTIONS:
        warnings.append("unknown_action")
        action = "noop"

    content = str(raw.get("content") or "").strip()
    evidence = str(raw.get("evidence") or "").strip()
    raw_tags = parse_json_list(raw.get("tags"))
    memory_type, type_is_usable = _resolve_memory_type(
        str(raw.get("memory_type") or "").strip(),
        raw_tags,
        warnings,
    )
    if not type_is_usable:
        action = "noop"
    if action != "noop" and not content:
        warnings.append("empty_content")
        action = "noop"
    if action != "noop" and not evidence:
        warnings.append("empty_evidence")
        action = "noop"
    task_status = str(raw.get("task_status") or "").strip()
    task_updated_by = str(raw.get("task_updated_by") or "").strip()
    if memory_type == "task_state":
        if not task_status:
            task_status = "open"
        if task_status not in TASK_STATUSES:
            warnings.append("invalid_task_status")
            action = "noop"
        if not task_updated_by:
            task_updated_by = "assistant" if task_status == "done" else "user"
        if task_updated_by not in TASK_UPDATED_BY_VALUES:
            warnings.append("invalid_task_updated_by")
            action = "noop"
    else:
        task_status = ""
        task_updated_by = ""

    mode_affinity = [
        str(value).strip()
        for value in parse_json_list(raw.get("mode_affinity"))
        if str(value).strip()
    ]
    tags = _normalize_tags(memory_type, raw_tags)
    if action != "noop" and memory_type and not tags:
        warnings.append("default_tags_applied")
        tags = _default_tags(memory_type)

    classification_reason = sanitize_internal_references(raw.get("classification_reason"))
    if action != "noop" and not classification_reason:
        warnings.append("missing_classification_reason")
        classification_reason = "Normalized by memory policy because the model did not provide a reason."

    related_memory_ids = [
        str(value).strip()
        for value in parse_json_list(raw.get("related_memory_ids"))
        if str(value).strip()
    ]

    return {
        "action": action,
        "memory_type": memory_type,
        "content": content,
        "evidence": evidence,
        "classification_reason": classification_reason,
        "mode_affinity": mode_affinity,
        "tags": tags,
        "importance": clamp_float(raw.get("importance"), 0.5),
        "confidence": clamp_float(raw.get("confidence"), 0.5),
        "stability": clamp_float(raw.get("stability"), 0.5),
        "target_memory_id": str(raw.get("target_memory_id") or "").strip(),
        "related_memory_ids": related_memory_ids,
        "task_status": task_status,
        "task_updated_by": task_updated_by,
        "validation_warnings": warnings,
    }


def normalize_writer_output(value: Any) -> dict[str, Any]:
    """标准化完整 writer 输出，并汇总所有校验警告。"""
    if not isinstance(value, dict):
        return {"decisions": [], "validation_warnings": ["invalid_output"]}
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        return {"decisions": [], "validation_warnings": ["missing_decisions"]}
    normalized = [
        normalize_decision(item)
        for item in decisions
        if isinstance(item, dict)
    ]
    warnings: list[str] = []
    for decision in normalized:
        warnings.extend(decision.get("validation_warnings", []))
    return {
        "decisions": normalized,
        "validation_warnings": list(dict.fromkeys(warnings)),
    }


def normalize_memory_row(row: dict[str, Any]) -> dict[str, Any]:
    """把数据库记忆行转换为前端和召回链路共用的规范结构。"""
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
    item["classification_reason"] = sanitize_internal_references(item.get("classification_reason"))
    item["policy_version"] = str(item.get("policy_version") or POLICY_VERSION)
    item["scope"] = str(item.get("scope") or "user")
    item["scope_chat_id"] = str(item.get("scope_chat_id") or "")
    item["task_status"] = str(item.get("task_status") or "")
    item["task_updated_by"] = str(item.get("task_updated_by") or "")
    item["plan_id"] = str(item.get("plan_id") or "")
    return item


def build_memory_context_messages(memories: list[dict[str, Any]], max_chars: int) -> list[dict[str, str]]:
    """把记忆列表压缩成模型上下文消息，并控制总字符数。"""
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
        if memory_type == "task_state" and memory.get("task_status"):
            line += f"；任务状态：{memory['task_status']}"
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
