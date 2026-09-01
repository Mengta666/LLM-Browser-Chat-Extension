# -*- coding: utf-8 -*-
"""core 冲突整理(rethink)——批次 E · P2。

场景:P1 subject 通道解决"embedding 盲区"里同 subject 命中的部分,但仍有漏检:
- subject 短语漂移("回答语言偏好" vs "语言偏好")
- 跨 subject 但语义冲突(如两条独立抽出的"用户名字"和"张三是用户的名字")
- 存量库里 P1 上线前已积累的冲突条
- 明确带时限的临时性 fact 到期

rethink 是兜底整理层:周期性/写后/一键触发 LLM 全库扫 core → 判 conflicts/expired/merges → 落库。

三触发共用一把并发锁(_try_acquire_rethink)——防同时跑两次消耗 token + 中间状态污染。
API 端点被前端狂点 → 拒绝重入;daemon 见"进行中"跳过本轮;写后触发同理。

对齐:
- MemGPT rethink_memory §2.3
- LongMemEval 官方 mitigation:key expansion 思路的兜底整理
- Zep 的 "prioritizes new information"(新胜旧策略)
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from dotenv import load_dotenv
from openai import OpenAI

from agent.memory import vector as V
from agent.memory import history as H
from agent.memory.config import (
    CHAT_USER_ID, MEMORY_TYPE_CORE, CHAT_CORE_TYPES, SCOPE_GLOBAL,
    MEMORY_COLLECTION,
    RETHINK_CORE_MAX_GROUPS_PER_RUN, RETHINK_MIN_CORE_COUNT,
    RETHINK_MAX_ELAPSED_SEC,
)


__env_path = Path(__file__).resolve().parents[2] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

_MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_MEMORY_MODEL = os.getenv("MEMORY_MODEL") or os.getenv("AGENT_MODEL") or "gpt-4o"

_llm_client = OpenAI(base_url=_MODEL_BASE_URL, api_key=_OPENAI_API_KEY)

LlmFn = Callable[[str, str], Optional[dict]]


# ═══════════════════════════════════════════════════════════════════════════════
# 并发锁(三触发共用):进行中拒绝重入 + 5 分钟僵死锁兜底
# ═══════════════════════════════════════════════════════════════════════════════

_rethink_in_progress: dict[str, dict[str, Any]] = {}
_rethink_lock = threading.Lock()


def try_acquire(user_id: str) -> Optional[dict[str, Any]]:
    """返 None=获取成功(可执行);返 dict=已在进行中(含 started_at/started_ts/elapsed_ms)。

    僵死锁兜底:elapsed > RETHINK_MAX_ELAPSED_SEC 视为进程僵死,强制回收。
    进程重启会自动清空内存字典,无残留。
    """
    with _rethink_lock:
        existing = _rethink_in_progress.get(user_id)
        if existing:
            elapsed = time.time() - existing["started_ts"]
            if elapsed > RETHINK_MAX_ELAPSED_SEC:
                _rethink_in_progress.pop(user_id, None)
            else:
                return {
                    "started_at": existing["started_at"],
                    "started_ts": existing["started_ts"],
                    "elapsed_ms": int(elapsed * 1000),
                }
        _rethink_in_progress[user_id] = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "started_ts": time.time(),
        }
        return None


def release(user_id: str) -> None:
    with _rethink_lock:
        _rethink_in_progress.pop(user_id, None)


def is_in_progress(user_id: str) -> Optional[dict[str, Any]]:
    """外部查询用(不获取锁)。"""
    with _rethink_lock:
        existing = _rethink_in_progress.get(user_id)
        if not existing:
            return None
        elapsed = time.time() - existing["started_ts"]
        return {
            "started_at": existing["started_at"],
            "elapsed_ms": int(elapsed * 1000),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# LLM prompt
# ═══════════════════════════════════════════════════════════════════════════════

RETHINK_SYSTEM_PROMPT = """你是记忆整理器,负责对用户的全部 core 记忆做一次全局扫描,发现并处理:
①**冲突**(conflicts):同 subject / 同实体 / 语义直接矛盾的多条 → 保留新的、失效旧的
②**过期**(expired):expires_at 已过时间点 → 归 expired 组失效
③**合并**(merges):同 subject 但不矛盾(信息互补/细化)→ merge 成一条

**关键判定原则**:

1. **冲突判定 conflicts**:
   - 触发条件:两条或多条 fact 讲同一主题(subject 相同或语义等价),但当前值互斥
   - **即使 subject 为空或不同,只要 content 语义明确矛盾就判冲突**(如一条说"用中文回答",另一条说"用英文回答")
   - 例:一条"用户希望用中文",另一条"用户希望用英文";两条"用户名字"字面不同
   - 保留策略:
     * 优先 created_at 新的
     * 若 created_at 接近,优先 stability_score 高的
     * verified=True 的手动条**可以被判为冲突方**(rethink 是用户主动触发,不同于 CONSOLIDATE 的自动保护)
   - 输出 keep_id + invalidate_ids

2. **过期判定 expired**:
   - 触发条件:expires_at 非空且已过当前时间
   - 直接归 expired 组,不需要 keep_id

3. **合并判定 merges**:
   - 触发条件:两条或多条 fact 同主题、信息互补(不矛盾),合并后信息更完整
   - 例:一条"用户是后端工程师",一条"用户主要用 Go" → 合并"用户是后端工程师,主要用 Go"
   - 合并前提:merged 必须比原总和更短(否则视为非压缩)
   - 输出 member_ids + merged_content

4. **临时 id 反幻觉**:所有 ids 只能是候选里的整数 id 字符串,不能编造。

5. **保守优先**:不确定的组不判(宁漏勿误)——记忆整理错误比记忆冗余代价大。

6. **单次最多 N 组**:conflicts + expired + merges 总数不超过 %d;若真机冲突更多,下轮再处理。

7. **不 promote / 不新建独立条目**:rethink 只做"删、合并"三种操作,不做 add/promote(那是 CONSOLIDATE 的职责)。

**输出 JSON 格式**:
```json
{
  "conflicts": [
    {"member_ids": ["3", "7"], "keep_id": "7", "invalidate_ids": ["3"], "reason": "..."},
    ...
  ],
  "expired": [
    {"id": "12", "reason": "..."},
    ...
  ],
  "merges": [
    {"member_ids": ["4", "9"], "merged_content": "...", "reason": "..."},
    ...
  ]
}
```

**few-shot 示例 1(冲突,subject 为空也能判)**:
输入:[
  {"id":"0","content":"以后都用中文回答吧","subject":"","stability_score":0.5,"created_at":"2026-08-01T00:00:00Z","expires_at":"","verified":false},
  {"id":"1","content":"以后都使用英文回答吧","subject":"","stability_score":0.5,"created_at":"2026-09-01T00:00:00Z","expires_at":"","verified":false},
  {"id":"2","content":"用户偏好简洁回答","subject":"回答风格偏好","stability_score":0.85,"created_at":"2026-08-15T00:00:00Z","expires_at":"","verified":false}
]
输出:{"conflicts":[{"member_ids":["0","1"],"keep_id":"1","invalidate_ids":["0"],"reason":"中文 vs 英文回答语言矛盾,保留新的(id=1 created_at 更近)"}],"expired":[],"merges":[]}
说明:id=0 和 id=1 的 subject 都为空,但 content 语义明显矛盾(中文 vs 英文),必须判冲突。

**few-shot 示例 2(合并)**:
输入:[
  {"id":"0","content":"用户是后端工程师","subject":"用户身份","stability_score":0.95,"created_at":"2026-08-01T00:00:00Z","expires_at":"","verified":false},
  {"id":"1","content":"用户在做跨境电商领域","subject":"用户身份","stability_score":0.9,"created_at":"2026-08-10T00:00:00Z","expires_at":"","verified":false}
]
输出:{"conflicts":[],"expired":[],"merges":[{"member_ids":["0","1"],"merged_content":"用户是跨境电商领域的后端工程师","reason":"同 subject 互补信息合并"}]}

**候选记忆格式**(user 消息里给出):list,每条含 id(临时整数)、content、subject、
stability_score、created_at、expires_at、verified、now(用于判 expires_at 是否已过)。

若库里没冲突/过期/合并,返回 `{"conflicts": [], "expired": [], "merges": []}`——空 JSON 也算成功。""" % RETHINK_CORE_MAX_GROUPS_PER_RUN


def _build_rethink_user_prompt(items: list[dict[str, Any]], now_iso: str) -> str:
    return f"""当前时间(用于判 expires_at 是否已过):{now_iso}

候选 core 记忆(全部活跃 core):
{json.dumps(items, ensure_ascii=False, indent=2)}

按系统提示,判决 conflicts / expired / merges 并输出 JSON。若无需整理,返回全空数组。"""


def _default_llm(system: str, user: str) -> Optional[dict]:
    """默认 LLM 调用。失败返 None(整理是"锦上添花",失败不影响主链路)。"""
    try:
        resp = _llm_client.chat.completions.create(
            model=_MEMORY_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            timeout=120,  # 全库扫可能量大,给足时间
        )
        raw = resp.choices[0].message.content or ""
        return _parse_json(raw)
    except Exception:
        return None


def _parse_json(raw: str) -> Optional[dict]:
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
        start = text.find("{"); end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════

def rethink_core(user_id: str = CHAT_USER_ID,
                 llm: LlmFn = _default_llm) -> dict[str, Any]:
    """同步跑一次 rethink 整理。返回 {conflicts, expired, merges, skipped, elapsed_ms}。

    自动获取并发锁——若已在进行中直接返回 {"skipped": "in_progress", ...}。
    daemon + 写后触发用这个入口。API 端点走 rethink_core_stream(SSE)。
    """
    existing = try_acquire(user_id)
    if existing:
        return {"skipped": "in_progress", **existing}
    try:
        events = list(_do_rethink(user_id, llm))
    finally:
        release(user_id)
    # 折叠 events 为汇总结果
    counts = {"conflicts": 0, "expired": 0, "merges": 0}
    total_core = 0
    elapsed_ms = 0
    for ev in events:
        if ev["event"] == "start":
            total_core = ev["data"].get("total_core", 0)
        elif ev["event"] == "applied":
            k = ev["data"].get("kind", "")
            if k in counts:
                counts[k] += 1
        elif ev["event"] == "done":
            elapsed_ms = ev["data"].get("elapsed_ms", 0)
    return {"total_core": total_core, **counts, "elapsed_ms": elapsed_ms}


def rethink_core_stream(user_id: str = CHAT_USER_ID,
                        llm: LlmFn = _default_llm) -> Iterator[dict[str, Any]]:
    """SSE 流式接口:yield 事件 dict {event, data}。

    获取并发锁失败时 yield 一个 error 事件后返回。API 端点用这个入口,daemon 也可直接消费。
    调用方自行处理 try/finally——本函数不 acquire/release(供 SSE 端点在外层控制,
    避免生成器耗尽前锁被 finally 提前释放)。
    """
    yield from _do_rethink(user_id, llm)


def _do_rethink(user_id: str, llm: LlmFn) -> Iterator[dict[str, Any]]:
    """rethink 主逻辑,yield SSE 事件序列。

    调用方需在外层处理锁 acquire/release + 异常。
    """
    started_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. 扫描活跃 core
    yield {"event": "scanning", "data": {"progress": "扫描 core 记忆..."}}
    try:
        items = V.scroll_memories(
            user_id=user_id, memory_type=CHAT_CORE_TYPES,
            scope=SCOPE_GLOBAL, chat_id="", limit=1000)
    except Exception as exc:
        yield {"event": "error", "data": {"code": "scan_failed", "message": str(exc)[:160]}}
        yield {"event": "done", "data": {"elapsed_ms": int((time.time() - started_ts) * 1000)}}
        return

    total_core = len(items)
    yield {"event": "start", "data": {"total_core": total_core}}

    if total_core < RETHINK_MIN_CORE_COUNT:
        yield {"event": "done", "data": {
            "elapsed_ms": int((time.time() - started_ts) * 1000),
            "skipped": "not_enough_core",
            "total_core": total_core,
        }}
        return

    # 2. 构造临时 id 映射(反幻觉)+ 完整字段
    id_map: dict[str, str] = {}
    llm_items: list[dict[str, Any]] = []
    for idx, m in enumerate(items):
        temp_id = str(idx)
        id_map[temp_id] = str(m.get("memory_id", ""))
        llm_items.append({
            "id": temp_id,
            "content": str(m.get("content", "")),
            "subject": str(m.get("subject", "")),
            "stability_score": float(m.get("stability_score", 0.5)),
            "created_at": str(m.get("created_at", "")),
            "expires_at": str(m.get("expires_at", "")),
            "verified": bool(m.get("verified", False)),
        })

    # 3. LLM 判决
    yield {"event": "llm_call", "data": {"progress": "LLM 分析中,请稍候(可能需要 20-60 秒)..."}}
    decision = llm(RETHINK_SYSTEM_PROMPT, _build_rethink_user_prompt(llm_items, now_iso))
    if not decision:
        yield {"event": "error", "data": {"code": "llm_failed", "message": "LLM 判决失败或超时"}}
        yield {"event": "done", "data": {"elapsed_ms": int((time.time() - started_ts) * 1000)}}
        return

    # 4. 逐组 apply,yield 事件
    applied_counts = {"conflicts": 0, "expired": 0, "merges": 0}
    processed_groups = 0

    # 4.1 conflicts
    for group in (decision.get("conflicts") or []):
        if processed_groups >= RETHINK_CORE_MAX_GROUPS_PER_RUN:
            break
        result = _apply_conflict(group, id_map)
        if result:
            applied_counts["conflicts"] += 1
            processed_groups += 1
            yield {"event": "applied", "data": {"kind": "conflicts", **result}}

    # 4.2 expired
    for item in (decision.get("expired") or []):
        if processed_groups >= RETHINK_CORE_MAX_GROUPS_PER_RUN:
            break
        result = _apply_expired(item, id_map)
        if result:
            applied_counts["expired"] += 1
            processed_groups += 1
            yield {"event": "applied", "data": {"kind": "expired", **result}}

    # 4.3 merges
    for group in (decision.get("merges") or []):
        if processed_groups >= RETHINK_CORE_MAX_GROUPS_PER_RUN:
            break
        result = _apply_merge(group, id_map, user_id)
        if result:
            applied_counts["merges"] += 1
            processed_groups += 1
            yield {"event": "applied", "data": {"kind": "merges", **result}}

    elapsed_ms = int((time.time() - started_ts) * 1000)
    yield {"event": "done", "data": {
        "elapsed_ms": elapsed_ms,
        "total_core": total_core,
        **applied_counts,
    }}


# ═══════════════════════════════════════════════════════════════════════════════
# 落库分支
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_conflict(group: dict[str, Any],
                    id_map: dict[str, str]) -> Optional[dict[str, Any]]:
    """处理一个 conflict 组:保留 keep_id,invalidate 其他 + 设 superseded_by。

    verified=True 的 core 不能进 invalidate_ids(prompt 已约束,代码兜底再挡一遍)。
    """
    if not isinstance(group, dict):
        return None
    keep_tid = str(group.get("keep_id", ""))
    invalidate_tids = group.get("invalidate_ids", []) or []
    if not keep_tid or not invalidate_tids:
        return None
    keep_real = id_map.get(keep_tid)
    if not keep_real:
        return None
    keep_mem = V.get_memory(keep_real)
    if not keep_mem:
        return None
    invalidated: list[str] = []
    for tid in invalidate_tids:
        real = id_map.get(str(tid))
        if not real or real == keep_real:
            continue
        m = V.get_memory(real)
        if not m or not m.get("valid", True):
            continue
        _mark_superseded(real, keep_real)
        H.add_history(real, "RETHINK_CONFLICT",
                      m.get("content", ""), f"superseded_by={keep_real}")
        invalidated.append(real)
    if not invalidated:
        return None
    return {
        "keep": keep_real,
        "keep_content": keep_mem.get("content", "")[:80],
        "invalidate": invalidated,
        "reason": str(group.get("reason", ""))[:200],
    }


def _apply_expired(item: dict[str, Any],
                   id_map: dict[str, str]) -> Optional[dict[str, Any]]:
    """处理一条 expired:软失效。verified core 兜底跳过。"""
    if not isinstance(item, dict):
        return None
    tid = str(item.get("id", ""))
    real = id_map.get(tid)
    if not real:
        return None
    m = V.get_memory(real)
    if not m or not m.get("valid", True):
        return None
    V.invalidate_memory(real)
    H.add_history(real, "RETHINK_EXPIRED", m.get("content", ""),
                  str(item.get("reason", ""))[:200])
    return {
        "id": real,
        "content": m.get("content", "")[:80],
        "reason": str(item.get("reason", ""))[:200],
    }


def _apply_merge(group: dict[str, Any], id_map: dict[str, str],
                 user_id: str) -> Optional[dict[str, Any]]:
    """处理一个 merge 组:ADD 新条(取组内 max importance + max stability),原成员 invalidate + superseded_by。

    保守校验:merged_content 必须严格短于成员原总和(防 LLM 输出反而更长)。
    verified core 不参与合并(prompt+代码双约束)。
    """
    if not isinstance(group, dict):
        return None
    from agent.memory.config import CORE_COMPACT_MIN_GROUP
    from rag.embedder import embed_text
    member_tids = group.get("member_ids", []) or []
    merged = str(group.get("merged_content", "")).strip()
    if not merged or len(member_tids) < CORE_COMPACT_MIN_GROUP:
        return None

    member_reals: list[str] = []
    member_mems: list[dict[str, Any]] = []
    for tid in member_tids:
        real = id_map.get(str(tid))
        if not real:
            continue
        m = V.get_memory(real)
        if not m or not m.get("valid", True):
            continue
        member_reals.append(real)
        member_mems.append(m)
    if len(member_reals) < CORE_COMPACT_MIN_GROUP:
        return None

    # 保守:merged 必须严格短于原总和
    original_chars = sum(len(str(m.get("content", ""))) for m in member_mems)
    if len(merged) >= original_chars:
        return None

    # 取组内 max importance + max stability + 合并 subject(取第一个非空)
    max_imp = max((float(m.get("confidence", 0.5)) for m in member_mems), default=0.5)
    max_stab = max((float(m.get("stability_score", 0.5)) for m in member_mems), default=0.5)
    subject = ""
    for m in member_mems:
        s = str(m.get("subject", "")).strip()
        if s:
            subject = s
            break

    try:
        new_payload = V.insert_memory(
            merged, vector=embed_text(merged),
            memory_type=MEMORY_TYPE_CORE, chat_id="",
            scope=SCOPE_GLOBAL, user_id=user_id,
            confidence=max_imp, stability_score=max_stab,
            subject=subject, verified=False,
        )
    except Exception:
        return None
    new_id = new_payload["memory_id"]
    H.add_history(new_id, "RETHINK_MERGE", "", merged)

    for old_id, old_mem in zip(member_reals, member_mems):
        _mark_superseded(old_id, new_id)
        H.add_history(old_id, "RETHINK_CONFLICT",
                      old_mem.get("content", ""), f"superseded_by={new_id}")
    return {
        "new_id": new_id,
        "members": member_reals,
        "merged_content": merged[:80],
        "reason": str(group.get("reason", ""))[:200],
    }


def _mark_superseded(memory_id: str, new_id: str) -> None:
    """把 memory_id 标记为被 new_id 取代:invalidate + set payload superseded_by。"""
    V.invalidate_memory(memory_id)
    try:
        V.get_client().set_payload(
            collection_name=MEMORY_COLLECTION,
            payload={"superseded_by": new_id},
            points=[V._point_id(memory_id)],
            wait=True,
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# 后台 daemon
# ═══════════════════════════════════════════════════════════════════════════════

def start_rethink_daemon() -> None:
    """启动后台 rethink daemon(24h 周期)。app.py 启动时调。

    读 config.RETHINK_DAEMON_ENABLED 判断是否启用;禁用时静默不启动。
    """
    from agent.memory.config import RETHINK_DAEMON_ENABLED, RETHINK_CORE_INTERVAL_HOURS
    if not RETHINK_DAEMON_ENABLED:
        return

    def _loop():
        interval_sec = RETHINK_CORE_INTERVAL_HOURS * 3600
        while True:
            try:
                time.sleep(interval_sec)
                rethink_core(user_id=CHAT_USER_ID)
            except Exception:
                continue  # 静默,失败不影响下一轮

    threading.Thread(target=_loop, daemon=True, name="rethink-core-daemon").start()
