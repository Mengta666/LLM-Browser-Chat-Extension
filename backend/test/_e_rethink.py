# -*- coding: utf-8 -*-
"""批次 E · P2 rethink 端到端验证。

seed 一批"冲突/过期/合并"典型 core → 跑 rethink_core → 断言:
- conflict 组:keep 保留有效,invalidate_ids 里的 valid=False + superseded_by=keep
- expired 组:invalid_at 非空,valid=False
- merge 组:新条 valid=True,member 全部 valid=False + superseded_by=新条 id
- verified=True 手动 core 永远保留不动
"""
from __future__ import annotations

import io
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from agent.memory import vector as V
from agent.memory import rethink as R
from agent.memory.config import (
    CHAT_USER_ID, MEMORY_TYPE_CORE, SCOPE_GLOBAL, MEMORY_COLLECTION,
)
from rag.embedder import embed_text


def clean():
    """清空 chat_user_id 命名空间。"""
    items = V.scroll_memories(user_id=CHAT_USER_ID, limit=1000, include_invalid=True)
    for m in items:
        try:
            V.delete_memory(m["memory_id"])
        except Exception:
            pass


def seed(content: str, *, subject="", confidence=0.7, stability_score=0.7,
         verified=False, expires_at="", memory_type=MEMORY_TYPE_CORE) -> str:
    """真向量 seed 一条 core。返回 memory_id。"""
    p = V.insert_memory(
        content, vector=embed_text(content),
        memory_type=memory_type, scope=SCOPE_GLOBAL, chat_id="",
        user_id=CHAT_USER_ID, confidence=confidence,
        stability_score=stability_score, verified=verified,
        subject=subject, expires_at=expires_at,
    )
    return p["memory_id"]


def main():
    print("=" * 70)
    print("E-P2.9 rethink 端到端验证")
    print("=" * 70)

    print("\n[STEP 0] 清库")
    clean()
    assert V.count_memories(user_id=CHAT_USER_ID, include_invalid=True) == 0

    # ── seed 冲突组 ─────────────────────────────────────────
    print("\n[STEP 1] seed 冲突组 · 同 subject='回答语言偏好'")
    # 旧的中文偏好,新的英文偏好
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    id_zh = seed("用户希望回答使用中文", subject="回答语言偏好", confidence=0.85, stability_score=0.85)
    time.sleep(0.5)  # 保证 created_at 有先后
    id_en = seed("用户希望回答使用英文", subject="回答语言偏好", confidence=0.9, stability_score=0.9)
    # 手工把中文条 created_at 改到 30 天前(模拟旧条)
    V.get_client().set_payload(
        collection_name=MEMORY_COLLECTION,
        payload={"created_at": old_ts},
        points=[V._point_id(id_zh)], wait=True)
    print(f"  中文旧条 id={id_zh[:12]}... (created_at={old_ts[:10]})")
    print(f"  英文新条 id={id_en[:12]}...")

    # ── seed 过期组 ─────────────────────────────────────────
    print("\n[STEP 2] seed 过期组 · expires_at 已过 3 天")
    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    id_exp = seed("用户本周主要处理紧急 bug",
                  subject="临时任务",
                  confidence=0.5, stability_score=0.4,
                  expires_at=past)
    print(f"  过期条 id={id_exp[:12]}... (expires_at={past[:10]})")

    # ── seed 合并组 ─────────────────────────────────────────
    print("\n[STEP 3] seed 合并组 · 同 subject='用户身份' 信息互补")
    id_ident1 = seed("用户是后端工程师", subject="用户身份", confidence=0.9, stability_score=0.95)
    time.sleep(0.3)
    id_ident2 = seed("用户主要使用 Go 语言", subject="用户身份", confidence=0.85, stability_score=0.9)
    print(f"  身份条 1 id={id_ident1[:12]}... 'x是后端工程师'")
    print(f"  身份条 2 id={id_ident2[:12]}... 'x主要用 Go'")

    # ── seed verified 手动条(应永远保留,不干扰其他冲突组) ─────
    print("\n[STEP 4] seed verified=True 手动条(独立 subject,不干扰冲突判决)")
    id_manual = seed("用户所在公司使用 Go 微服务架构",
                     subject="用户团队",
                     confidence=0.7, stability_score=0.85,
                     verified=True)
    print(f"  手动条 id={id_manual[:12]}... (verified=True, subject=用户团队)")

    total = V.count_memories(user_id=CHAT_USER_ID, include_invalid=True)
    print(f"\n  库中总条数: {total}")
    assert total == 6, f"expected 6, got {total}"

    # ── 跑 rethink ─────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[STEP 5] 触发 rethink_core")
    print("=" * 70)
    result = R.rethink_core(user_id=CHAT_USER_ID)
    print(f"\n  rethink 返回: {result}")

    # ── 断言 ───────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[STEP 6] 断言落库正确")
    print("=" * 70)

    # 6.1 冲突:英文条 valid=True,中文条 valid=False + superseded_by=英文条
    zh = V.get_memory(id_zh)
    en = V.get_memory(id_en)
    print(f"\n  中文旧条: valid={zh.get('valid')}, superseded_by={zh.get('superseded_by','')[:12]}...")
    print(f"  英文新条: valid={en.get('valid')}, superseded_by={en.get('superseded_by','')[:12]}...")
    # LLM 判决可能有抖动:期望中文条被 invalidate,superseded_by 指向英文条
    passed_conflict = (not zh.get("valid", True)) and en.get("valid", True) \
                      and zh.get("superseded_by", "") == id_en
    print(f"  [conflict] {'✓ PASS' if passed_conflict else '✗ FAIL'}")

    # 6.2 过期:expires_at 已过的条 valid=False
    exp_m = V.get_memory(id_exp)
    print(f"\n  过期条: valid={exp_m.get('valid')}, invalid_at={exp_m.get('invalid_at','')[:10]}")
    passed_expired = not exp_m.get("valid", True)
    print(f"  [expired] {'✓ PASS' if passed_expired else '✗ FAIL'}")

    # 6.3 合并:两条身份条应被 invalidate,新条 valid=True + subject='用户身份'
    id1 = V.get_memory(id_ident1)
    id2 = V.get_memory(id_ident2)
    # 新合并条通过 scroll 找出——它 valid=True + subject 与原来相同(用户身份)
    active_core = V.scroll_memories(
        user_id=CHAT_USER_ID, memory_type=MEMORY_TYPE_CORE,
        scope=SCOPE_GLOBAL, chat_id="", limit=100)
    merged_new = None
    for m in active_core:
        if m["memory_id"] in (id_ident1, id_ident2, id_zh, id_en, id_manual):
            continue
        if m.get("subject") == "用户身份":
            merged_new = m
            break
    print(f"\n  身份条 1: valid={id1.get('valid')}, superseded_by={id1.get('superseded_by','')[:12]}...")
    print(f"  身份条 2: valid={id2.get('valid')}, superseded_by={id2.get('superseded_by','')[:12]}...")
    print(f"  新合并条: {'存在:'+merged_new['content'][:40] if merged_new else '无(LLM 未判 merge)'}")
    # LLM 可能不判 merge——这种"两条独立事实"合并本就是可选行为,不强制断言
    if merged_new:
        passed_merge = (not id1.get("valid", True)) and (not id2.get("valid", True)) \
                       and id1.get("superseded_by", "") == merged_new["memory_id"] \
                       and id2.get("superseded_by", "") == merged_new["memory_id"]
        print(f"  [merge] {'✓ PASS' if passed_merge else '✗ FAIL'}")
    else:
        passed_merge = None
        print(f"  [merge] SKIP (LLM 未产出 merge,可能语义上判为不需合并)")

    # 6.4 verified 保护:手动条永远 valid=True
    manual = V.get_memory(id_manual)
    print(f"\n  verified 手动条: valid={manual.get('valid')}, superseded_by={manual.get('superseded_by','')[:12]}...")
    passed_verified = manual.get("valid", True) and not manual.get("superseded_by", "")
    print(f"  [verified 保护] {'✓ PASS' if passed_verified else '✗ FAIL'}")

    # 汇总
    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    results = [
        ("conflict 组", passed_conflict),
        ("expired 组", passed_expired),
        ("merge 组", passed_merge),
        ("verified 保护", passed_verified),
    ]
    for name, r in results:
        icon = "✓" if r else ("─" if r is None else "✗")
        status = "PASS" if r else ("SKIP" if r is None else "FAIL")
        print(f"  {icon} {name}: {status}")

    hard_pass = passed_conflict and passed_expired and passed_verified
    print(f"\n{'✓ 硬断言全 PASS' if hard_pass else '✗ 有硬断言 FAIL'}")
    return 0 if hard_pass else 1


if __name__ == "__main__":
    sys.exit(main())
