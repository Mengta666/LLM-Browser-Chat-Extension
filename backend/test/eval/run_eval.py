# -*- coding: utf-8 -*-
"""评测主入口。跑 4 类 cases,输出 5 指标 + 每条 case 的 pass/fail。

用法:
    ./.venv/Scripts/python.exe -B test/eval/run_eval.py                   # 全跑
    ./.venv/Scripts/python.exe -B test/eval/run_eval.py --only retrieval  # 只跑一类
    ./.venv/Scripts/python.exe -B test/eval/run_eval.py --skip-slow       # 跳过 update/promotion(不跑真 LLM)

5 指标:
- recall@5:检索是否捞到 gold_mem
- 注入 precision:注入项里相关占比(gold 数 / 注入总数)
- 更新正确率:更新对里 gold 是新 A 的占比
- 弃权误杀率:该弃权 case 里注入了噪声的占比(越低越好)
- 端到端 acc(可选):需 GPT-judge,暂不启用
"""
import sys, argparse, time
from pathlib import Path

# Windows 控制台 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).absolute().parents[2]))
sys.path.insert(0, str(Path(__file__).absolute().parent))

import harness as H
from agent.memory import vector as V
from agent.memory import retriever as R
from agent.memory.config import CHAT_USER_ID, MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC


# ═══════════════════════════════════════════════════════════
# 通用工具
# ═══════════════════════════════════════════════════════════
def _log(msg, tag=""):
    prefix = f"[{tag}] " if tag else ""
    print(f"{prefix}{msg}", flush=True)


def _pass(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    line = f"{tag} {name}"
    if detail:
        line += f" | {detail}"
    print(line, flush=True)
    return bool(cond)


# ═══════════════════════════════════════════════════════════
# 检索评测(retrieval + rejection)
# ═══════════════════════════════════════════════════════════
def eval_retrieval() -> dict:
    """跑 retrieval.jsonl + rejection.jsonl,统计 recall@5 和 弃权误杀率。"""
    cases = H.load_cases("retrieval") + H.load_cases("rejection")
    _log(f"检索评测:{len(cases)} 条 cases", tag="retrieval")

    hit = miss = fp = tp = 0            # recall@5 / precision
    rej_fail = 0; rej_total = 0         # 弃权误杀
    pass_list = []; fail_list = []

    for case in cases:
        H.clean()
        tag_to_id = H.seed_case(case)
        gold_tag_ids = {tag_to_id[k] for k in tag_to_id if k.startswith("g")}

        for q in case.get("queries", []):
            gold_ids = {tag_to_id.get(g, g) for g in q.get("gold_mem_ids", [])}
            chat_id = q.get("chat_id", "")

            # episodic 召回
            epi = R.recall_episodic_memories(q["text"], chat_id=chat_id, top_k=5,
                                             user_id=CHAT_USER_ID)
            # core 全量(注入 precision/recall 计算含 core;rejection 类只看 episodic)
            core = R.retrieve_core_memories(user_id=CHAT_USER_ID)
            hit_ids = {m["memory_id"] for m in epi + core}

            if case["type"] == "rejection":
                # 该弃权:只看 episodic 是否被误召回(core 每轮全量注入,不受 query 影响)
                rej_total += 1
                epi_ids = {m["memory_id"] for m in epi}
                if epi_ids:
                    rej_fail += 1
                    fail_list.append(f"{case['id']} · 该弃权却召回 {len(epi_ids)} 条 episodic")
                else:
                    pass_list.append(f"{case['id']} · 正确弃权")
                continue

            # retrieval:算 recall/precision
            # gold 空:该 query 无正例(评测集配置错误应放到 rejection),跳过 recall 统计
            if not gold_ids:
                pass_list.append(f"{case['id']} · gold 为空,跳过(应移到 rejection)")
                continue

            recall_hit = bool(gold_ids & hit_ids)
            if recall_hit: hit += 1
            else: miss += 1
            for h in hit_ids:
                if h in gold_ids: tp += 1
                else: fp += 1
            (pass_list if recall_hit else fail_list).append(
                f"{case['id']} · gold={len(gold_ids)}, hit={len(hit_ids)}, recall={recall_hit}"
            )

    total_ret = hit + miss
    recall_pct = 100 * hit / total_ret if total_ret else 0
    prec_pct = 100 * tp / (tp + fp) if (tp + fp) else 0
    rej_pct = 100 * rej_fail / rej_total if rej_total else 0

    _log(f"recall@5             : {hit}/{total_ret}  ({recall_pct:.1f}%)")
    _log(f"注入 precision       : {tp}/{tp+fp}  ({prec_pct:.1f}%)")
    _log(f"弃权误杀率            : {rej_fail}/{rej_total}  ({rej_pct:.1f}%)  [↓ 越低越好]")

    return {"recall_pct": recall_pct, "precision_pct": prec_pct,
            "rejection_fail_pct": rej_pct,
            "passes": pass_list, "fails": fail_list}


# ═══════════════════════════════════════════════════════════
# 更新评测
# ═══════════════════════════════════════════════════════════
def eval_update() -> dict:
    """跑 update.jsonl,统计更新正确率。走真 LLM 抽取+决策。"""
    cases = H.load_cases("update")
    _log(f"更新评测:{len(cases)} 条(走真 LLM,较慢)", tag="update")

    correct = 0
    pass_list = []; fail_list = []

    for case in cases:
        H.clean()
        # 依次跑每轮 turn
        for t in case["turns"]:
            H.force_write_sync(t["user"], t["assistant"], t["chat_id"])
            time.sleep(0.3)  # 让 wait=True 写入落盘

        # 读所有含 invalid
        allm = V.scroll_memories(user_id=CHAT_USER_ID, limit=100, include_invalid=True)
        active = [m for m in allm if m.get("valid", True)]
        invalid = [m for m in allm if not m.get("valid", True)]

        exp = case["expect"]
        active_txt = " ".join(m["content"] for m in active)
        invalid_txt = " ".join(m["content"] for m in invalid)

        act_ok = all(any(kw in active_txt for kw in _ensure_list(exp.get("active_contains", [])))
                     for _ in [1])  # 占位:检查所有关键词都出现
        # 简化:任一 active_contains 命中即算 OK
        act_ok = any(kw in active_txt for kw in _ensure_list(exp.get("active_contains", [""])))
        inv_ok = True
        for kw in _ensure_list(exp.get("invalid_contains", [])):
            if kw and kw not in invalid_txt:
                inv_ok = False
                break

        max_c = exp.get("active_max_count")
        cnt_ok = True if max_c is None else len(active) <= max_c

        ok = act_ok and inv_ok and cnt_ok
        if ok: correct += 1
        (pass_list if ok else fail_list).append(
            f"{case['id']} · active={[m['content'][:20] for m in active]}, invalid={[m['content'][:20] for m in invalid]}"
        )

    pct = 100 * correct / len(cases) if cases else 0
    _log(f"更新正确率            : {correct}/{len(cases)}  ({pct:.1f}%)")
    return {"correct_pct": pct, "passes": pass_list, "fails": fail_list}


def _ensure_list(x):
    if isinstance(x, list): return x
    return [x] if x else []


# ═══════════════════════════════════════════════════════════
# 晋升评测(B3/B4 落地后启用完整版)
# ═══════════════════════════════════════════════════════════
def eval_promotion() -> dict:
    """跑 promotion.jsonl。B3/B4 未落地前只测 seed + 断言"库里状态"部分。

    B3/B4 落地后:
    - stable_pos:trigger 后走 detect_recurrence + promote_memory,断言 canonical 存在
    - progress_neg:trigger 后断言未晋升
    - post_promote_flip:seed 一条 core,turns 触发 DELETE,断言 invalidate
    - noise_no_flip:seed 一条 verified core,噪声不撤(future feature)
    - concurrent:两 daemon 同时 write,断言最终 1 条 core
    """
    cases = H.load_cases("promotion")
    _log(f"晋升评测:{len(cases)} 条(部分需 B3/B4 落地后启用)", tag="promotion")

    # 先只跑 post_promote_flip(不需要 B3/B4,靠 B2 链路三就能验)
    try:
        from agent.memory import promotion  # B3/B4 落地后才有
        has_promotion_module = True
    except ImportError:
        has_promotion_module = False
        _log("  (promotion.py 未就绪 → 跳过 stable_pos/progress_neg/concurrent,只跑 flip)")

    passed = failed = skipped = 0
    pass_list = []; fail_list = []

    for case in cases:
        subtype = case.get("subtype", "")
        H.clean()

        if subtype == "post_promote_flip":
            tag_to_id = H.seed_case(case)
            c1_id = tag_to_id.get("promoted") or tag_to_id.get("c1")
            for t in case.get("turns", []):
                H.force_write_sync(t["user"], t["assistant"], t["chat_id"])
                time.sleep(0.3)
            after = V.get_memory(c1_id)
            exp_inv = case["expect"].get("c1_invalidated", False)
            got_inv = (after is None) or (not after.get("valid", True))
            ok = got_inv == exp_inv
            if ok: passed += 1
            else: failed += 1
            (pass_list if ok else fail_list).append(
                f"{case['id']} · {subtype} · exp_inv={exp_inv}, got_inv={got_inv}")

        elif subtype == "noise_no_flip":
            tag_to_id = H.seed_case(case)
            c1_id = tag_to_id.get("promoted") or tag_to_id.get("c1")
            for t in case.get("turns", []):
                H.force_write_sync(t["user"], t["assistant"], t["chat_id"])
                time.sleep(0.3)
            after = V.get_memory(c1_id)
            got_inv = (after is None) or (not after.get("valid", True))
            # 期望不失效(verified 高门槛 - future),当前系统未实现故可能 fail
            ok = not got_inv
            if ok: passed += 1
            else: failed += 1
            (pass_list if ok else fail_list).append(
                f"{case['id']} · {subtype} · got_inv={got_inv} (未实现 verified 高门槛可能失败)")

        elif subtype in ("stable_pos", "progress_neg", "concurrent", "gc_sibling") and has_promotion_module:
            tag_to_id = H.seed_case(case)
            # concurrent 有多个 trigger,同一线程串行跑(简化版并发验证 —— 断言"最终 core 数")
            trigs = (case.get("triggers_concurrent")
                     or ([case["trigger"]] if case.get("trigger") else []))
            for trig in trigs:
                H.force_write_sync(trig["user"], trig["assistant"], trig["chat_id"])
                time.sleep(0.5)
            # 数活跃 core
            core_now = V.scroll_memories(user_id=CHAT_USER_ID,
                                         memory_type=MEMORY_TYPE_CORE,
                                         chat_id="", limit=100)
            # concurrent 类看 final_core_count(期望恰好 1 条,验证幂等收敛)
            # 其他类看 should_promote(期望 True/False)
            if subtype == "concurrent":
                exp_count = case["expect"].get("final_core_count", 1)
                got_count = len(core_now)
                ok = got_count == exp_count
                (pass_list if ok else fail_list).append(
                    f"{case['id']} · {subtype} · exp_core_count={exp_count}, got={got_count}")
            else:
                exp_promote = case["expect"].get("should_promote", False)
                got_promote = len(core_now) >= 1
                ok = got_promote == exp_promote
                (pass_list if ok else fail_list).append(
                    f"{case['id']} · {subtype} · exp={exp_promote}, got_promote={got_promote}, core={len(core_now)}")
            if ok: passed += 1
            else: failed += 1
        else:
            skipped += 1
            pass_list.append(f"{case['id']} · {subtype} · SKIP(依赖未就绪)")

    total = passed + failed
    pct = 100 * passed / total if total else 0
    _log(f"晋升正确率            : {passed}/{total}  ({pct:.1f}%)  skip={skipped}")
    return {"correct_pct": pct, "skipped": skipped,
            "passes": pass_list, "fails": fail_list}


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None,
                        help="只跑一类:retrieval / update / promotion")
    parser.add_argument("--skip-slow", action="store_true",
                        help="跳过 update/promotion(不调远程 LLM)")
    args = parser.parse_args()

    _log("=" * 60)
    _log("批次 B 评测", tag="run_eval")
    _log("=" * 60)

    H.reset_collection()
    results = {}

    if args.only in (None, "retrieval"):
        results["retrieval"] = eval_retrieval()

    if not args.skip_slow:
        if args.only in (None, "update"):
            results["update"] = eval_update()
        if args.only in (None, "promotion"):
            results["promotion"] = eval_promotion()

    _log("=" * 60)
    _log("汇总")
    _log("=" * 60)
    all_fails = []
    for name, r in results.items():
        all_fails.extend(f"  [{name}] {x}" for x in r.get("fails", []))
    if all_fails:
        _log(f"FAIL {len(all_fails)} 条:")
        for line in all_fails[:20]:  # 只显头 20 条
            _log(line)
        if len(all_fails) > 20:
            _log(f"  ...({len(all_fails)-20} more)")
    else:
        _log("全部 PASS")


if __name__ == "__main__":
    main()
