# -*- coding: utf-8 -*-
"""core 存量控量:超预算触发 LLM 分组摘要压缩(对齐 MemGPT rethink)。

场景:P0 拆掉 core 条数上限后,core 只增不减(除非 UPDATE/矛盾软失效);晋升每次成功再 +1。
存量最终会超 CORE_CHAR_BUDGET → 注入侧按 importance 优先把 trivia 挤出注入窗——但那是"隐藏"不是"消除"。
真正的存量收缩机制只有 core 摘要。此模块提供:

- maybe_compact_core:检查 core 总字符是否超阈值,超则触发 LLM 分组摘要
- 分组由 LLM 一次看全部 core 判定(简单版,先上线看效果)

触发点:service.write_chat_memory 的 prune_global_preferences 之后异步调用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from openai import OpenAI

from agent.memory import vector as V
from agent.memory import history as H
from agent.memory.config import (
    CHAT_USER_ID, MEMORY_TYPE_CORE, CHAT_CORE_TYPES, SCOPE_GLOBAL,
    CORE_CHAR_BUDGET, CORE_COMPACT_TRIGGER_RATIO, CORE_COMPACT_MIN_GROUP,
    MEMORY_COLLECTION,
)
from rag.embedder import embed_text


__env_path = Path(__file__).resolve().parents[2] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

_MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_MEMORY_MODEL = os.getenv("MEMORY_MODEL") or os.getenv("AGENT_MODEL") or "gpt-4o"

_llm_client = OpenAI(base_url=_MODEL_BASE_URL, api_key=_OPENAI_API_KEY)

LlmFn = Callable[[str, str], Optional[dict]]


COMPACT_SYSTEM_PROMPT = """你是 core 记忆压缩器。给你若干条关于用户的 core 记忆(每条带整数 id),
将**主题相近、信息可合并**的条目分组,并给出整合版正文。

**合并原则**:
- 保留所有非冗余信息,不丢事实
- 语气统一、去重、精简
- 时序/演化信息若矛盾以最新为准(created_at 已按新到旧排序)

**不能合并的**:
- 主题不同的(如"用户偏好中文" 和 "用户是后端工程师"不该合并)
- 冲突的(留旧不动,或返回不合并)
- 单独出现的(len(member_ids) < 2 不需要合并)

few-shot 示例:

输入:[
  {"id": "0", "content": "用户偏好中文回答"},
  {"id": "1", "content": "用户希望回答简洁"},
  {"id": "2", "content": "用户是后端工程师"},
  {"id": "3", "content": "用户希望回答用中文并且先给结论"}
]
输出:{
  "groups": [
    {"member_ids": ["0","1","3"],
     "merged": "用户偏好中文回答,风格简洁、先给结论"}
  ]
}
  # id=2 是独立身份类,不合并

输入:[
  {"id": "0", "content": "用户是后端工程师"},
  {"id": "1", "content": "用户偏好中文"},
  {"id": "2", "content": "用户在做跨境电商"}
]
输出:{"groups": []}
  # 三条主题各异,不能合并

要求:只输出 JSON,格式 {"groups": [{"member_ids": [...], "merged": "..."}]}。
member_ids 必须来自输入的整数 id;merged 是压缩后的正文。"""


def _default_llm(system: str, user: str) -> Optional[dict]:
    """默认 LLM 调用。失败返 None(压缩是"锦上添花",失败不影响主链路)。"""
    try:
        resp = _llm_client.chat.completions.create(
            model=_MEMORY_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            timeout=90,     # compaction 数据量较大,给多点时间
        )
        raw = resp.choices[0].message.content or ""
        return _parse_json(raw)
    except Exception:
        return None


def _parse_json(raw: str) -> Optional[dict]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{"); end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None


def _build_compact_user_prompt(items: list[dict[str, str]]) -> str:
    return "输入:\n" + json.dumps(items, ensure_ascii=False, indent=2)


def maybe_compact_core(user_id: str = CHAT_USER_ID,
                       llm: LlmFn = _default_llm) -> Optional[dict[str, Any]]:
    """检查 core 总字符是否超阈值 → 触发 LLM 分组摘要。

    返回 {compacted: N, saved_chars: X} 或 None(未触发/失败)。
    """
    try:
        items = V.scroll_memories(
            user_id=user_id, memory_type=CHAT_CORE_TYPES,
            scope=SCOPE_GLOBAL, chat_id="", limit=1000)
    except Exception:
        return None

    total_chars = sum(len(str(m.get("content", ""))) for m in items)
    threshold = CORE_CHAR_BUDGET * CORE_COMPACT_TRIGGER_RATIO
    if total_chars <= threshold:
        return None   # 未超阈值,不触发

    # 按 created_at 降序(新在前),对齐注入侧排序
    items.sort(key=lambda m: str(m.get("created_at", "")), reverse=True)

    # 组装 LLM 用的临时 id 映射(反幻觉)
    id_map: dict[str, str] = {}
    llm_items: list[dict[str, str]] = []
    for idx, m in enumerate(items):
        temp_id = str(idx)
        id_map[temp_id] = str(m.get("memory_id", ""))
        llm_items.append({"id": temp_id, "content": str(m.get("content", ""))})

    result = llm(COMPACT_SYSTEM_PROMPT, _build_compact_user_prompt(llm_items))
    if not result or not isinstance(result.get("groups"), list):
        return None

    compacted = 0
    saved = 0
    for group in result["groups"]:
        if not isinstance(group, dict):
            continue
        member_temp_ids = group.get("member_ids", [])
        merged = str(group.get("merged", "")).strip()
        if not merged or len(member_temp_ids) < CORE_COMPACT_MIN_GROUP:
            continue

        # 映射回真实 id + 保守校验
        member_real_ids = []
        for t in member_temp_ids:
            real = id_map.get(str(t))
            if real:
                member_real_ids.append(real)
        if len(member_real_ids) < CORE_COMPACT_MIN_GROUP:
            continue

        # 保守:merged 要短于原总和才算真压缩(防 LLM 输出反而变长)
        original_chars = 0
        max_imp = 0.0
        for mid in member_real_ids:
            m = V.get_memory(mid)
            if not m: continue
            original_chars += len(str(m.get("content", "")))
            max_imp = max(max_imp, float(m.get("confidence", 0.5)))
        if len(merged) >= original_chars:
            continue

        # 落库:新条 ADD(取组内 max importance,不降级),原成员软失效
        try:
            new_payload = V.insert_memory(
                merged, vector=embed_text(merged),
                memory_type=MEMORY_TYPE_CORE, chat_id="",
                user_id=user_id, confidence=max_imp)
            for old_id in member_real_ids:
                V.invalidate_memory(old_id)
            H.add_history(new_payload["memory_id"], "COMPACT", "", merged)
            compacted += 1
            saved += original_chars - len(merged)
        except Exception:
            continue

    if compacted == 0:
        return None
    return {"compacted": compacted, "saved_chars": saved}
