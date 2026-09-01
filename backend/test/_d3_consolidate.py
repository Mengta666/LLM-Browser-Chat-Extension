# -*- coding: utf-8 -*-
"""D3 验证:CONSOLIDATE_SYSTEM_PROMPT 五 action 判定的复现测试。

9 条 case 覆盖 skip/add/update/delete/promote 五 action,含边界 case。
每条 case seed 特定候选 + trigger 一条 fact,断言 LLM 判定 action 与预期一致。
PASS 判据 >= 7/9(允许 2 条边缘偏差)。
"""
import sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8")
except: pass
sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from agent.memory import writer as W
from agent.memory.prompts import CONSOLIDATE_SYSTEM_PROMPT, build_consolidate_user_prompt

# 9 条 case,直接构造 candidates dict(不走 Qdrant seed,单测 LLM 判定)
cases = [
    # 【skip】完全等价
    {
        "id": "skip-1",
        "candidates": [
            {"id":"0","content":"用户是后端工程师主要用 Go","chat_id":"","memory_type":"core","stability_score":0.95,"valid":True}
        ],
        "new_fact": {"content":"用户是后端工程师,主要使用 Go","stability_score":0.90,"memory_type":"core"},
        "current_chat_id": "sess_A",
        "expected_action": "skip",
    },
    # 【skip】价值太低
    {
        "id": "skip-2",
        "candidates": [],
        "new_fact": {"content":"用户今天说了句谢谢","stability_score":0.10,"memory_type":"episodic"},
        "current_chat_id": "sess_A",
        "expected_action": "skip",
    },
    # 【add】独立事实
    {
        "id": "add-1",
        "candidates": [
            {"id":"0","content":"用户偏好中文回答","chat_id":"","memory_type":"core","stability_score":0.90,"valid":True}
        ],
        "new_fact": {"content":"用户在准备一场技术分享","stability_score":0.60,"memory_type":"episodic"},
        "current_chat_id": "sess_A",
        "expected_action": "add",
    },
    # 【update】同主题细化
    {
        "id": "update-1",
        "candidates": [
            {"id":"0","content":"用户喜欢喝咖啡","chat_id":"","memory_type":"core","stability_score":0.75,"valid":True}
        ],
        "new_fact": {"content":"用户喜欢喝不加糖的美式咖啡","stability_score":0.80,"memory_type":"core"},
        "current_chat_id": "sess_A",
        "expected_action": "update",
    },
    # 【delete】明确矛盾
    {
        "id": "delete-1",
        "candidates": [
            {"id":"0","content":"用户常用语言是 Python","chat_id":"","memory_type":"core","stability_score":0.90,"valid":True}
        ],
        "new_fact": {"content":"用户现在主要用 Go,不再用 Python","stability_score":0.90,"memory_type":"core"},
        "current_chat_id": "sess_B",
        "expected_action": "delete",
    },
    # 【promote】跨 3 会话稳定事实(核心场景 — L4 case)
    {
        "id": "promote-1",
        "candidates": [
            {"id":"0","content":"我最近在负责一个订单系统迁移的项目,老 MySQL 拆到分库分表",
             "chat_id":"sess_L1","memory_type":"episodic","stability_score":0.70,"valid":True},
            {"id":"1","content":"我们订单迁移最近一直在解决双写一致性问题",
             "chat_id":"sess_L2","memory_type":"episodic","stability_score":0.65,"valid":True},
        ],
        "new_fact": {"content":"忙订单那个迁移的项目,还没搞完","stability_score":0.72,"memory_type":"episodic"},
        "current_chat_id": "sess_L3",
        "expected_action": "promote",
    },
    # 【promote 反例】3 会话都是进展 → 不 promote,判 add
    {
        "id": "promote-neg-1",
        "candidates": [
            {"id":"0","content":"订单迁移刚开始设计","chat_id":"sess_P1","memory_type":"episodic","stability_score":0.55,"valid":True},
            {"id":"1","content":"订单迁移遇到并发瓶颈需要优化","chat_id":"sess_P2","memory_type":"episodic","stability_score":0.50,"valid":True},
        ],
        "new_fact": {"content":"订单迁移下周一上线","stability_score":0.45,"memory_type":"episodic"},
        "current_chat_id": "sess_P3",
        "expected_action": "add",  # 反例:不应 promote
    },
    # 【add】只有 2 个会话不够 promote 阈值
    {
        "id": "add-2",
        "candidates": [
            {"id":"0","content":"用户在做订单迁移项目","chat_id":"sess_L1","memory_type":"episodic","stability_score":0.72,"valid":True}
        ],
        "new_fact": {"content":"用户负责订单系统迁移","stability_score":0.70,"memory_type":"episodic"},
        "current_chat_id": "sess_A",
        "expected_action": "add",  # 只 2 会话,不 promote
    },
    # 【skip 保护】高 verified core 不能被 episodic 判 delete
    {
        "id": "skip-verified",
        "candidates": [
            {"id":"0","content":"用户是后端工程师","chat_id":"","memory_type":"core","stability_score":0.95,"valid":True}
        ],
        "new_fact": {"content":"用户今天讨论了些前端 CSS 问题","stability_score":0.35,"memory_type":"episodic"},
        "current_chat_id": "sess_A",
        # 期望 add:不该判 delete/update 一条稳定 core
        "expected_action": "add",
    },
]

results = []
for c in cases:
    print(f"\n[case {c['id']}] fact: {c['new_fact']['content'][:50]}", flush=True)
    user_prompt = build_consolidate_user_prompt(
        c["new_fact"], c["candidates"], c["current_chat_id"])
    decision = W._default_llm(CONSOLIDATE_SYSTEM_PROMPT, user_prompt)

    if not decision:
        results.append((False, f"{c['id']}: LLM 返回空"))
        print(f"  → FAIL LLM 空", flush=True)
        continue

    got_action = str(decision.get("action", "")).lower()
    expected = c["expected_action"]
    ok = got_action == expected
    reason = decision.get("reason", "")[:80]
    tgt = decision.get("target_ids", [])
    print(f"  → {'PASS' if ok else 'FAIL'} got={got_action} exp={expected} tgt={tgt}", flush=True)
    print(f"    reason: {reason}", flush=True)
    results.append((ok, f"{c['id']}: got={got_action} exp={expected}"))

passed = sum(1 for ok, _ in results if ok)
total = len(results)
print(f"\n{'='*60}")
print(f"[D3] {passed}/{total} PASS(判据 >= 7/9)")
print(f"{'='*60}")
for ok, msg in results:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {msg}")
if passed >= 7:
    print("\n✓ D3 通过判据,可推进 D4")
else:
    print("\n✗ D3 未达判据,需修 CONSOLIDATE_PROMPT 或 few-shot")
