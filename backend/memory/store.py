"""记忆存储实现模块。

封装长期记忆的抽取、校验、SQLite 落库、Qdrant 向量同步和聊天上下文召回。
SQLite 是事实来源，向量库只作为语义检索索引。
"""
import json
import os
import re
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from openai import OpenAI

from memory.policy_v2 import (
    ACTIVE_TASK_STATUSES,
    ENABLED_MEMORY_TYPES,
    POLICY_VERSION,
    WRITER_SYSTEM_PROMPT,
    build_memory_context_messages,
    derive_memory_mode,
    json_dumps,
    memory_types_for_mode,
    normalize_decision,
    normalize_memory_row,
    normalize_writer_output,
)
from storage.db import db

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MEMORY_WRITER_MODEL = os.getenv("MEMORY_WRITER_MODEL", "")
MEMORY_WRITER_AUTO_RUN = os.getenv("MEMORY_WRITER_AUTO_RUN", "1") == "1"
MEMORY_RETRIEVAL_TOP_K = int(os.getenv("MEMORY_RETRIEVAL_TOP_K", "5"))
MEMORY_CONTEXT_MAX_CHARS = int(os.getenv("MEMORY_CONTEXT_MAX_CHARS", "1500"))
MEMORY_USER_ID = os.getenv("MEMORY_USER_ID", "local")


def search_memories(query_text: str, filters: dict[str, Any], top_k: int = 5) -> list[dict[str, Any]]:
    """延迟导入向量检索实现，避免模块加载时就连接 Qdrant。"""
    from memory.vector_index import search_memories as vector_search_memories

    return vector_search_memories(query_text, filters, top_k)


def upsert_memory(memory_item: dict[str, Any]) -> None:
    """延迟导入向量写入实现，把活跃记忆同步到 Qdrant。"""
    from memory.vector_index import upsert_memory as vector_upsert_memory

    vector_upsert_memory(memory_item)


def delete_memory_vector(memory_id: str) -> None:
    """延迟导入向量删除实现，用于软删除或 supersede 后清理索引。"""
    from memory.vector_index import delete_memory_vector as vector_delete_memory

    vector_delete_memory(memory_id)


def make_id(prefix: str) -> str:
    """生成带业务前缀的随机 ID。"""
    return f"{prefix}_{uuid4().hex}"


def _safe_json_loads(value: Any, default: Any) -> Any:
    """宽松解析 JSON 字段，无法解析时返回默认值。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _extract_first_json_object(text: str) -> dict[str, Any]:
    """从模型自由输出中抽取第一个 JSON 对象。"""
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


def _call_writer_json(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    """调用 memory writer 模型，并解析其 JSON 输出。"""
    resolved_model = MEMORY_WRITER_MODEL or model
    if not resolved_model:
        raise RuntimeError("MEMORY_WRITER_MODEL is not configured")
    if not OPENAI_API_KEY or not MODEL_BASE_URL:
        raise RuntimeError("MODEL_BASE_URL or OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=MODEL_BASE_URL)
    response = client.chat.completions.create(
        model=resolved_model,
        messages=messages,
        stream=False,
    )
    content = response.choices[0].message.content if response.choices else ""
    return _extract_first_json_object(content or "")


def _turn_payload_for_writer(job: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    """把一轮完整对话压缩成 writer 可消费的输入载荷。"""
    messages = db.list_turn_messages(turn["turn_id"])
    user_messages = [message for message in messages if message.get("role") == "user"]
    assistant_messages = [message for message in messages if message.get("role") == "assistant"]
    user_message_ids = [message["message_id"] for message in user_messages if message.get("message_id")]
    assistant_message = assistant_messages[-1] if assistant_messages else {}
    sources = _safe_json_loads(assistant_message.get("sources_json"), [])

    compact_sources = []
    for source in (sources if isinstance(sources, list) else []):
        if not isinstance(source, dict):
            continue
        compact_sources.append({
            "source_id": source.get("source_id", ""),
            "source_kind": source.get("source_kind") or source.get("type") or "",
            "title": source.get("title", ""),
            "url": source.get("url") or source.get("canonical_url") or "",
        })

    return {
        "job_id": job["job_id"],
        "chat_id": turn["chat_id"],
        "turn_id": turn["turn_id"],
        "turn_index": turn["turn_index"],
        "task_type": turn["task_type"],
        "query_text": turn["query_text"],
        "focus_text": turn["focus_text"],
        "use_current_page": bool(turn["use_current_page"]),
        "use_web_search": bool(turn["use_web_search"]),
        "retrieval_query": turn["retrieval_query"],
        "web_search_query": turn["web_search_query"],
        "page_url": turn["page_url"],
        "page_title": turn["page_title"],
        "origin": turn.get("origin", "user"),
        "synthetic_user": bool(turn.get("synthetic_user", 0)),
        "plan_id": turn.get("plan_id", ""),
        "user_message": "\n\n".join(str(message.get("content") or "") for message in user_messages),
        "assistant_final_answer": str(assistant_message.get("content") or ""),
        "source_metadata": compact_sources,
        "source_message_ids": user_message_ids,
    }


def _should_skip_writer_for_origin(turn_payload: dict[str, Any]) -> bool:
    """判断该 turn 是否来自自动计划执行，避免把合成用户消息写入记忆。"""
    return (
        bool(turn_payload.get("synthetic_user"))
        or str(turn_payload.get("origin") or "").strip() == "plan_auto_execution"
    )


def _skipped_writer_output(turn_payload: dict[str, Any]) -> dict[str, Any]:
    """构造因来源策略跳过 writer 时的标准输出。"""
    return {
        "candidates": [],
        "related": {},
        "decisions": [],
        "applied": [{"action": "noop", "reason": "skipped_by_origin"}],
        "policy_version": POLICY_VERSION,
        "validation_warnings": ["skipped_by_origin"],
        "skip_reason": "plan_auto_execution_turn",
        "origin": str(turn_payload.get("origin") or ""),
        "synthetic_user": bool(turn_payload.get("synthetic_user")),
        "plan_id": str(turn_payload.get("plan_id") or ""),
    }


def _normalize_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    """把候选记忆先按 insert 决策标准化，复用统一策略校验。"""
    output = normalize_writer_output({"decisions": [{**raw, "action": "insert"}]})
    decisions = output.get("decisions", [])
    return decisions[0] if decisions else {"action": "noop"}


def _extract_candidates(raw_output: dict[str, Any]) -> list[dict[str, Any]]:
    """从第一阶段模型输出中提取可继续决策的候选记忆。"""
    raw_candidates = raw_output.get("candidates")
    if not isinstance(raw_candidates, list):
        return []
    candidates: list[dict[str, Any]] = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            continue
        candidate = _normalize_candidate(raw_candidate)
        if candidate["action"] == "noop" or not candidate["content"]:
            continue
        candidates.append(candidate)
    return candidates


def _search_related_memories(candidate: dict[str, Any], turn_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """为候选记忆查找相似或同范围旧记忆，供第二阶段决策去重/更新。"""
    if candidate["memory_type"] == "task_state":
        rows = db.list_memory_items(
            status="active",
            user_id=MEMORY_USER_ID,
            memory_types=["task_state"],
            scope="chat",
            scope_chat_id=str(turn_payload.get("chat_id") or ""),
            limit=20,
        )
        return [
            {
                "memory_id": memory["memory_id"],
                "memory_type": memory["memory_type"],
                "scope": memory.get("scope", ""),
                "scope_chat_id": memory.get("scope_chat_id", ""),
                "task_status": memory.get("task_status", ""),
                "task_updated_by": memory.get("task_updated_by", ""),
                "content": memory["content"],
                "evidence": memory.get("evidence", ""),
                "classification_reason": memory.get("classification_reason", ""),
                "policy_version": memory.get("policy_version", ""),
                "tags": normalize_memory_row(memory).get("tags", []),
                "importance": memory.get("importance", 0.5),
                "confidence": memory.get("confidence", 0.5),
                "stability": memory.get("stability", 0.5),
                "updated_at": memory.get("updated_at", ""),
            }
            for memory in rows[:3]
            if memory.get("status") == "active"
        ]

    active_rows = db.list_memory_items(
        status="active",
        user_id=MEMORY_USER_ID,
        memory_types=[candidate["memory_type"]],
        limit=20,
    )
    if not active_rows:
        return []

    try:
        hits = search_memories(
            candidate["content"],
            {
                "user_id": MEMORY_USER_ID,
                "status": "active",
                "memory_types": [candidate["memory_type"]],
            },
            top_k=3,
        )
        memory_ids = [hit["memory_id"] for hit in hits if hit.get("memory_id")]
        rows = db.list_memory_items_by_ids(memory_ids)
    except Exception:
        rows = active_rows[:3]

    return [
        {
            "memory_id": memory["memory_id"],
            "memory_type": memory["memory_type"],
            "content": memory["content"],
            "evidence": memory.get("evidence", ""),
            "classification_reason": memory.get("classification_reason", ""),
            "policy_version": memory.get("policy_version", ""),
            "tags": normalize_memory_row(memory).get("tags", []),
            "importance": memory.get("importance", 0.5),
            "confidence": memory.get("confidence", 0.5),
            "stability": memory.get("stability", 0.5),
            "updated_at": memory.get("updated_at", ""),
        }
        for memory in rows
        if memory.get("status") == "active"
    ]


EVIDENCE_BOUND_MEMORY_TYPES = {"user_profile", "project_state", "procedural_feedback"}
GENERIC_MEMORY_TERMS = {"用户", "要求", "偏好", "回答", "风格", "默认", "以后", "后续", "长期"}
MEMORY_SUPPORT_TERMS = [
    "中文",
    "英文",
    "语言",
    "专业",
    "全面",
    "深度",
    "聚合",
    "简洁",
    "直接",
    "详细",
    "精简",
    "完整",
    "结构化",
    "对比",
    "优缺点",
    "引用",
    "来源",
    "搜索",
    "技术",
    "代码",
    "特例",
    "结合代码",
    "日志",
    "调用链",
    "调试",
    "复盘",
    "规划",
    "计划",
    "设计",
    "架构",
    "实现",
    "优化",
    "生产级",
    "错误处理",
    "类型定义",
    "复杂度",
    "最佳实践",
    "测试",
    "完成",
    "没完成",
    "继续修",
    "不对",
    "bug",
    "阻塞",
    "取消",
    "暂缓",
    "前端",
    "后端",
    "数据库",
    "memory",
    "rag",
    "web search",
]


def _distinctive_memory_terms(text: str) -> set[str]:
    """提取用于证据匹配的相对有辨识度词项。"""
    value = str(text or "").lower()
    terms = {
        term.lower()
        for term in MEMORY_SUPPORT_TERMS
        if term.lower() in value and term not in GENERIC_MEMORY_TERMS
    }
    terms.update(
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}", value)
        if token not in {"user", "answer", "memory"}
    )
    return terms


def _current_user_support_text(turn_payload: dict[str, Any]) -> str:
    """提取只能来自用户侧的证据文本。"""
    return "\n".join(
        str(turn_payload.get(key) or "")
        for key in ["query_text", "focus_text", "user_message"]
    )


def _current_task_support_text(turn_payload: dict[str, Any]) -> str:
    """提取可用于 task_state 的本轮任务证据文本。"""
    return "\n".join(
        str(turn_payload.get(key) or "")
        for key in ["query_text", "focus_text", "user_message", "assistant_final_answer"]
    )


def _related_evidence_text(
        decision: dict[str, Any],
        related_memories: dict[str, list[dict[str, Any]]],
) -> str:
    """取出某条决策指向的旧记忆证据。"""
    memory = _related_memory_for_decision(decision, related_memories)
    if memory:
        return str(memory.get("evidence") or "")
    return ""


def _related_memory_for_decision(
        decision: dict[str, Any],
        related_memories: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """按 target/related ID 找到当前决策对应的旧记忆。"""
    target_ids = {
        str(decision.get("target_memory_id") or "").strip(),
        *[str(value).strip() for value in decision.get("related_memory_ids", [])],
    }
    target_ids.discard("")
    for memories in related_memories.values():
        for memory in memories:
            if target_ids and memory.get("memory_id") in target_ids:
                return memory
    target_memory_id = str(decision.get("target_memory_id") or "").strip()
    if target_memory_id:
        return dict(db.get_memory_item(target_memory_id) or {})
    return {}


def _evidence_mentions_current_user_text(evidence: str, user_text: str) -> bool:
    """判断证据是否能被当前轮用户文本支持。"""
    evidence_value = str(evidence or "").strip()
    user_value = str(user_text or "").strip()
    if not evidence_value or not user_value:
        return False
    if evidence_value in user_value or user_value in evidence_value:
        return True
    evidence_terms = _distinctive_memory_terms(evidence_value)
    user_terms = _distinctive_memory_terms(user_value)
    if not evidence_terms:
        return False
    return bool(evidence_terms & user_terms)


def _prune_content_to_supported_clauses(content: str, support_text: str) -> str:
    """只保留能被证据文本支持的记忆分句。"""
    support_terms = _distinctive_memory_terms(support_text)
    if not support_terms:
        return ""

    clauses = [
        clause.strip(" ，,。；;")
        for clause in re.split(r"[。；;]\s*|[，,]\s*", str(content or ""))
        if clause.strip(" ，,。；;")
    ]
    kept: list[str] = []
    for clause in clauses:
        clause_terms = _distinctive_memory_terms(clause)
        if not clause_terms:
            continue
        overlap = clause_terms & support_terms
        required = 1 if len(clause_terms) <= 2 else max(2, (len(clause_terms) + 1) // 2)
        if len(overlap) >= required:
            kept.append(clause)

    if not kept:
        return ""
    content = "；".join(dict.fromkeys(kept)) + "。"
    content = content.replace("；且", "，且")
    content = content.replace("；并且", "，并且")
    content = content.replace("时；要求", "时，要求")
    return content


def _constrain_user_evidenced_decision(
        decision: dict[str, Any],
        turn_payload: dict[str, Any],
        related_memories: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """约束用户画像/项目状态等记忆必须由当前用户文本支持。"""
    if decision.get("memory_type") not in EVIDENCE_BOUND_MEMORY_TYPES:
        return decision
    if decision.get("action") == "noop" and not str(decision.get("content") or "").strip():
        return decision

    user_text = _current_user_support_text(turn_payload)
    warnings = list(decision.get("validation_warnings", []))
    if not _evidence_mentions_current_user_text(str(decision.get("evidence") or ""), user_text):
        warnings.append("evidence_not_supported_by_current_user_text")
        return {
            **decision,
            "action": "noop",
            "validation_warnings": list(dict.fromkeys(warnings)),
        }

    support_text = "\n".join([
        user_text,
        _related_evidence_text(decision, related_memories),
    ])
    pruned_content = _prune_content_to_supported_clauses(str(decision.get("content") or ""), support_text)
    if not pruned_content:
        warnings.append("content_not_supported_by_user_evidence")
        return {
            **decision,
            "action": "noop",
            "validation_warnings": list(dict.fromkeys(warnings)),
        }
    if pruned_content != decision.get("content"):
        warnings.append("content_pruned_to_user_evidence")
        decision = {
            **decision,
            "content": pruned_content,
            "classification_reason": (
                str(decision.get("classification_reason") or "").strip()
                + " 已按用户原文证据裁剪未被支持的扩展内容。"
            ).strip(),
            "validation_warnings": list(dict.fromkeys(warnings)),
        }
    if decision.get("action") in {"update", "supersede"}:
        related_evidence = _related_evidence_text(decision, related_memories)
        if related_evidence and related_evidence not in str(decision.get("evidence") or ""):
            warnings.append("related_evidence_preserved")
            decision = {
                **decision,
                "evidence": "\n".join(
                    part
                    for part in [
                        related_evidence.strip(),
                        str(decision.get("evidence") or "").strip(),
                    ]
                    if part
                ),
                "validation_warnings": list(dict.fromkeys(warnings)),
            }
    if decision.get("action") == "noop" and decision.get("target_memory_id"):
        related_memory = _related_memory_for_decision(decision, related_memories)
        old_content = str(related_memory.get("content") or "")
        pruned_old_content = _prune_content_to_supported_clauses(old_content, support_text)
        if pruned_old_content and pruned_old_content != old_content:
            warnings.append("noop_converted_to_update_for_memory_cleanup")
            combined_evidence = "\n".join(
                part
                for part in [
                    str(related_memory.get("evidence") or "").strip(),
                    str(decision.get("evidence") or "").strip(),
                ]
                if part
            )
            return {
                **decision,
                "action": "update",
                "content": pruned_old_content,
                "evidence": combined_evidence,
                "classification_reason": "已按用户原文证据清理旧记忆中未被支持的扩展内容。",
                "validation_warnings": list(dict.fromkeys(warnings)),
            }
    return decision


def _constrain_task_state_decision(
        decision: dict[str, Any],
        turn_payload: dict[str, Any],
        related_memories: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """约束 task_state 只能更新当前 chat/plan 范围内且有证据支持的任务。"""
    if decision.get("memory_type") != "task_state":
        return decision
    if decision.get("action") == "noop" and not str(decision.get("content") or "").strip():
        return decision

    warnings = list(decision.get("validation_warnings", []))
    related_memory = _related_memory_for_decision(decision, related_memories)
    if related_memory:
        related_chat_id = str(related_memory.get("scope_chat_id") or related_memory.get("source_chat_id") or "")
        if related_chat_id and related_chat_id != str(turn_payload.get("chat_id") or ""):
            warnings.append("task_state_cross_chat_target")
            return {
                **decision,
                "action": "noop",
                "validation_warnings": list(dict.fromkeys(warnings)),
            }

    support_text = "\n".join([
        _current_task_support_text(turn_payload),
        _related_evidence_text(decision, related_memories),
    ])
    if not _evidence_mentions_current_user_text(str(decision.get("evidence") or ""), support_text):
        warnings.append("task_evidence_not_supported_by_turn")
        return {
            **decision,
            "action": "noop",
            "validation_warnings": list(dict.fromkeys(warnings)),
        }

    pruned_content = _prune_content_to_supported_clauses(str(decision.get("content") or ""), support_text)
    if not pruned_content:
        warnings.append("task_content_not_supported_by_turn")
        return {
            **decision,
            "action": "noop",
            "validation_warnings": list(dict.fromkeys(warnings)),
        }
    if pruned_content != decision.get("content"):
        warnings.append("task_content_pruned_to_turn_evidence")
        decision = {
            **decision,
            "content": pruned_content,
            "validation_warnings": list(dict.fromkeys(warnings)),
        }

    if decision.get("action") in {"update", "supersede"}:
        related_evidence = _related_evidence_text(decision, related_memories)
        if related_evidence and related_evidence not in str(decision.get("evidence") or ""):
            warnings.append("related_evidence_preserved")
            decision = {
                **decision,
                "evidence": "\n".join(
                    part
                    for part in [
                        related_evidence.strip(),
                        str(decision.get("evidence") or "").strip(),
                    ]
                    if part
                ),
                "validation_warnings": list(dict.fromkeys(warnings)),
            }
    return decision


def _sync_memory_vector(memory_id: str) -> None:
    """按 SQLite 当前状态刷新单条记忆的向量索引。"""
    row = db.get_memory_item(memory_id)
    if not row:
        return
    item = normalize_memory_row(row)
    if item.get("status") != "active":
        delete_memory_vector(memory_id)
        return
    upsert_memory(item)


def _existing_task_plan_id(target_memory_id: str) -> str:
    """读取旧 task_state 已绑定的 plan_id，避免更新时丢失计划关联。"""
    if not target_memory_id:
        return ""
    row = db.get_memory_item(target_memory_id)
    if not row or row.get("memory_type") != "task_state":
        return ""
    return str(row.get("plan_id") or "")


def _memory_scope_fields(
        decision: dict[str, Any],
        turn_payload: dict[str, Any],
        target_memory_id: str = "",
) -> dict[str, str]:
    """根据记忆类型和 turn 信息计算 scope/task/plan 落库字段。"""
    if decision.get("memory_type") != "task_state":
        return {
            "scope": "user",
            "scope_chat_id": "",
            "task_status": "",
            "task_updated_by": "",
            "plan_id": "",
        }
    task_status = str(decision.get("task_status") or "open").strip()
    task_updated_by = str(decision.get("task_updated_by") or ("assistant" if task_status == "done" else "user")).strip()
    plan_id = str(turn_payload.get("plan_id") or "").strip() or _existing_task_plan_id(target_memory_id)
    return {
        "scope": "chat",
        "scope_chat_id": str(turn_payload.get("chat_id") or ""),
        "task_status": task_status,
        "task_updated_by": task_updated_by,
        "plan_id": plan_id,
    }


def _task_state_target_is_current_chat(target_memory_id: str, turn_payload: dict[str, Any]) -> bool:
    """防止一个 chat 的任务状态更新到另一个 chat。"""
    row = db.get_memory_item(target_memory_id)
    if not row or row.get("memory_type") != "task_state":
        return True
    target_chat_id = str(row.get("scope_chat_id") or row.get("source_chat_id") or "")
    return target_chat_id == str(turn_payload.get("chat_id") or "")


def _task_state_target_is_current_plan(target_memory_id: str, turn_payload: dict[str, Any]) -> bool:
    """防止计划执行产生的 task_state 更新跨到其他计划。"""
    incoming_plan_id = str(turn_payload.get("plan_id") or "").strip()
    if not incoming_plan_id:
        return True
    plan = db.get_plan(incoming_plan_id)
    if plan and str(plan.get("chat_id") or "") != str(turn_payload.get("chat_id") or ""):
        return False
    row = db.get_memory_item(target_memory_id)
    if not row or row.get("memory_type") != "task_state":
        return True
    existing_plan_id = str(row.get("plan_id") or "").strip()
    return not existing_plan_id or existing_plan_id == incoming_plan_id


def _apply_decision(decision: dict[str, Any], turn_payload: dict[str, Any]) -> dict[str, Any]:
    """把单条标准化决策应用到 SQLite，并同步向量索引。"""
    action = decision["action"]
    if action == "noop":
        return {"action": "noop"}
    if not decision["content"] or decision["memory_type"] not in ENABLED_MEMORY_TYPES:
        return {"action": "noop", "reason": "empty_or_unsupported"}

    target_memory_id = ""
    if action in {"update", "supersede"}:
        target_memory_id = decision["target_memory_id"] or (
            decision["related_memory_ids"][0] if decision["related_memory_ids"] else ""
        )
        if not target_memory_id:
            return {"action": "noop", "reason": "missing_target"}
        if not _task_state_target_is_current_chat(target_memory_id, turn_payload):
            return {"action": "noop", "reason": "task_state_cross_chat_target"}
        if not _task_state_target_is_current_plan(target_memory_id, turn_payload):
            return {"action": "noop", "reason": "task_state_cross_plan_target"}

    source_message_ids_json = json_dumps(turn_payload.get("source_message_ids", []))
    mode_affinity_json = json_dumps(decision["mode_affinity"])
    tags_json = json_dumps(decision["tags"])
    scope_fields = _memory_scope_fields(decision, turn_payload, target_memory_id)

    if action == "insert":
        memory_id = make_id("mem")
        db.insert_memory_item(
            memory_id=memory_id,
            user_id=MEMORY_USER_ID,
            memory_type=decision["memory_type"],
            scope=scope_fields["scope"],
            scope_chat_id=scope_fields["scope_chat_id"],
            content=decision["content"],
            evidence=decision["evidence"],
            classification_reason=decision["classification_reason"],
            policy_version=POLICY_VERSION,
            mode_affinity_json=mode_affinity_json,
            tags_json=tags_json,
            source_chat_id=turn_payload["chat_id"],
            source_turn_id=turn_payload["turn_id"],
            source_message_ids_json=source_message_ids_json,
            importance=decision["importance"],
            confidence=decision["confidence"],
            stability=decision["stability"],
            task_status=scope_fields["task_status"],
            task_updated_by=scope_fields["task_updated_by"],
            plan_id=scope_fields["plan_id"],
        )
        _sync_memory_vector(memory_id)
        return {"action": "insert", "memory_id": memory_id}

    if action == "update":
        db.update_memory_item(target_memory_id, {
            "scope": scope_fields["scope"],
            "scope_chat_id": scope_fields["scope_chat_id"],
            "content": decision["content"],
            "evidence": decision["evidence"],
            "classification_reason": decision["classification_reason"],
            "policy_version": POLICY_VERSION,
            "mode_affinity_json": mode_affinity_json,
            "tags_json": tags_json,
            "source_chat_id": turn_payload["chat_id"],
            "source_turn_id": turn_payload["turn_id"],
            "source_message_ids_json": source_message_ids_json,
            "importance": decision["importance"],
            "confidence": decision["confidence"],
            "stability": decision["stability"],
            "status": "active",
            "task_status": scope_fields["task_status"],
            "task_updated_by": scope_fields["task_updated_by"],
            "plan_id": scope_fields["plan_id"],
        })
        _sync_memory_vector(target_memory_id)
        return {"action": "update", "memory_id": target_memory_id}

    if action == "supersede":
        memory_id = make_id("mem")
        db.insert_memory_item(
            memory_id=memory_id,
            user_id=MEMORY_USER_ID,
            memory_type=decision["memory_type"],
            scope=scope_fields["scope"],
            scope_chat_id=scope_fields["scope_chat_id"],
            content=decision["content"],
            evidence=decision["evidence"],
            classification_reason=decision["classification_reason"],
            policy_version=POLICY_VERSION,
            mode_affinity_json=mode_affinity_json,
            tags_json=tags_json,
            source_chat_id=turn_payload["chat_id"],
            source_turn_id=turn_payload["turn_id"],
            source_message_ids_json=source_message_ids_json,
            importance=decision["importance"],
            confidence=decision["confidence"],
            stability=decision["stability"],
            supersedes_memory_id=target_memory_id,
            task_status=scope_fields["task_status"],
            task_updated_by=scope_fields["task_updated_by"],
            plan_id=scope_fields["plan_id"],
        )
        db.update_memory_item(target_memory_id, {
            "status": "superseded",
            "superseded_by_memory_id": memory_id,
            "valid_to": db._now(),
        })
        delete_memory_vector(target_memory_id)
        _sync_memory_vector(memory_id)
        return {"action": "supersede", "memory_id": memory_id, "superseded_memory_id": target_memory_id}

    return {"action": "noop", "reason": "unsupported_action"}


def create_memory_extraction_job(turn_state: dict[str, Any], final_text: str, sources: list[dict[str, Any]]) -> str:
    """为已完成聊天 turn 创建后台记忆抽取任务。"""
    job_id = make_id("memjob")
    input_json = json_dumps({
        "chat_id": turn_state.get("chat_id", ""),
        "turn_id": turn_state.get("turn_id", ""),
        "model": turn_state.get("model", ""),
        "assistant_preview": str(final_text or "")[:1000],
        "source_count": len(sources or []),
        "policy_version": POLICY_VERSION,
    })
    db.create_memory_extraction_job(
        job_id=job_id,
        chat_id=str(turn_state.get("chat_id", "")),
        turn_id=str(turn_state.get("turn_id", "")),
        input_json=input_json,
    )
    return job_id


def start_memory_writer(job_id: str) -> None:
    """根据配置启动后台 writer；自动计划执行 turn 会同步跳过。"""
    if not MEMORY_WRITER_AUTO_RUN:
        return

    if _job_should_skip_writer_for_origin(job_id):
        _safe_run_memory_extraction_job(job_id)
        return

    thread = threading.Thread(
        target=_safe_run_memory_extraction_job,
        args=(job_id,),
        daemon=True,
    )
    thread.start()


def _job_should_skip_writer_for_origin(job_id: str) -> bool:
    """通过 job 反查 turn 来源，判断是否应跳过 writer。"""
    job = db.get_memory_extraction_job(job_id)
    if not job:
        return False
    turn = db.get_chat_turn(job["turn_id"])
    if not turn:
        return False
    return (
        bool(turn.get("synthetic_user", 0))
        or str(turn.get("origin") or "").strip() == "plan_auto_execution"
    )


def _safe_run_memory_extraction_job(job_id: str) -> None:
    """后台线程入口，确保异常会写回 job 状态。"""
    try:
        run_memory_extraction_job(job_id)
    except Exception as exc:
        db.update_memory_extraction_job(
            job_id,
            status="failed",
            output_json="{}",
            error_message=str(exc),
            completed=True,
        )


def run_memory_extraction_job(job_id: str) -> dict[str, Any]:
    """完整运行两阶段 memory writer，并应用最终写入决策。"""
    job = db.get_memory_extraction_job(job_id)
    if not job:
        raise ValueError(f"memory job not found: {job_id}")

    db.update_memory_extraction_job(job_id, status="processing", output_json="{}", error_message="")
    turn = db.get_chat_turn(job["turn_id"])
    if not turn:
        raise ValueError(f"turn not found: {job['turn_id']}")
    if turn["status"] != "complete":
        raise ValueError("memory writer only accepts complete turns")

    input_data = _safe_json_loads(job.get("input_json"), {})
    turn_payload = _turn_payload_for_writer(job, turn)
    if _should_skip_writer_for_origin(turn_payload):
        output = _skipped_writer_output(turn_payload)
        db.update_memory_extraction_job(
            job_id,
            status="complete",
            output_json=json_dumps(output),
            error_message="",
            completed=True,
        )
        return output

    candidate_output = _call_writer_json(
        str(input_data.get("model") or ""),
        _build_candidate_prompt(turn_payload),
    )
    candidates = _extract_candidates(candidate_output)
    related: dict[str, list[dict[str, Any]]] = {}
    for index, candidate in enumerate(candidates):
        related[str(index)] = _search_related_memories(candidate, turn_payload)

    decision_output = _call_writer_json(
        str(input_data.get("model") or ""),
        _build_decision_prompt(turn_payload, candidates, related),
    )
    normalized_output = normalize_writer_output(decision_output)
    decisions = [
        _constrain_task_state_decision(
            _constrain_user_evidenced_decision(decision, turn_payload, related),
            turn_payload,
            related,
        )
        for decision in normalized_output["decisions"]
    ]
    applied = [
        _apply_decision(decision, turn_payload)
        for decision in decisions
    ]
    output = {
        "candidates": candidates,
        "related": related,
        "decisions": decisions,
        "applied": applied,
        "policy_version": POLICY_VERSION,
        "validation_warnings": list(dict.fromkeys(
            list(normalized_output.get("validation_warnings", []))
            + [
                warning
                for decision in decisions
                for warning in decision.get("validation_warnings", [])
            ]
        )),
    }
    db.update_memory_extraction_job(
        job_id,
        status="complete",
        output_json=json_dumps(output),
        error_message="",
        completed=True,
    )
    return output


def update_chat_summary_rule(chat_id: str, turn_index: int, user_content: str, assistant_content: str) -> None:
    """用规则方式追加聊天摘要，限制总长度避免上下文膨胀。"""
    previous = db.get_chat_summary(chat_id) or {}
    previous_summary = str(previous.get("summary") or "").strip()
    current = f"第{turn_index}轮 用户：{user_content}\n助手：{assistant_content}"
    summary = f"{previous_summary}\n{current}".strip() if previous_summary else current
    if len(summary) > 3000:
        summary = summary[-3000:]
    db.upsert_chat_summary(
        chat_id=chat_id,
        summary=summary,
        source_turn_index=turn_index,
    )


def retrieve_memory_context(
        query_text: str,
        focus_text: str,
        task_type: str,
        use_current_page: bool,
        use_web_search: bool,
        chat_id: str = "",
        top_k: int = MEMORY_RETRIEVAL_TOP_K,
        max_chars: int = MEMORY_CONTEXT_MAX_CHARS,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """按当前任务模式召回长期记忆，并返回可注入模型的上下文消息。"""
    mode = derive_memory_mode(task_type, use_current_page, use_web_search)
    memory_types = memory_types_for_mode(mode)
    user_memory_types = [memory_type for memory_type in memory_types if memory_type != "task_state"]
    active_user_rows = db.list_memory_items(
        status="active",
        user_id=MEMORY_USER_ID,
        memory_types=user_memory_types,
        scope="user",
        limit=100,
    ) if user_memory_types else []
    task_rows = db.list_memory_items(
        status="active",
        user_id=MEMORY_USER_ID,
        memory_types=["task_state"],
        scope="chat",
        scope_chat_id=str(chat_id or ""),
        task_statuses=sorted(ACTIVE_TASK_STATUSES),
        limit=20,
    ) if "task_state" in memory_types and chat_id else []
    if not active_user_rows and not task_rows:
        return [], {
            "memory_mode": mode,
            "memory_retrieved_count": 0,
            "memory_ids": [],
            "memory_types": [],
        }

    query = "\n".join(part for part in [focus_text, query_text] if part).strip()
    user_rows = active_user_rows[:top_k]
    if query and user_memory_types:
        try:
            hits = search_memories(
                query,
                {
                    "user_id": MEMORY_USER_ID,
                    "status": "active",
                    "memory_types": user_memory_types,
                },
                top_k=top_k,
            )
            hit_ids = [hit["memory_id"] for hit in hits if hit.get("memory_id")]
            hit_rows = [
                row for row in db.list_memory_items_by_ids(hit_ids)
                if row.get("scope", "user") == "user"
            ]
            if hit_rows:
                user_rows = hit_rows
        except Exception:
            user_rows = active_user_rows[:top_k]

    rows = [*task_rows, *user_rows]
    memories = [
        normalize_memory_row(row)
        for row in rows[:top_k]
        if row.get("status") == "active"
    ]
    memory_ids = [memory["memory_id"] for memory in memories]
    db.mark_memory_used(memory_ids)
    messages = build_memory_context_messages(memories, max_chars)
    return messages, {
        "memory_mode": mode,
        "memory_retrieved_count": len(memories),
        "memory_ids": memory_ids,
        "memory_types": list(dict.fromkeys(memory["memory_type"] for memory in memories)),
    }


def create_manual_memory(
        content: str,
        memory_type: str,
        evidence: str = "",
        scope_chat_id: str = "",
        mode_affinity: list[str] | None = None,
        tags: list[str] | None = None,
        importance: float = 0.5,
        confidence: float = 0.9,
        stability: float = 0.9,
        task_status: str = "",
        task_updated_by: str = "",
        plan_id: str = "",
) -> dict[str, Any]:
    """手工创建一条记忆，走同一套策略校验并同步向量。"""
    if memory_type not in ENABLED_MEMORY_TYPES:
        raise ValueError(f"unsupported memory_type: {memory_type}")
    normalized_content = str(content or "").strip()
    if not normalized_content:
        raise ValueError("content is required")
    decision = normalize_decision({
        "action": "insert",
        "memory_type": memory_type,
        "content": normalized_content,
        "evidence": evidence or "manual memory",
        "classification_reason": "Manually created memory.",
        "mode_affinity": mode_affinity or [],
        "tags": tags or [],
        "importance": importance,
        "confidence": confidence,
        "stability": stability,
        "task_status": task_status,
        "task_updated_by": task_updated_by,
        "target_memory_id": "",
        "related_memory_ids": [],
    })
    if decision["action"] == "noop":
        raise ValueError("manual memory did not pass policy validation")
    if decision["memory_type"] == "task_state" and not str(scope_chat_id or "").strip():
        raise ValueError("scope_chat_id is required for task_state")

    memory_id = make_id("mem")
    scope = "chat" if decision["memory_type"] == "task_state" else "user"
    db.insert_memory_item(
        memory_id=memory_id,
        user_id=MEMORY_USER_ID,
        memory_type=decision["memory_type"],
        scope=scope,
        scope_chat_id=str(scope_chat_id or "").strip() if scope == "chat" else "",
        content=decision["content"],
        evidence=decision["evidence"],
        classification_reason=decision["classification_reason"],
        policy_version=POLICY_VERSION,
        mode_affinity_json=json_dumps(decision["mode_affinity"]),
        tags_json=json_dumps(decision["tags"]),
        importance=decision["importance"],
        confidence=decision["confidence"],
        stability=decision["stability"],
        task_status=decision["task_status"] if scope == "chat" else "",
        task_updated_by=decision["task_updated_by"] if scope == "chat" else "",
        plan_id=str(plan_id or "").strip() if scope == "chat" else "",
    )
    vector_error = ""
    try:
        _sync_memory_vector(memory_id)
    except Exception as exc:
        vector_error = str(exc)
    row = db.get_memory_item(memory_id)
    memory = normalize_memory_row(row) if row else {}
    if vector_error:
        memory["vector_error"] = vector_error
    return memory


def patch_memory(
        memory_id: str,
        content: str | None = None,
        evidence: str | None = None,
        classification_reason: str | None = None,
        scope_chat_id: str | None = None,
        mode_affinity: list[str] | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        stability: float | None = None,
        status: str | None = None,
        task_status: str | None = None,
        task_updated_by: str | None = None,
        plan_id: str | None = None,
) -> dict[str, Any]:
    """局部更新记忆字段，并根据 active 状态刷新向量。"""
    updates: dict[str, Any] = {}
    if content is not None:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("content must not be empty")
        updates["content"] = normalized_content
    if evidence is not None:
        updates["evidence"] = evidence.strip()
    if classification_reason is not None:
        updates["classification_reason"] = classification_reason.strip()
    if scope_chat_id is not None:
        updates["scope_chat_id"] = scope_chat_id.strip()
    if mode_affinity is not None:
        updates["mode_affinity_json"] = json_dumps(mode_affinity)
    if tags is not None:
        updates["tags_json"] = json_dumps(tags)
    if importance is not None:
        updates["importance"] = max(0.0, min(1.0, float(importance)))
    if confidence is not None:
        updates["confidence"] = max(0.0, min(1.0, float(confidence)))
    if stability is not None:
        updates["stability"] = max(0.0, min(1.0, float(stability)))
    if status is not None:
        updates["status"] = status
        if status != "active":
            updates["valid_to"] = db._now()
    if task_status is not None:
        updates["task_status"] = task_status.strip()
    if task_updated_by is not None:
        updates["task_updated_by"] = task_updated_by.strip()
    if plan_id is not None:
        updates["plan_id"] = plan_id.strip()

    db.update_memory_item(memory_id, updates)
    row = db.get_memory_item(memory_id)
    if not row:
        raise ValueError(f"memory not found: {memory_id}")
    vector_error = ""
    try:
        if row["status"] == "active":
            _sync_memory_vector(memory_id)
        else:
            delete_memory_vector(memory_id)
    except Exception as exc:
        vector_error = str(exc)
    memory = normalize_memory_row(row)
    if vector_error:
        memory["vector_error"] = vector_error
    return memory


def delete_memory(memory_id: str) -> None:
    """软删除记忆，并尽力删除对应向量。"""
    if not db.get_memory_item(memory_id):
        raise ValueError(f"memory not found: {memory_id}")
    db.soft_delete_memory_item(memory_id)
    try:
        delete_memory_vector(memory_id)
    except Exception:
        return


def rerun_memory_job(job_id: str) -> dict[str, Any]:
    """把已有记忆抽取任务重置为 pending 后立即重跑。"""
    job = db.get_memory_extraction_job(job_id)
    if not job:
        raise ValueError(f"memory job not found: {job_id}")
    db.update_memory_extraction_job(job_id, status="pending", output_json="{}", error_message="")
    return run_memory_extraction_job(job_id)


def _build_candidate_prompt(turn_payload: dict[str, Any]) -> list[dict[str, str]]:
    """构造第一阶段候选记忆抽取 prompt。"""
    return [
        {"role": "system", "content": WRITER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请先从本轮对话抽取候选长期记忆，只输出 JSON："
                '{"candidates":[{"memory_type":"user_profile|project_state|task_state|procedural_feedback|episodic_lesson|external_knowledge_ref",'
                '"content":"","evidence":"","classification_reason":"","mode_affinity":[],"tags":[],'
                '"importance":0.5,"confidence":0.5,"stability":0.5}]}\n\n'
                f"输入：{json_dumps(turn_payload)}"
            ),
        },
    ]


def _build_decision_prompt(
        turn_payload: dict[str, Any],
        candidates: list[dict[str, Any]],
        related_memories: dict[str, list[dict[str, Any]]],
) -> list[dict[str, str]]:
    """构造第二阶段写入/更新/去重决策 prompt。"""
    return [
        {"role": "system", "content": WRITER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请基于候选记忆和相似旧记忆做最终写入决策，只输出严格 JSON："
                '{"decisions":[{"action":"insert|update|supersede|noop",'
                '"memory_type":"user_profile|project_state|task_state|procedural_feedback|episodic_lesson|external_knowledge_ref",'
                '"content":"","evidence":"","classification_reason":"","mode_affinity":[],"tags":[],'
                '"importance":0.5,"confidence":0.5,"stability":0.5,'
                '"target_memory_id":"","related_memory_ids":[]}]}\n\n'
                f"本轮输入：{json_dumps(turn_payload)}\n"
                f"候选记忆：{json_dumps(candidates)}\n"
                f"相似旧记忆：{json_dumps(related_memories)}"
            ),
        },
    ]
