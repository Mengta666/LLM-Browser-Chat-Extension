# -*- coding: utf-8 -*-
"""阈值离线标定(#5)。依赖 B1 retrieval.jsonl 的正/负例分布。

产出 config/thresholds_<embed_model>.json,替代硬编码魔数:
- RECALL_MIN_COSINE:episodic 召回主闸门(负例分布 P95)
- RECALL_GAP_REF:三因子 gap-gated 稀释门(两分布均值差)
- PROMOTE_SIM_COSINE:晋升复现检测(晋升语义相似正例 P50-P90)

用法:
    ./.venv/Scripts/python.exe -B test/eval/calibrate_thresholds.py
"""
import sys, json, os
from pathlib import Path
from statistics import mean, median

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).absolute().parents[2]))
sys.path.insert(0, str(Path(__file__).absolute().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).absolute().parents[2] / "config" / ".env")

import harness as H
from agent.memory import vector as V
from agent.memory.config import (
    CHAT_USER_ID, MEMORY_TYPE_EPISODIC, MEMORY_TYPE_CORE,
    INSTRUCT_CHAT, MEMORY_COLLECTION,
)
from rag.embedder import embed_query, embed_text


def p95(xs: list[float]) -> float:
    """95 分位。"""
    if not xs: return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.95))]


def p50(xs: list[float]) -> float:
    return median(xs) if xs else 0.0


def sample_retrieval_pairs():
    """从 retrieval.jsonl 采样正/负例 cosine。返回 (pos_cos, neg_cos)。"""
    cases = H.load_cases("retrieval")
    pos_cos, neg_cos = [], []

    for case in cases:
        H.clean()
        # seed 用真向量(检索评测需要,非零向量)
        seed_id_to_tags: dict[str, list[str]] = {}
        for s in case.get("seed_memories", []):
            mid = H.seed_one(
                content=s["content"],
                memory_type=s.get("memory_type", "core"),
                chat_id=s.get("chat_id", ""),
                confidence=s.get("confidence", 0.8),
                use_zero_vector=False,       # 标定必须用真 embedding
            )
            seed_id_to_tags[mid] = s.get("tags", [])

        for q in case.get("queries", []):
            try:
                qv = embed_query(q["text"], INSTRUCT_CHAT)
            except Exception:
                continue
            # 拉所有 seed 的余弦分数(限本 case 的 seed,不跨 case)
            all_scores = V.dense_scores(
                qv, top_k=50, user_id=CHAT_USER_ID,
                memory_type=None,     # 不限类型(retrieval 里 core+episodic 都有)
            )
            # 按 tag 分类
            for mid, cos in all_scores.items():
                tags = seed_id_to_tags.get(mid, [])
                if "gold" in tags:
                    pos_cos.append(cos)
                elif "distractor" in tags or "cross_session_distractor" in tags:
                    neg_cos.append(cos)

    return pos_cos, neg_cos


def sample_promotion_pairs():
    """从 promotion.jsonl 里 stable_pos + progress_neg 采晋升语义相似度。
    - stable_pos:sibling 之间 cosine → 正例
    - progress_neg:seed 之间 cosine → 负例(进展不算稳定,不该被误判为复现)
    """
    cases = H.load_cases("promotion")
    stable_pos, progress_neg = [], []

    for case in cases:
        subtype = case.get("subtype", "")
        if subtype not in ("stable_pos", "progress_neg"):
            continue
        H.clean()
        # seed 用真向量
        contents = []
        for s in case.get("seed_memories", []):
            H.seed_one(
                content=s["content"],
                memory_type=s.get("memory_type", "episodic"),
                chat_id=s.get("chat_id", ""),
                confidence=s.get("confidence", 0.6),
                use_zero_vector=False,
            )
            contents.append(s["content"])
        # 两两算 cosine
        for i in range(len(contents)):
            try:
                v_i = embed_text(contents[i])
                scores = V.dense_scores(v_i, top_k=50, user_id=CHAT_USER_ID,
                                        memory_type=MEMORY_TYPE_EPISODIC,
                                        chat_id=None)
                # 排除自己(cosine≈1)
                for mid, cos in scores.items():
                    if cos > 0.99: continue
                    if subtype == "stable_pos":
                        stable_pos.append(cos)
                    else:
                        progress_neg.append(cos)
            except Exception:
                continue

    return stable_pos, progress_neg


def main():
    embed_model = os.environ.get("EMBEDDING_MODEL", "unknown")
    print(f"[calibrate] 使用 embedding: {embed_model}")
    print()

    print("=" * 60)
    print("采样检索正/负例(retrieval.jsonl)...")
    print("=" * 60)
    H.reset_collection()
    pos, neg = sample_retrieval_pairs()
    if pos and neg:
        print(f"检索正例 n={len(pos)}: mean={mean(pos):.3f}, p50={p50(pos):.3f}")
        print(f"检索负例 n={len(neg)}: mean={mean(neg):.3f}, p95={p95(neg):.3f}")
        recall_min = round(p95(neg), 2)
        gap_ref = round(max(0.0, mean(pos) - mean(neg)), 2)
    else:
        print(f"数据不足:pos={len(pos)}, neg={len(neg)},跳过检索标定")
        recall_min, gap_ref = None, None
    print()

    print("=" * 60)
    print("采样晋升正/负例(promotion.jsonl)...")
    print("=" * 60)
    H.reset_collection()
    stab_pos, prog_neg = sample_promotion_pairs()
    if stab_pos:
        print(f"晋升稳定事实 n={len(stab_pos)}: mean={mean(stab_pos):.3f}, p50={p50(stab_pos):.3f}")
        # PROMOTE_SIM_COSINE 取 stable_pos P50 - 留 buffer,避免过严
        # 若有 progress_neg 数据,取"稳定 P50 和 进展 mean 的中点"更稳
        if prog_neg:
            print(f"晋升进展负例 n={len(prog_neg)}: mean={mean(prog_neg):.3f}")
            promote_sim = round((p50(stab_pos) + mean(prog_neg)) / 2, 2)
        else:
            promote_sim = round(p50(stab_pos) * 0.9, 2)   # 90% of P50 留 buffer
    else:
        print(f"数据不足:stab_pos={len(stab_pos)}, prog_neg={len(prog_neg)},跳过晋升标定")
        promote_sim = None
    print()

    # 写出配置
    result = {"embed_model": embed_model}
    if recall_min is not None:
        result["RECALL_MIN_COSINE"] = recall_min
    if gap_ref is not None:
        result["RECALL_GAP_REF"] = gap_ref
    if promote_sim is not None:
        result["PROMOTE_SIM_COSINE"] = promote_sim

    safe_name = embed_model.replace("/", "_").replace(":", "_")
    out = Path(__file__).absolute().parents[2] / "config" / f"thresholds_{safe_name}.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"→ 标定结果写入 {out.relative_to(Path.cwd())}")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
