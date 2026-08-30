# -*- coding: utf-8 -*-
"""跨会话晋升:检测与执行(批次 B)。

核心机制(归并模型,推翻了初版的"计数字段模型"):
- detect_recurrence:某条新写入的 episodic,用 user 级 dense_scores 找跨会话候选,
  LLM 判"同一稳定事实 vs 同一主题不同进展",返回稳定事实的 sibling 集(含自己)。
- promote_memory:兄弟集覆盖 ≥ PROMOTE_THRESHOLD 个 distinct chat_id 时,
  选确定性 canonical(min(valid_ids))升 core + 其余兄弟软失效。
- demote_memory:回退。

关键设计:
- 复现判定用 dense_scores(直接给 cosine),不用 search_memories(它返 RRF 融合分)。
- include_invalid=True:被 GC 软失效的兄弟仍是复现证据,参与计数。
- canonical 只从 valid 兄弟中选(防选中 invalid 条,晋升白做)。
- 并发:最终一致 + 幂等收敛(不上锁),靠 min() + 幂等守卫 + 失效前检查三点。
- 晋升同时拉高 reinforce_count(防被 prune)和 confidence(保 -confidence 主导排序靠前)。
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
    CHAT_USER_ID, MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC,
    PROMOTE_THRESHOLD, PROMOTE_SIM_COSINE, PROMOTE_CONFIDENCE,
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


# ═══════════════════════════════════════════════════════════
# LLM 判定 prompt
# ═══════════════════════════════════════════════════════════
STABILITY_SYSTEM_PROMPT = """你是记忆归并判定器。给你一条新事实和若干候选记忆(每条带整数 id),
判定每条候选与新事实是否描述**同一个稳定事实**,而非**同一主题的不同进展**。

**稳定事实**特征:陈述用户长期不变的属性、身份、偏好
  例:"用户在做订单迁移项目" 与 "用户负责订单系统迁移" —— 同一稳定事实

**进展**特征:同一主题在不同时间点的状态、遇到的问题、决定、里程碑
  例:"订单迁移在做" · "订单迁移遇到并发瓶颈" · "订单迁移下周一上线"
        —— 三条都是订单迁移,但状态在演化,不是复现

判定原则:
- 事件在演化 → 进展,不算稳定事实
- 描述客观不变的属性/身份 → 稳定事实
- 拿不准 → 不算稳定事实(宁缺毋滥,避免误升)

few-shot 示例:

新事实:用户在做订单迁移项目
候选:[
  {"id": "0", "content": "用户负责订单系统迁移"},
  {"id": "1", "content": "订单迁移已定下周一上线"}
]
输出:{"stable": [{"id": "0"}]}
  # id=1 是进展不算

新事实:用户主要用 Go
候选:[
  {"id": "0", "content": "用户是后端工程师,主要写 Go"},
  {"id": "1", "content": "用户偶尔用 Python"}
]
输出:{"stable": [{"id": "0"}]}

新事实:订单迁移这周做压测
候选:[
  {"id": "0", "content": "订单迁移刚开始设计"},
  {"id": "1", "content": "订单迁移遇到瓶颈"}
]
输出:{"stable": []}
  # 三条都是进展,没有稳定事实

要求:只输出 JSON,格式 {"stable": [{"id"}]}。id 必须来自"候选"给出的整数 id。"""


def _build_stability_prompt(fact_content: str,
                            candidates: list[dict[str, str]]) -> str:
    parts = ["新事实:", fact_content, "", "候选:",
             json.dumps(candidates, ensure_ascii=False, indent=2)]
    return "\n".join(parts)


def _default_llm(system: str, user: str) -> Optional[dict]:
    """默认 LLM 调用。失败返 None(晋升是"锦上添花",失败不阻塞主链路)。"""
    try:
        resp = _llm_client.chat.completions.create(
            model=_MEMORY_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            timeout=60,
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


# ═══════════════════════════════════════════════════════════
# 链路一 · 复现检测
# ═══════════════════════════════════════════════════════════
def detect_recurrence(fact_content: str, current_chat_id: str,
                      user_id: str = CHAT_USER_ID,
                      llm: LlmFn = _default_llm) -> set[str]:
    """检测跨会话稳定事实的兄弟集(不含 current 那条自己,由调用方补上)。

    步骤:
    1. dense_scores 拉 user 级候选(cosine ≥ PROMOTE_SIM_COSINE)
       - chat_id=None 不限会话(关键 1:跨会话)
       - include_invalid=True 含被 GC 软失效的兄弟(关键 2:软失效仍是复现证据)
    2. LLM 判每条候选与 fact 是否为"同一稳定事实"
    3. 返回判为稳定事实的 memory_ids
    """
    try:
        query_vec = embed_text(fact_content)
    except Exception:
        return set()

    try:
        scores = V.dense_scores(
            query_vec, top_k=20, user_id=user_id,
            memory_type=MEMORY_TYPE_EPISODIC,
            chat_id=None,               # 关键 1:跨会话
            include_invalid=True,        # 关键 2:含 GC 兄弟
        )
    except Exception:
        return set()

    # 阈值过滤 + 排除当前自己(如已落库,memory_id 会出现在候选里)
    candidates = [(mid, cos) for mid, cos in scores.items()
                  if cos >= PROMOTE_SIM_COSINE]
    if not candidates:
        return set()

    # 组装 LLM 用的临时 id 映射(反幻觉:与 writer.py 同款)
    id_map: dict[str, str] = {}
    llm_items: list[dict[str, str]] = []
    for idx, (mid, _) in enumerate(candidates):
        m = V.get_memory(mid)
        if not m:
            continue
        temp_id = str(idx)
        id_map[temp_id] = mid
        llm_items.append({"id": temp_id, "content": str(m.get("content", ""))})
    if not llm_items:
        return set()

    # LLM 判定
    result = llm(STABILITY_SYSTEM_PROMPT,
                 _build_stability_prompt(fact_content, llm_items))
    if not result or not isinstance(result.get("stable"), list):
        return set()

    stable_ids: set[str] = set()
    for item in result["stable"]:
        if not isinstance(item, dict):
            continue
        temp = str(item.get("id", ""))
        real = id_map.get(temp)
        if real:
            stable_ids.add(real)
    return stable_ids


def distinct_chat_ids_of(memory_ids) -> set[str]:
    """统计一批 memory_id 覆盖的 distinct chat_id 集合。"""
    chats: set[str] = set()
    for mid in memory_ids:
        m = V.get_memory(str(mid))
        if m:
            chats.add(m.get("chat_id", ""))
    return chats


# ═══════════════════════════════════════════════════════════
# 链路二 · 达标晋升(canonical + 归并)
# ═══════════════════════════════════════════════════════════
def promote_memory(sibling_ids) -> Optional[str]:
    """归并式晋升:canonical 升 core + 其余兄弟软失效。

    - canonical = min(valid_ids):确定性选取,两线程同时达标也收敛同一条。
    - 幂等守卫:已升则直接返回 canonical id,不重复升。
    - 失效前检查:invalidate 兄弟前跳过已 CORE 的(防误伤刚被另一线程晋升的条)。
    - 晋升同时拉高 reinforce_count(防被 prune_global_preferences 物删)
      和 confidence(保 -confidence 主导排序靠前,与 §06 新排序一致)。

    返回 canonical memory_id;若无 valid 兄弟返回 None。
    """
    sibling_ids = list(sibling_ids)
    # 只从 valid 兄弟中选(避免选中被 GC 的 invalid 条 → 晋升出 valid=false 的 core → 静默不注入)
    valid_ids = []
    for s in sibling_ids:
        m = V.get_memory(s)
        if m and m.get("valid", True):
            valid_ids.append(s)
    if not valid_ids:
        return None

    canonical = min(valid_ids)              # 确定性 + 幂等收敛(免锁)
    m = V.get_memory(canonical)
    if not m:
        return None
    if m.get("memory_type") == MEMORY_TYPE_CORE:
        return canonical                     # 幂等守卫:已升,跳过

    # 晋升 canonical:改 5 字段
    try:
        V.get_client().set_payload(
            collection_name=MEMORY_COLLECTION,
            payload={
                "chat_id": "",                     # 转全局
                "memory_type": MEMORY_TYPE_CORE,   # 升 core
                "promoted_from": m.get("chat_id", ""),  # 记原会话供 demote 回退
                "reinforce_count": max(
                    int(m.get("reinforce_count", 0)), PROMOTE_THRESHOLD),
                "confidence": max(
                    float(m.get("confidence", 0.5)), PROMOTE_CONFIDENCE),
            },
            points=[V._point_id(canonical)], wait=True)
    except Exception:
        return None

    # 其余兄弟软失效(杜绝与 canonical 双注入)
    for sid in valid_ids:
        if sid == canonical:
            continue
        sib = V.get_memory(sid)
        # 失效前检查:并发下另一线程可能刚升 sib,跳过
        if sib and sib.get("memory_type") != MEMORY_TYPE_CORE:
            try:
                V.invalidate_memory(sid)
            except Exception:
                pass

    try:
        H.add_history(canonical, "PROMOTE", "", m.get("content", ""))
    except Exception:
        pass
    return canonical


def demote_memory(memory_id: str) -> bool:
    """晋升回退:core → episodic,chat_id 恢复到 promoted_from。

    供记忆面板"撤销晋升"UI 或误升的手动修复。仅当 promoted_from 非空(即真的是晋升上来的)才回退。
    """
    m = V.get_memory(memory_id)
    if not m or not m.get("promoted_from"):
        return False
    try:
        V.get_client().set_payload(
            collection_name=MEMORY_COLLECTION,
            payload={
                "chat_id": m["promoted_from"],
                "memory_type": MEMORY_TYPE_EPISODIC,
                "promoted_from": "",
            },
            points=[V._point_id(memory_id)], wait=True)
        H.add_history(memory_id, "DEMOTE", "", m.get("content", ""))
        return True
    except Exception:
        return False
