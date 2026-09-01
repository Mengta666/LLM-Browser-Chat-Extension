# -*- coding: utf-8 -*-
"""测同一稳定事实的多种自然表达之间的余弦分布,以及和干扰项的分布对比。

目的:回答"PROMOTE_SIM_COSINE 该定多少"这个问题——用真实数据说话。
"""
import sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8")
except: pass
sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from rag.embedder import embed_text

# ═══════════════════════════════════════════════════════════
# 三组样本:
#   A. 同一稳定事实的多种自然表达(应该都被判为"同事实")
#   B. 同主题的进展(不该判为"同事实",避免误升)
#   C. 完全不同主题(基线负例)
# ═══════════════════════════════════════════════════════════

# A 组:"用户在做订单迁移项目"—— 5 种真实自然表达
group_A_stable = [
    "我最近在负责一个订单系统迁移的项目,老 MySQL 拆到分库分表",
    "我们订单迁移最近一直在解决双写一致性问题",
    "忙订单那个迁移的项目,还没搞完",
    "手头在做订单系统的迁移",
    "现在的主项目是订单迁移",
]

# B 组:同主题订单迁移的"进展"(不该复现)
group_B_progress = [
    "订单迁移这周做压测",
    "订单迁移昨天灰度上线了 10%",
    "订单迁移的双写一致性 bug 修好了",
]

# C 组:完全不同主题(基线负例)
group_C_unrelated = [
    "用户是后端工程师主要用 Go",
    "用户偏好回答用中文简洁",
    "上周团建吃了 A 餐厅",
]

def cos(v1, v2):
    """cosine similarity."""
    import math
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def show_matrix(name, texts):
    print(f"\n=== {name} · 两两余弦矩阵 ===")
    vecs = [embed_text(t) for t in texts]
    n = len(texts)
    print(f"{'':4s} " + " ".join(f"[{i}]  " for i in range(n)))
    for i in range(n):
        row = f"[{i}] "
        for j in range(n):
            c = cos(vecs[i], vecs[j])
            row += f"{c:.3f} "
        print(row + f"  {texts[i][:40]}")
    return vecs


def pair_stats(name, group_vecs, group_texts):
    """算所有两两 pair 的分布,排除对角。"""
    import statistics
    pairs = []
    n = len(group_vecs)
    for i in range(n):
        for j in range(i+1, n):
            pairs.append(cos(group_vecs[i], group_vecs[j]))
    if not pairs:
        return
    print(f"\n{name}:")
    print(f"  pair 数:{len(pairs)}")
    print(f"  min={min(pairs):.4f}  max={max(pairs):.4f}")
    print(f"  mean={statistics.mean(pairs):.4f}  median={statistics.median(pairs):.4f}")
    return pairs


def cross_stats(name, vecs_A, vecs_B, texts_A, texts_B):
    """A 组每个 vs B 组每个的余弦。"""
    import statistics
    pairs = []
    for va in vecs_A:
        for vb in vecs_B:
            pairs.append(cos(va, vb))
    print(f"\n{name}(A × B 交叉):")
    print(f"  pair 数:{len(pairs)}")
    print(f"  min={min(pairs):.4f}  max={max(pairs):.4f}")
    print(f"  mean={statistics.mean(pairs):.4f}  median={statistics.median(pairs):.4f}")
    return pairs


# 跑
print("=" * 70)
print("A 组:同一稳定事实(订单迁移)的多种自然表达")
print("=" * 70)
for i, t in enumerate(group_A_stable):
    print(f"  [{i}] {t}")
vecs_A = show_matrix("A 组内", group_A_stable)

print("\n" + "=" * 70)
print("B 组:同主题订单迁移的进展")
print("=" * 70)
for i, t in enumerate(group_B_progress):
    print(f"  [{i}] {t}")
vecs_B = show_matrix("B 组内", group_B_progress)

print("\n" + "=" * 70)
print("C 组:完全不同主题")
print("=" * 70)
for i, t in enumerate(group_C_unrelated):
    print(f"  [{i}] {t}")
vecs_C = show_matrix("C 组内", group_C_unrelated)

# 统计
print("\n\n" + "=" * 70)
print("统计汇总")
print("=" * 70)
p_A = pair_stats("[A 组内] 稳定事实两两(正例 · 应该都过阈值)", vecs_A, group_A_stable)
p_B = pair_stats("[B 组内] 进展两两(边界 · 严格应该不过)", vecs_B, group_B_progress)
p_C = pair_stats("[C 组内] 无关两两(负例 · 应该都不过)", vecs_C, group_C_unrelated)

p_AB = cross_stats("[A 稳定 × B 进展] 交叉(关键·B 应该被挡但同主题相似度高)", vecs_A, vecs_B, group_A_stable, group_B_progress)
p_AC = cross_stats("[A 稳定 × C 无关] 交叉(应该都低)", vecs_A, vecs_C, group_A_stable, group_C_unrelated)


# 决策分析
print("\n\n" + "=" * 70)
print("阈值分析")
print("=" * 70)
if p_A and p_AB:
    A_min = min(p_A)
    A_p20 = sorted(p_A)[max(0, int(len(p_A) * 0.2) - 1)]
    AB_max = max(p_AB)
    AB_p80 = sorted(p_AB)[min(len(p_AB)-1, int(len(p_AB) * 0.8))]
    print(f"\nA 组内(稳定事实间)最低相似度:{A_min:.4f}")
    print(f"A 组内 P20(80% 的稳定事实对高于此):{A_p20:.4f}")
    print(f"A × B 最高交叉:{AB_max:.4f}")
    print(f"A × B P80(80% 的进展-稳定交叉低于此):{AB_p80:.4f}")

    # 理想阈值:能把 A 组内的都放行、把 A×B 的都挡下
    if A_min > AB_max:
        print(f"\n✓ 完美分离:A 组最低 {A_min:.4f} > A×B 最高 {AB_max:.4f}")
        print(f"  阈值可设为 [{AB_max:.2f}, {A_min:.2f}] 区间任一值")
        print(f"  推荐:{(A_min + AB_max) / 2:.2f}")
    else:
        overlap = AB_max - A_min
        print(f"\n✗ 分布重叠:A 组最低 {A_min:.4f} < A×B 最高 {AB_max:.4f} (重叠 {overlap:.4f})")
        print(f"  纯余弦无法完全分离,LLM stability judge 是必要的第二层")
        print(f"  若追求 recall:阈值取 A_p20={A_p20:.2f},召回 80% 稳定事实,可能有少量 B 进入候选")
        print(f"  若追求 precision:阈值取 AB_p80={AB_p80:.2f},挡下 80% 进展,可能漏 20% 稳定")
