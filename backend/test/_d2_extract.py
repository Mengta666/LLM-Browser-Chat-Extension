# -*- coding: utf-8 -*-
"""D2 验证:EXTRACT_PROMPT 加 stability_score 后,LLM 判定质量。

10 条真实自然表达 case,人工标 expected stability_score 期望值。
断言 LLM 输出与期望 ±0.15 内即通过。PASS 判据 >= 8/10。
"""
import sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8")
except: pass
sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from agent.memory import writer as W

# 10 条 case:5 稳定属性 / 3 主题稳定含时序 / 2 一次性
cases = [
    # 稳定属性(>= 0.85)
    {"user": "我是后端工程师,主要用 Go", "assistant": "了解",
     "expected_stability": 0.90, "expected_type": "core"},
    {"user": "以后回答我都用中文,简洁点", "assistant": "好的",
     "expected_stability": 0.90, "expected_type": "core"},
    {"user": "我叫张三,在阿里工作", "assistant": "你好张三",
     "expected_stability": 0.95, "expected_type": "core"},
    {"user": "我喜欢代码带详细注释", "assistant": "了解",
     "expected_stability": 0.85, "expected_type": "core"},
    {"user": "我做跨境电商这行,主要东南亚市场", "assistant": "了解",
     "expected_stability": 0.90, "expected_type": "core"},

    # 主题稳定含时序(0.6-0.85)
    {"user": "我最近在做订单系统迁移的项目,下周上线", "assistant": "了解",
     "expected_stability": 0.70, "expected_type": "episodic"},
    {"user": "在准备一场关于 RAG 的技术分享", "assistant": "了解",
     "expected_stability": 0.65, "expected_type": "episodic"},
    {"user": "我在带一个 5 人后端团队", "assistant": "了解",
     "expected_stability": 0.85, "expected_type": "core"},

    # 一次性事件/进展(0.3-0.6)
    {"user": "订单迁移昨天灰度 10%,今天准备扩到 50%", "assistant": "了解",
     "expected_stability": 0.35, "expected_type": "episodic"},
    # 闲聊(应返空 facts)
    {"user": "今天天气不错,帮我看看这段代码", "assistant": "...",
     "expected_stability": None, "expected_type": None},
]

results = []
for i, c in enumerate(cases):
    print(f"\n[case {i+1}] user: {c['user'][:50]}", flush=True)
    facts = W.extract_chat_facts(c["user"], c["assistant"])

    if c["expected_stability"] is None:
        # 闲聊 case,期望空 facts
        ok = len(facts) == 0
        results.append((ok, f"case {i+1}: 闲聊,expected=empty, got {len(facts)} facts"))
        print(f"  → {'PASS' if ok else 'FAIL'} facts={len(facts)}", flush=True)
        continue

    if not facts:
        results.append((False, f"case {i+1}: 期望有 fact,LLM 未抽出"))
        print(f"  → FAIL 未抽出 fact", flush=True)
        continue

    # 取第一条 fact(通常单轮抽出一条)
    f = facts[0]
    print(f"  → content: {f['content'][:60]}", flush=True)
    print(f"    type={f['memory_type']} conf={f['confidence']:.2f} stability={f.get('stability_score',0):.2f}", flush=True)

    stab = f.get("stability_score", 0)
    diff = abs(stab - c["expected_stability"])
    # 判定:stability_score ±0.15 内且 memory_type 一致
    type_ok = f["memory_type"] == c["expected_type"]
    stab_ok = diff <= 0.15
    ok = type_ok and stab_ok
    results.append((ok, f"case {i+1}: type={f['memory_type']}(exp={c['expected_type']}), "
                        f"stab={stab:.2f}(exp={c['expected_stability']:.2f}, diff={diff:.2f})"))
    print(f"  → {'PASS' if ok else 'FAIL'} (type_ok={type_ok}, stab_ok={stab_ok})", flush=True)

# 汇总
passed = sum(1 for ok, _ in results if ok)
total = len(results)
print(f"\n{'='*60}")
print(f"[D2] {passed}/{total} PASS(判据 >= 8/10)")
print(f"{'='*60}")
for ok, msg in results:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {msg}")

if passed >= 8:
    print("\n✓ D2 通过判据,可推进 D3")
else:
    print("\n✗ D2 未达判据,需检查 EXTRACT_PROMPT 或 few-shot")
