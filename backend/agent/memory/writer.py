"""记忆写入器(两阶段 LLM,对齐 mem0)。

阶段一 extract:从已完成任务抽取候选事实。
阶段二 decide:对候选事实,对照向量检索到的相似旧记忆,决定 ADD/UPDATE/DELETE/NONE。

反幻觉核心:给决策 LLM 的旧记忆只带临时整数 id("0","1"...),LLM 输出的 id
只能是这些整数;代码用 uuid_mapping 映射回真实 memory_id。LLM 永远碰不到真实 UUID,
因此无法误伤或凭空修改任意记忆。ADD 的新 id 落到"不在 mapping 里"→ 走新建。
"""

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from openai import OpenAI

from agent.memory import vector as V
from agent.memory import history as H
from agent.memory.config import (
    WRITE_SEARCH_TOP_K, MEMORY_KIND_SEMANTIC, SCOPE_USER, SCOPE_DOMAIN, DEFAULT_USER_ID,
)
from agent.memory.prompts import (
    EXTRACT_SYSTEM_PROMPT, build_extract_user_prompt,
    DECISION_SYSTEM_PROMPT, build_decision_user_prompt,
)
from rag.embedder import embed_text


__env_path = Path(__file__).resolve().parents[2] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

_MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_MEMORY_MODEL = os.getenv("MEMORY_MODEL") or os.getenv("AGENT_MODEL") or "gpt-4o"

_llm_client = OpenAI(base_url=_MODEL_BASE_URL, api_key=_OPENAI_API_KEY)

# 允许测试注入:签名 (system_prompt, user_prompt) -> dict
LlmFn = Callable[[str, str], Optional[dict]]


def _default_llm(system_prompt: str, user_prompt: str) -> Optional[dict]:
    """默认 LLM 调用:JSON 模式,失败返回 None(记忆写入失败不该拖垮 agent)。"""
    try:
        resp = _llm_client.chat.completions.create(
            model=_MEMORY_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            timeout=60,
        )
        raw = resp.choices[0].message.content or ""
        return _parse_json(raw)
    except Exception:
        return None


def _parse_json(raw: str) -> Optional[dict]:
    """宽松解析 JSON(容忍 ```json 包裹和前后噪声)。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None


def extract_facts(task: str, trajectory: str, domain: str = "",
                  llm: LlmFn = _default_llm) -> list[dict[str, Any]]:
    """阶段一:抽取候选事实。返回 [{content, scope, domain}]。"""
    result = llm(EXTRACT_SYSTEM_PROMPT, build_extract_user_prompt(task, trajectory, domain))
    if not result:
        return []
    facts = result.get("facts", [])
    if not isinstance(facts, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        content = str(f.get("content", "")).strip()
        if not content:
            continue
        scope = f.get("scope", SCOPE_USER)
        if scope not in (SCOPE_USER, SCOPE_DOMAIN):
            scope = SCOPE_USER
        cleaned.append({
            "content": content,
            "scope": scope,
            "domain": str(f.get("domain", "")).strip() if scope == SCOPE_DOMAIN else "",
        })
    return cleaned


def _apply_decision(event: str, temp_id: str, text: str,
                    uuid_mapping: dict[str, str], fact_meta: dict[str, Any],
                    user_id: str) -> Optional[str]:
    """把单条决策落库。返回落库动作的简短描述,NONE/无效返回 None。

    反幻觉:UPDATE/DELETE 的 temp_id 必须在 uuid_mapping 里(即来自本次检索的真实旧记忆)。
    不在 mapping 里的 UPDATE/DELETE 一律忽略(LLM 若编造 id 也无从生效)。
    """
    event = str(event or "").upper()
    real_id = uuid_mapping.get(str(temp_id))

    if event == "ADD":
        if not text:
            return None
        payload = V.insert_memory(
            text, vector=embed_text(text),
            memory_kind=MEMORY_KIND_SEMANTIC,
            scope=fact_meta.get("scope", SCOPE_USER),
            domain=fact_meta.get("domain", ""),
            user_id=user_id,
        )
        H.add_history(payload["memory_id"], "ADD", "", text)
        return f"ADD {payload['memory_id']}"

    if event == "UPDATE":
        if not real_id or not text:
            return None  # 反幻觉:编造的 id 不生效
        prev = V.get_memory(real_id)
        updated = V.update_memory(real_id, text, vector=embed_text(text))
        if updated is None:
            return None
        H.add_history(real_id, "UPDATE", (prev or {}).get("content", ""), text)
        return f"UPDATE {real_id}"

    if event == "DELETE":
        if not real_id:
            return None  # 反幻觉
        prev = V.get_memory(real_id)
        if prev is None:
            return None
        V.delete_memory(real_id)
        H.add_history(real_id, "DELETE", prev.get("content", ""), "")
        return f"DELETE {real_id}"

    return None  # NONE 或未知


def write_memory(task: str, trajectory: str, domain: str = "",
                 user_id: str = DEFAULT_USER_ID, llm: LlmFn = _default_llm) -> dict[str, Any]:
    """完整两阶段写入。返回 {facts, applied:[...], skipped_hash:int}。

    整个流程对异常宽容:任一环节失败都不抛,返回已完成的部分(记忆写入是尽力而为)。
    """
    result: dict[str, Any] = {"facts": [], "applied": [], "skipped_hash": 0}

    # 阶段一:抽取
    try:
        facts = extract_facts(task, trajectory, domain, llm=llm)
    except Exception:
        return result
    result["facts"] = facts
    if not facts:
        return result

    # hash 去重:对每条事实,先看是否已有完全相同正文(命中直接跳过,连决策 LLM 都不调)
    fact_texts = [f["content"] for f in facts]
    fresh_facts: list[dict[str, Any]] = []
    for f in facts:
        h = V.content_hash(f["content"])
        # 用 domain/scope 过滤后搜同 hash 的代价高;这里简单用检索候选里的 hash 判重(下方统一处理)
        fresh_facts.append(f)

    # 阶段二:对每条事实检索相似旧记忆 → 决策
    # 为了让 uuid_mapping 干净,逐条事实处理(每条独立的临时 id 空间)
    for fact in fresh_facts:
        content = fact["content"]
        scope = fact.get("scope", SCOPE_USER)
        domain_f = fact.get("domain", "")
        try:
            query_vec = embed_text(content)
            similar = V.search_memories(
                query_vec, top_k=WRITE_SEARCH_TOP_K, user_id=user_id,
                memory_kind=MEMORY_KIND_SEMANTIC,
                scope=scope, domain=(domain_f or None) if scope == SCOPE_DOMAIN else None,
            )
        except Exception:
            similar = []

        # hash 去重:与已有完全相同 → 跳过
        new_hash = V.content_hash(content)
        if any(m.get("hash") == new_hash for m in similar):
            result["skipped_hash"] += 1
            continue

        # 构造临时 id 映射(反幻觉)
        uuid_mapping: dict[str, str] = {}
        existing_for_llm: list[dict[str, str]] = []
        for idx, mem in enumerate(similar):
            uuid_mapping[str(idx)] = str(mem.get("memory_id", ""))
            existing_for_llm.append({"id": str(idx), "text": str(mem.get("content", ""))})

        decision = llm(DECISION_SYSTEM_PROMPT,
                       build_decision_user_prompt(existing_for_llm, [content]))
        if not decision:
            # 决策失败 → 保守:直接 ADD 这条新事实(它不在库里)
            action = _apply_decision("ADD", "", content, {}, fact, user_id)
            if action:
                result["applied"].append(action)
            continue

        memory_ops = decision.get("memory", [])
        if not isinstance(memory_ops, list):
            continue
        for op in memory_ops:
            if not isinstance(op, dict):
                continue
            action = _apply_decision(
                op.get("event", ""), op.get("id", ""), str(op.get("text", "")).strip(),
                uuid_mapping, fact, user_id,
            )
            if action:
                result["applied"].append(action)

    return result
