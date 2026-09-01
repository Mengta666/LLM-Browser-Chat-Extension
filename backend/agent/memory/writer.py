"""记忆写入器(两阶段 LLM,批次 D 重构为单一 CONSOLIDATE 决策)。

阶段一 extract:从对话抽取候选事实(每条含 stability_score 稳定度打分)。
阶段二 consolidate:对候选事实,一次 LLM 调用判 skip/add/update/delete/promote 五 action。

反幻觉核心:给 LLM 的候选记忆只带临时整数 id("0","1"...),LLM 输出的 target_ids
只能是这些整数;代码用 uuid_mapping 映射回真实 memory_id。LLM 永远碰不到真实 UUID。
promote 新条以 core 直接落库、target_ids 兄弟软失效(合并了旧 detect_recurrence 逻辑)。
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
    WRITE_SEARCH_TOP_K, CHAT_USER_ID,
    MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC,
    SCOPE_GLOBAL,
)
from agent.memory.prompts import (
    CONSOLIDATE_SYSTEM_PROMPT, build_consolidate_user_prompt,
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


def _importance_to_confidence(raw: Any) -> float:
    """LLM importance(1-10)→ confidence 存储位(0.1-1.0)。缺省/异常回退 0.5(中性)。

    confidence 字段兼作 importance:供检索三因子重排与 episodic GC 排序共用。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.5
    v = max(1.0, min(10.0, v))
    return round(v / 10.0, 3)


def _norm_stability(raw: Any) -> float:
    """LLM stability_score(0.0-1.0)→ 归一。缺省/异常/越界回退 0.5(中性)。

    与 confidence(importance)独立:stability_score 表达"主题多稳定",供 consolidate LLM
    判 promote 时使用。0.85+ 视为长期稳定属性,0.6-0.85 是稳定主题含时序细节,
    0.3-0.6 是项目进展快照。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.5
    v = max(0.0, min(1.0, v))
    return round(v, 3)


def _norm_subject(raw: Any) -> str:
    """LLM subject 主题短语归一化(≤64 字符,strip)。非 str/None/异常返回空串。

    批次 E · P1:subject 是 CONSOLIDATE 候选拉取的第二条腿(embedding 之外),
    用于补 embedding 相似度低但同主题的漏检。空串表"未抽出",不进 subject 副通道。
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip().replace("\n", " ")[:64]


def _norm_expires_at(raw: Any) -> str:
    """LLM expires_at ISO 时间归一化。非法返回空串。

    批次 E · P2:expires_at 是"预计失效时刻"(未来),与 invalid_at(已失效时刻,过去)
    语义分工。EXTRACT 明示时限才 set,大多留空;rethink 判 expires_at < now 归 expired。
    宽容解析:接受 "2026-09-07T00:00:00Z" / "2026-09-07T00:00:00+00:00" / "2026-09-07" 等常见格式;
    非 str 或解析失败 → 空串。
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    # 兼容 Z 后缀(fromisoformat 3.11 前不支持 Z)
    s_norm = s[:-1] + "+00:00" if s.endswith("Z") else s
    from datetime import datetime as _dt
    try:
        # 允许纯日期(转成午夜 UTC)
        if len(s_norm) == 10 and s_norm.count("-") == 2:
            _dt.fromisoformat(s_norm + "T00:00:00+00:00")
            return s_norm + "T00:00:00+00:00"
        _dt.fromisoformat(s_norm)
        return s_norm
    except (ValueError, TypeError):
        return ""


def _apply_decision_d(action: str, target_ids_raw: list[str], canonical_content: str,
                      uuid_mapping: dict[str, str], fact_meta: dict[str, Any],
                      user_id: str, chat_id: str = "") -> Optional[str]:
    """批次 D:统一决策 apply。五 action:skip/add/update/delete/promote。

    - skip:不做落库(hash 去重之外的软等价,LLM 判)。target_ids 仅用于日志追溯。
    - add:插入新事实,memory_type 依 fact_meta 而定(core→chat_id="",episodic→chat_id=X)。
    - update:target_ids[0] 指向的旧条被 canonical_content 覆盖(保留其 memory_type)。
      跨类污染守卫:episodic 事实不许 UPDATE 一条 core。
    - delete:target_ids 里所有旧条软失效(可回溯);同时以新事实 ADD 一条替代。
      跨类护栏:target 是 verified=True 的 core 时,拒绝 delete(手动条只能用户手动删)。
    - promote:target_ids 里的 episodic 兄弟集全部软失效;canonical_content 以 core
      直接落库(memory_type=core, chat_id="", confidence 拉到 max(0.9, importance),
      stability_score 拉到 max(0.85, extracted),promoted_from=当前 chat_id)。

    反幻觉:target_ids 只能是 uuid_mapping 里的临时整数,不在的忽略。
    """
    action = str(action or "").lower()

    # 解析 target_ids → 真实 memory_id 列表(过滤掉不在 mapping 里的编造 id)
    resolved: list[str] = []
    for t in target_ids_raw or []:
        real = uuid_mapping.get(str(t))
        if real:
            resolved.append(real)

    # ── skip ─────────────────────────────────────────────
    if action == "skip":
        # 不落库,不改库;日志留痕
        return f"SKIP (targets={resolved})" if resolved else "SKIP"

    # ── add ─────────────────────────────────────────────
    if action == "add":
        text = canonical_content
        if not text:
            return None
        # episodic → chat_id=X 会话隔离;core → chat_id="" 全局
        fact_type = fact_meta.get("memory_type", MEMORY_TYPE_EPISODIC)
        fact_chat_id = chat_id if fact_type == MEMORY_TYPE_EPISODIC else ""
        payload = V.insert_memory(
            text, vector=embed_text(text),
            memory_type=fact_type,
            scope=fact_meta.get("scope", SCOPE_GLOBAL),
            user_id=user_id,
            confidence=fact_meta.get("confidence", 0.5),
            verified=False,
            keywords=fact_meta.get("keywords", []),
            chat_id=fact_chat_id,
            stability_score=fact_meta.get("stability_score", 0.5),
            subject=fact_meta.get("subject", ""),
            expires_at=fact_meta.get("expires_at", ""),
        )
        H.add_history(payload["memory_id"], "ADD", "", text)
        return f"ADD {payload['memory_id']}"

    # ── update ──────────────────────────────────────────
    if action == "update":
        if not resolved or not canonical_content:
            return None
        real_id = resolved[0]  # 通常 UPDATE 只对一个目标
        prev = V.get_memory(real_id)
        if prev is None:
            return None
        # 跨类污染守卫:episodic 事实不许 UPDATE 一条 core
        if (fact_meta.get("memory_type") == MEMORY_TYPE_EPISODIC
                and prev.get("memory_type") == MEMORY_TYPE_CORE):
            return f"REJECT_CROSS_UPDATE {real_id}"
        updated = V.update_memory(real_id, canonical_content, vector=embed_text(canonical_content))
        if updated is None:
            return None
        H.add_history(real_id, "UPDATE", prev.get("content", ""), canonical_content)
        return f"UPDATE {real_id}"

    # ── delete ──────────────────────────────────────────
    if action == "delete":
        if not resolved:
            return None
        # verified 手动 core 保护(见审查 #12)
        invalidated_ids = []
        for real_id in resolved:
            prev = V.get_memory(real_id)
            if prev is None:
                continue
            if prev.get("verified") and prev.get("memory_type") == MEMORY_TYPE_CORE:
                # 手动 core 不允许 LLM 自动删,只能用户手动删
                continue
            V.invalidate_memory(real_id)
            H.add_history(real_id, "INVALIDATE", prev.get("content", ""), "")
            invalidated_ids.append(real_id)
        # 同时 ADD 新事实(替代)
        if canonical_content:
            fact_type = fact_meta.get("memory_type", MEMORY_TYPE_EPISODIC)
            fact_chat_id = chat_id if fact_type == MEMORY_TYPE_EPISODIC else ""
            new_payload = V.insert_memory(
                canonical_content, vector=embed_text(canonical_content),
                memory_type=fact_type,
                scope=fact_meta.get("scope", SCOPE_GLOBAL),
                user_id=user_id,
                confidence=fact_meta.get("confidence", 0.5),
                verified=False,
                keywords=fact_meta.get("keywords", []),
                chat_id=fact_chat_id,
                stability_score=fact_meta.get("stability_score", 0.5),
                subject=fact_meta.get("subject", ""),
                expires_at=fact_meta.get("expires_at", ""),
            )
            H.add_history(new_payload["memory_id"], "ADD", "", canonical_content)
            return f"INVALIDATE {invalidated_ids} + ADD {new_payload['memory_id']}"
        return f"INVALIDATE {invalidated_ids}"

    # ── promote ─────────────────────────────────────────
    if action == "promote":
        # 直接以 core 形式落库(不经过 episodic 中转)
        text = canonical_content
        if not text:
            return None
        # confidence 拉到 max(0.9, fact 的 importance)
        boosted_confidence = max(0.9, float(fact_meta.get("confidence", 0.5)))
        # stability_score 拉到 max(0.85, fact 的 stability)
        boosted_stability = max(0.85, float(fact_meta.get("stability_score", 0.5)))
        new_payload = V.insert_memory(
            text, vector=embed_text(text),
            memory_type=MEMORY_TYPE_CORE,       # 直接落 core
            scope=SCOPE_GLOBAL,
            user_id=user_id,
            confidence=boosted_confidence,
            verified=False,
            keywords=fact_meta.get("keywords", []),
            chat_id="",                          # core 全局
            promoted_from=chat_id,               # 记原会话供 demote 回退
            stability_score=boosted_stability,
            subject=fact_meta.get("subject", ""),
            # promote 的 core 是稳定核心事实,不继承 fact 的 expires_at(留空)
        )
        H.add_history(new_payload["memory_id"], "PROMOTE", "", text)
        # 兄弟软失效(target_ids 里的 episodic)
        for real_id in resolved:
            try:
                sib = V.get_memory(real_id)
                # 失效前检查:并发下 sib 可能刚被另一线程升 core,跳过
                if sib and sib.get("memory_type") != MEMORY_TYPE_CORE:
                    V.invalidate_memory(real_id)
            except Exception:
                pass
        return f"PROMOTE {new_payload['memory_id']} (siblings={resolved})"

    return None  # 未知 action


def _consolidate_facts(facts: list[dict[str, Any]], *, user_id: str, chat_id: str,
                       llm: LlmFn, result: dict[str, Any]) -> None:
    """阶段二:对每条事实检索相似旧记忆 → hash 去重 → LLM 决策 → 落库。

    逐条事实处理,每条独立的临时 id 空间(反幻觉)。就地累加到 result["applied"]/["skipped_hash"]。
    去重作用域按类型分流:episodic 仅在本会话(chat_id)内比对,core 在全局
    (chat_id="")内比对——避免跨会话误去重,也让 core 的矛盾/更新作用于全局。
    """
    for fact in facts:
        content = fact["content"]
        memory_type = fact.get("memory_type", MEMORY_TYPE_EPISODIC)
        scope = fact.get("scope", SCOPE_GLOBAL)
        fact_subject = fact.get("subject", "")
        # D 版:候选池不再按 fact 类型分流,一次拉全部相似记忆(跨 chat + 跨 memory_type)
        # 让 CONSOLIDATE LLM 看到完整上下文自主判 action(含 promote)
        try:
            query_vec = embed_text(content)
            # 通道 A(embedding):hybrid dense+sparse RRF top 20
            similar = V.search_memories(
                query_vec, query_text=content, top_k=WRITE_SEARCH_TOP_K * 2,  # 20
                user_id=user_id, memory_type=None,      # 关键:不限类型
                chat_id=None,                            # 关键:跨会话
                include_invalid=True,                    # 含被 GC 的,防漏计数
            )
            # 通道 B(批次 E · P1 · subject 硬匹配):
            # 补 embedding 相似度低但同 subject 的漏检("改用英文"↔"用户希望用中文")。
            # 仅当 fact 抽出非空 subject 时启用;空 subject 退化到通道 A 单路。
            if fact_subject:
                try:
                    subject_matches = V.scroll_memories(
                        user_id=user_id, subject=fact_subject,
                        include_invalid=True, limit=WRITE_SEARCH_TOP_K * 2,
                    )
                except Exception:
                    subject_matches = []
                # merge by memory_id(通道 A 先来的保留 score,通道 B 新的补进来)
                seen_ids = {str(m.get("memory_id", "")) for m in similar}
                for m in subject_matches:
                    mid = str(m.get("memory_id", ""))
                    if mid and mid not in seen_ids:
                        similar.append(m)
                        seen_ids.add(mid)
        except Exception:
            similar = []

        # hash 去重:与已有完全相同 → 跳过(连 CONSOLIDATE LLM 都不调)
        new_hash = V.content_hash(content)
        if any(m.get("hash") == new_hash for m in similar):
            result["skipped_hash"] += 1
            continue

        # 构造临时 id 映射(反幻觉)+ 完整候选结构(带类型/chat_id/稳定度/subject)
        uuid_mapping: dict[str, str] = {}
        candidates_for_llm: list[dict[str, Any]] = []
        for idx, mem in enumerate(similar):
            uuid_mapping[str(idx)] = str(mem.get("memory_id", ""))
            candidates_for_llm.append({
                "id": str(idx),
                "content": str(mem.get("content", "")),
                "chat_id": str(mem.get("chat_id", "")),
                "memory_type": str(mem.get("memory_type", "")),
                "stability_score": float(mem.get("stability_score", 0.5)),
                "subject": str(mem.get("subject", "")),
                "valid": bool(mem.get("valid", True)),
            })

        # 单一 CONSOLIDATE LLM 判五 action
        decision = llm(CONSOLIDATE_SYSTEM_PROMPT,
                       build_consolidate_user_prompt(fact, candidates_for_llm, chat_id))
        if not decision:
            # 决策失败 → 保守 add(不冒 update/delete/promote 风险)
            action = _apply_decision_d(
                "add", [], content, uuid_mapping, fact, user_id, chat_id)
            if action:
                result["applied"].append(action)
            continue

        action = _apply_decision_d(
            str(decision.get("action", "add")).lower(),
            decision.get("target_ids", []) or [],
            str(decision.get("canonical_content", "") or content).strip(),
            uuid_mapping, fact, user_id, chat_id,
        )
        if action:
            result["applied"].append(action)


# ═══════════════════════════════════════════════════════════════════════════════
# chat 写入:从对话抽 core/episodic(单 prompt,无成败分流)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_chat_facts(user_msg: str, assistant_msg: str,
                       history_summary: str = "",
                       llm: LlmFn = _default_llm) -> list[dict[str, Any]]:
    """chat 抽取阶段:从一轮(或多轮摘要+最近一轮)对话抽 core/episodic。

    返回 [{content, memory_type, scope, domain, keywords, confidence, stability_score}]。
    content/keywords 过脱敏。core→scope=global 常驻;episodic→scope=global 按需检索。
    domain 一律空(chat 无站点)。
    stability_score(批次 D):独立于 memory_type 的稳定度打分,供 consolidate 判 promote。
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
        if mtype not in (MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC):
            mtype = MEMORY_TYPE_EPISODIC  # 拿不准归按需层(不轻进常驻 core)
        cleaned.append({
            "content": content,
            "memory_type": mtype,
            "scope": SCOPE_GLOBAL,
            "domain": "",
            "entry_url": "",
            "intent_keywords": [],
            "keywords": _clean_keywords(f.get("keywords", [])),
            "confidence": _importance_to_confidence(f.get("importance")),
            "stability_score": _norm_stability(f.get("stability_score")),
            "subject": _norm_subject(f.get("subject")),
            "expires_at": _norm_expires_at(f.get("expires_at")),
        })
    return cleaned


def write_chat_memory(user_msg: str, assistant_msg: str,
                      history_summary: str = "",
                      user_id: str = CHAT_USER_ID, chat_id: str = "",
                      llm: LlmFn = _default_llm) -> dict[str, Any]:
    """chat 完整两阶段写入。返回 {facts, applied:[...], skipped_hash:int}。

    阶段一:extract_chat_facts;阶段二:复用 _consolidate_facts(检索相似→去重→决策→落库,
    矛盾走失效)。chat_id 用于 episodic 会话隔离(core 仍落全局)。
    对异常宽容,记忆写入是尽力而为。
    """
    result: dict[str, Any] = {"facts": [], "applied": [], "skipped_hash": 0}
    try:
        facts = extract_chat_facts(user_msg, assistant_msg, history_summary, llm=llm)
    except Exception:
        return result
    result["facts"] = facts
    if not facts:
        return result
    _consolidate_facts(facts, user_id=user_id, chat_id=chat_id, llm=llm, result=result)
    return result
