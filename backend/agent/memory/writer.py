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
    WRITE_SEARCH_TOP_K, DEFAULT_USER_ID, CHAT_USER_ID,
    MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_SITE_EXPERIENCE, MEMORY_TYPE_LESSON,
    MEMORY_TYPE_PERSONA, MEMORY_TYPE_EPISODIC,
    SCOPE_GLOBAL, SCOPE_DOMAIN, LESSON_INIT_CONFIDENCE,
)
from agent.memory.prompts import (
    EXTRACT_SYSTEM_PROMPT, build_extract_user_prompt,
    FAILURE_EXTRACT_SYSTEM_PROMPT, build_failure_extract_user_prompt,
    DECISION_SYSTEM_PROMPT, build_decision_user_prompt,
    CHAT_EXTRACT_SYSTEM_PROMPT, build_chat_extract_user_prompt,
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


def _redact_secrets(text: str) -> str:
    """脱敏:把疑似密钥/token/密码替换为 [REDACTED_SECRET],防写入长期记忆。"""
    import re
    s = str(text or "")
    s = re.sub(r"\b(sk|pk|ghp|xox[bap])[-_][A-Za-z0-9]{8,}", "[REDACTED_SECRET]", s)
    s = re.sub(r"(?i)\b(token|password|passwd|pwd|secret|api[_-]?key|access[_-]?key)\s*[=:]\s*\S+",
               r"\1=[REDACTED_SECRET]", s)
    s = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[REDACTED_SECRET]", s)  # 长 hex/base64 串
    return s


def _clean_keywords(raw: Any) -> list[str]:
    """规整 keywords 字段为去重字符串列表(≤8 个)。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for k in raw:
        k = str(k).strip()
        if k and k not in out:
            out.append(k)
    return out[:8]


def extract_facts(task: str, trajectory: str, domain: str = "",
                  success: bool = True,
                  llm: LlmFn = _default_llm) -> list[dict[str, Any]]:
    """阶段一:抽取候选事实。

    success=True:成功任务 → preference / site_experience。
    success=False:失败任务 → lesson(可推翻的教训)。
    返回 [{content, memory_type, scope, domain, entry_url, intent_keywords, keywords, confidence}]。
    content 与 keywords 均过脱敏。
    """
    if success:
        result = llm(EXTRACT_SYSTEM_PROMPT, build_extract_user_prompt(task, trajectory, domain))
    else:
        result = llm(FAILURE_EXTRACT_SYSTEM_PROMPT,
                     build_failure_extract_user_prompt(task, trajectory, domain))
    if not result:
        return []
    facts = result.get("facts", [])
    if not isinstance(facts, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        content = _redact_secrets(str(f.get("content", "")).strip())
        if not content:
            continue
        keywords = _clean_keywords(f.get("keywords", []))

        if not success:
            # 失败任务只产 lesson,domain 作用域,低置信度=待验证
            cleaned.append({
                "content": content,
                "memory_type": MEMORY_TYPE_LESSON,
                "scope": SCOPE_DOMAIN,
                "domain": str(f.get("domain", "")).strip() or domain,
                "entry_url": "",
                "intent_keywords": [],
                "keywords": keywords,
                "confidence": LESSON_INIT_CONFIDENCE,
            })
            continue

        # 成功任务:preference / site_experience
        mtype = f.get("memory_type", MEMORY_TYPE_SITE_EXPERIENCE)
        if mtype not in (MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_SITE_EXPERIENCE):
            mtype = MEMORY_TYPE_SITE_EXPERIENCE  # 拿不准归按需层
        if mtype == MEMORY_TYPE_PREFERENCE:
            cleaned.append({
                "content": content,
                "memory_type": MEMORY_TYPE_PREFERENCE,
                "scope": SCOPE_GLOBAL,
                "domain": "",
                "entry_url": "",
                "intent_keywords": [],
                "keywords": keywords,
                "confidence": 1.0,
            })
        else:
            kws = f.get("intent_keywords", [])
            cleaned.append({
                "content": content,
                "memory_type": MEMORY_TYPE_SITE_EXPERIENCE,
                "scope": SCOPE_DOMAIN,
                "domain": str(f.get("domain", "")).strip() or domain,
                "entry_url": str(f.get("entry_url", "")).strip(),
                "intent_keywords": [str(k).strip() for k in kws if str(k).strip()] if isinstance(kws, list) else [],
                "keywords": keywords,
                "confidence": 1.0,
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
            memory_type=fact_meta.get("memory_type", MEMORY_TYPE_SITE_EXPERIENCE),
            scope=fact_meta.get("scope", SCOPE_GLOBAL),
            domain=fact_meta.get("domain", ""),
            user_id=user_id,
            confidence=fact_meta.get("confidence", 1.0),
            verified=False,
            entry_url=fact_meta.get("entry_url", ""),
            intent_keywords=fact_meta.get("intent_keywords", []),
            keywords=fact_meta.get("keywords", []),
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
        # 矛盾时标记失效而非物理删除(可回溯,对齐 Zep 双时间;chat+agent 统一)
        V.invalidate_memory(real_id)
        H.add_history(real_id, "INVALIDATE", prev.get("content", ""), "")
        return f"INVALIDATE {real_id}"

    return None  # NONE 或未知


def write_memory(task: str, trajectory: str, domain: str = "",
                 success: bool = True,
                 user_id: str = DEFAULT_USER_ID, llm: LlmFn = _default_llm) -> dict[str, Any]:
    """完整两阶段写入。返回 {facts, applied:[...], skipped_hash:int}。

    success=True:成功任务 → preference/site_experience;False:失败任务 → lesson。
    整个流程对异常宽容:任一环节失败都不抛,返回已完成的部分(记忆写入是尽力而为)。
    """
    result: dict[str, Any] = {"facts": [], "applied": [], "skipped_hash": 0}

    # 阶段一:抽取(按 success 分流成功/失败 prompt)
    try:
        facts = extract_facts(task, trajectory, domain, success=success, llm=llm)
    except Exception:
        return result
    result["facts"] = facts
    if not facts:
        return result

    # 阶段二:消化(检索相似→去重→决策→落库)
    _consolidate_facts(facts, user_id=user_id, llm=llm, result=result)
    return result


def _consolidate_facts(facts: list[dict[str, Any]], *, user_id: str,
                       llm: LlmFn, result: dict[str, Any]) -> None:
    """阶段二:对每条事实检索相似旧记忆 → hash 去重 → LLM 决策 → 落库。

    逐条事实处理,每条独立的临时 id 空间(反幻觉)。就地累加到 result["applied"]/["skipped_hash"]。
    agent 写入(write_memory)与 chat 写入(write_chat_memory)共用此主体。
    """
    for fact in facts:
        content = fact["content"]
        memory_type = fact.get("memory_type", MEMORY_TYPE_SITE_EXPERIENCE)
        scope = fact.get("scope", SCOPE_GLOBAL)
        domain_f = fact.get("domain", "")
        try:
            query_vec = embed_text(content)
            similar = V.search_memories(
                query_vec, query_text=content, top_k=WRITE_SEARCH_TOP_K, user_id=user_id,
                memory_type=memory_type,
                scope=scope, domain=(domain_f or None) if scope == SCOPE_DOMAIN else None,
            )
        except Exception:
            similar = []

        # hash 去重:与已有完全相同 → 跳过(连决策 LLM 都不调)
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
            action = _apply_decision("ADD", "", content, uuid_mapping, fact, user_id)
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


# ═══════════════════════════════════════════════════════════════════════════════
# chat 写入:从对话抽 persona/preference/episodic(单 prompt,无成败分流)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_chat_facts(user_msg: str, assistant_msg: str,
                       history_summary: str = "",
                       llm: LlmFn = _default_llm) -> list[dict[str, Any]]:
    """chat 抽取阶段:从一轮(或多轮摘要+最近一轮)对话抽 persona/preference/episodic。

    返回 [{content, memory_type, scope, domain, keywords, confidence}]。content/keywords 过脱敏。
    persona/preference→scope=global 常驻;episodic→scope=global 按需检索。domain 一律空(chat 无站点)。
    """
    result = llm(CHAT_EXTRACT_SYSTEM_PROMPT,
                 build_chat_extract_user_prompt(user_msg, assistant_msg, history_summary))
    if not result:
        return []
    facts = result.get("facts", [])
    if not isinstance(facts, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        content = _redact_secrets(str(f.get("content", "")).strip())
        if not content:
            continue
        mtype = f.get("memory_type", MEMORY_TYPE_EPISODIC)
        if mtype not in (MEMORY_TYPE_PERSONA, MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_EPISODIC):
            mtype = MEMORY_TYPE_EPISODIC  # 拿不准归按需层(不轻进常驻 core)
        cleaned.append({
            "content": content,
            "memory_type": mtype,
            "scope": SCOPE_GLOBAL,
            "domain": "",
            "entry_url": "",
            "intent_keywords": [],
            "keywords": _clean_keywords(f.get("keywords", [])),
            "confidence": 1.0,
        })
    return cleaned


def write_chat_memory(user_msg: str, assistant_msg: str,
                      history_summary: str = "",
                      user_id: str = CHAT_USER_ID, llm: LlmFn = _default_llm) -> dict[str, Any]:
    """chat 完整两阶段写入。返回 {facts, applied:[...], skipped_hash:int}。

    阶段一:extract_chat_facts;阶段二:复用 _consolidate_facts(检索相似→去重→决策→落库,
    矛盾走失效)。对异常宽容,记忆写入是尽力而为。
    """
    result: dict[str, Any] = {"facts": [], "applied": [], "skipped_hash": 0}
    try:
        facts = extract_chat_facts(user_msg, assistant_msg, history_summary, llm=llm)
    except Exception:
        return result
    result["facts"] = facts
    if not facts:
        return result
    _consolidate_facts(facts, user_id=user_id, llm=llm, result=result)
    return result
