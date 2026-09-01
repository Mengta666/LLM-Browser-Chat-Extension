# -*- coding: utf-8 -*-
"""跑一次完整的 chat 记忆四条执行链路,输出每一步的真实数据。

不走 HTTP,直接调 service/writer/promotion 内部函数,便于捕获中间态。
"""
import sys, io, json, time
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8")
except: pass
sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from agent.memory import vector as V
from agent.memory import service as S
from agent.memory import writer as W
from agent.memory import retriever as R
from agent.memory import promotion
from agent.memory import history as H
from agent.memory.config import (
    CHAT_USER_ID, MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC,
    RECALL_MIN_COSINE, RECALL_REL_RATIO, RECALL_GAP_REF,
    PROMOTE_THRESHOLD, PROMOTE_SIM_COSINE, PROMOTE_CONFIDENCE,
    CORE_CHAR_BUDGET, MEMORY_COLLECTION,
)
from rag.embedder import embed_text, embed_query


def hr(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70, flush=True)


def kv(label, value):
    print(f"  {label}: {value}", flush=True)


def show_mem(m, indent="    "):
    if not m: return
    print(f"{indent}[{m.get('memory_type',''):8s}] chat={m.get('chat_id',''):8s} "
          f"conf={m.get('confidence',0):.2f} reinforce={m.get('reinforce_count',0)} "
          f"valid={m.get('valid',True)} promoted_from={m.get('promoted_from','')!r}", flush=True)
    print(f"{indent}  content: {m.get('content','')[:80]}", flush=True)


# ═══════════════════════════════════════════════════════════
# 前置:清库 + seed 一批背景数据(模拟已有 chat 历史)
# ═══════════════════════════════════════════════════════════
hr("STEP 0: 清库 + 前置 seed")

V.get_client().delete_collection(collection_name=MEMORY_COLLECTION)
V._collection_ready = False
V.ensure_collection()
print("collection 已重建", flush=True)

# 用真 embedding 种入 2 条 core + sess_A 3 条 episodic
# 模拟之前的对话已经产生这些记忆
_seed_specs = [
    # core
    ("用户是后端工程师,主要使用 Go", MEMORY_TYPE_CORE, "", 0.9, 2),
    ("用户希望回答用中文,风格简洁", MEMORY_TYPE_CORE, "", 0.8, 0),
    # sess_A 的 episodic(与 L1 query 相关)
    ("用户在做订单迁移项目,计划下周上线", MEMORY_TYPE_EPISODIC, "sess_A", 0.6, 0),
    ("订单迁移遇到并发瓶颈,团队讨论用队列削峰", MEMORY_TYPE_EPISODIC, "sess_A", 0.5, 0),
    ("用户上周团建吃了 A 餐厅", MEMORY_TYPE_EPISODIC, "sess_A", 0.3, 0),
]

seed_ids = {}
for content, mtype, cid, conf, rc in _seed_specs:
    vec = embed_text(content)
    p = V.insert_memory(content, vector=vec, memory_type=mtype,
                        user_id=CHAT_USER_ID, chat_id=cid,
                        confidence=conf, reinforce_count=rc)
    seed_ids[content[:15]] = p["memory_id"]
    print(f"  seed [{mtype:8s}] chat={cid!r:12s} conf={conf} → {p['memory_id']}", flush=True)


# ═══════════════════════════════════════════════════════════
# L1 · 读路径:模拟用户在 sess_A 问"订单迁移那个项目现在怎么样了"
# ═══════════════════════════════════════════════════════════
hr("L1 · 读路径")

query = "订单迁移那个项目现在怎么样了"
chat_id_l1 = "sess_A"
kv("用户 query", query)
kv("chat_id", chat_id_l1)

# L1.3 core 常驻
print("\n-- L1.3 core 常驻读取 --", flush=True)
core_items = R.retrieve_core_memories(user_id=CHAT_USER_ID)
kv("scroll core 条数", len(core_items))
for m in core_items:
    show_mem(m)

core_block = R.build_core_block(core_items)
print("\n  core_block (注入):")
for line in core_block.split("\n"):
    print(f"    {line}", flush=True)
kv("core_block 字符数", len(core_block))

# L1.4 episodic 双闸门 + 三因子
print("\n-- L1.4 episodic 召回(内部数据) --", flush=True)
from agent.memory.config import INSTRUCT_CHAT
query_vec = embed_query(query, INSTRUCT_CHAT)
kv("query_vec 维度", len(query_vec))

# 4a. hybrid search(拿 RRF)
candidates = V.search_memories(
    query_vec, query_text=query, top_k=10,
    user_id=CHAT_USER_ID, memory_type=MEMORY_TYPE_EPISODIC,
    chat_id=chat_id_l1,
)
print(f"\n  hybrid candidates (RRF 分):", flush=True)
for c in candidates:
    print(f"    RRF={c.get('score',0):.4f}  {c.get('content','')[:60]}", flush=True)

# 4b. dense_scores(拿真余弦)
cos_map = V.dense_scores(
    query_vec, top_k=10,
    user_id=CHAT_USER_ID, memory_type=MEMORY_TYPE_EPISODIC,
    chat_id=chat_id_l1)
print(f"\n  cos_map (纯余弦):", flush=True)
for mid, cos in sorted(cos_map.items(), key=lambda x: -x[1]):
    m = V.get_memory(mid)
    print(f"    cos={cos:.4f}  {m.get('content','')[:60]}", flush=True)

# 4c. 双闸门
print(f"\n  阈值:RECALL_MIN_COSINE={RECALL_MIN_COSINE}, RECALL_REL_RATIO={RECALL_REL_RATIO}", flush=True)
top_rrf = max((c.get("score",0) for c in candidates), default=0)
rrf_floor = top_rrf * RECALL_REL_RATIO
print(f"  top_rrf={top_rrf:.4f}, rrf_floor={rrf_floor:.4f}")

passed = []
for c in candidates:
    mid = c.get("memory_id","")
    cosine = cos_map.get(mid, 0.0)
    rrf = c.get("score", 0.0)
    ok = cosine >= RECALL_MIN_COSINE and rrf >= rrf_floor
    mark = "✓过" if ok else "✗挡"
    print(f"    {mark}  cos={cosine:.3f} rrf={rrf:.4f}  {c.get('content','')[:50]}", flush=True)
    if ok:
        c2 = dict(c); c2["cosine"] = cosine
        passed.append(c2)

# 4d. 三因子重排
if passed:
    print(f"\n  过闸门 {len(passed)} 条,做 gap-gated 三因子重排:", flush=True)
    result = R.recall_episodic_memories(query, chat_id=chat_id_l1,
                                        user_id=CHAT_USER_ID, top_k=5)
    for m in result:
        print(f"    _score3={m.get('_score3',0):.4f} cos={m.get('cosine',0):.3f} "
              f"rec={m.get('_recency',0):.3f} dilution={m.get('_dilution',0):.3f}", flush=True)
        print(f"      {m.get('content','')[:60]}")
else:
    print("  全被挡下,episodic 注入空", flush=True)

# ═══════════════════════════════════════════════════════════
# L2 · 写路径:用户第 3 轮说"以后回答请用中文,简洁点"
# ═══════════════════════════════════════════════════════════
hr("L2 · 写路径")

user_msg = "以后回答请用中文,简洁点"
assistant_msg = "好的,我记住了"
chat_id_l2 = "sess_A"

kv("user_msg", user_msg)
kv("assistant_msg", assistant_msg)
kv("chat_id", chat_id_l2)

# 直接调 write_chat_memory(绕 daemon 同步跑,便于观察)
print("\n-- L2.2 抽取 + L2.3 决策 + L2.4 挂钩 --", flush=True)
result = S.write_chat_memory(user_msg, assistant_msg,
                             history_summary="", chat_id=chat_id_l2)
print("\n  返回结果:", flush=True)
kv("facts 数", len(result.get("facts",[])))
for f in result.get("facts",[]):
    print(f"    [{f['memory_type']:8s}] conf={f['confidence']:.2f}  {f['content'][:60]}", flush=True)
kv("applied", result.get("applied",[]))
kv("skipped_hash", result.get("skipped_hash",0))

# 看落库状态
print("\n-- L2.6 库状态(L2 后) --", flush=True)
all_after_l2 = V.scroll_memories(user_id=CHAT_USER_ID, limit=100, include_invalid=True)
kv("库总条数(含 invalid)", len(all_after_l2))
for m in all_after_l2:
    show_mem(m, indent="  ")

# 看 memory_history
audit = 0
try:
    audit = H.count_events()
except: pass
kv("SQLite audit 事件总数", audit)


# ═══════════════════════════════════════════════════════════
# L3 · CRUD 路径:手动添加 "我用 pytest 做测试"
# ═══════════════════════════════════════════════════════════
hr("L3 · CRUD 路径")

l3_content = "我用 pytest 做测试"
kv("用户输入", l3_content)
kv("memory_type", "core")

# 模拟 api/memory.py:create_memory 逻辑
print("\n-- L3.2 校验 + 落库 --", flush=True)
# 校验(略,跳白名单)
l3_payload = V.insert_memory(
    l3_content, vector=embed_text(l3_content),
    memory_type=MEMORY_TYPE_CORE, scope="global", domain="",
    user_id=CHAT_USER_ID, confidence=0.7, verified=True,
)
print("  手动条落库:", flush=True)
show_mem(l3_payload, indent="  ")

# _view 展示
_view_fields = ["memory_id","content","memory_type","created_at","updated_at","valid","invalid_at"]
view = {k: l3_payload.get(k) for k in _view_fields}
print("\n  _view 返回给前端:", flush=True)
print(f"    {json.dumps(view, ensure_ascii=False)}"[:200], flush=True)

# 影响 L1 · 再次跑 core 读取,看排序位置
print("\n-- L3.3 影响后续 L1(重新 retrieve_core_memories) --", flush=True)
core_after_l3 = R.retrieve_core_memories(user_id=CHAT_USER_ID)
kv("core 条数", len(core_after_l3))
for i, m in enumerate(core_after_l3):
    manual = "手动" if m.get("verified") else "自动"
    print(f"    #{i+1} [{manual}] conf={m['confidence']:.2f}  {m['content'][:50]}", flush=True)


# ═══════════════════════════════════════════════════════════
# L4 · 晋升路径:三个 chat 各说一条相近的稳定事实
# ═══════════════════════════════════════════════════════════
hr("L4 · 晋升路径(3 个 chat 相近表达)")

# 清一下 L2 之前的干扰(保留 core,清 episodic)
epis = V.scroll_memories(user_id=CHAT_USER_ID, memory_type=MEMORY_TYPE_EPISODIC,
                        limit=100, include_invalid=True)
for m in epis:
    V.delete_memory(m["memory_id"])
print(f"清掉 {len(epis)} 条 episodic 便于观察 L4", flush=True)

# 三条相近但不同的表达
l4_turns = [
    ("sess_L1", "我最近在负责一个订单系统迁移的项目,老 MySQL 拆到分库分表",
                "了解,分库分表是个大工程"),
    ("sess_L2", "我们订单迁移最近一直在解决双写一致性问题",
                "双写一致性可以考虑异步补偿"),
    ("sess_L3", "忙订单那个迁移的项目,还没搞完",
                "迁移项目往往战线拉得比较长"),
]

for i, (cid, u, a) in enumerate(l4_turns):
    print(f"\n-- 第 {i+1} 轮:{cid} --", flush=True)
    kv("user", u)
    r = S.write_chat_memory(u, a, chat_id=cid)
    kv("抽出 facts", [(f['memory_type'], f['content'][:40]) for f in r.get('facts',[])])
    kv("applied", r.get('applied',[]))

    # 每轮结束后看看兄弟状态
    epis_now = V.scroll_memories(user_id=CHAT_USER_ID, memory_type=MEMORY_TYPE_EPISODIC,
                                 limit=100, include_invalid=True)
    cores_now = V.scroll_memories(user_id=CHAT_USER_ID, memory_type=MEMORY_TYPE_CORE,
                                  chat_id="", limit=100, include_invalid=True)
    kv(f"  库中 episodic 数(第{i+1}轮后)", len([m for m in epis_now if m.get('valid',True)]))
    kv(f"  库中 core 数", len([m for m in cores_now if m.get('valid',True)]))

    # 找刚落库的这条 episodic + 它的兄弟
    if r.get('applied'):
        for m in epis_now:
            if m.get('chat_id') == cid and m.get('valid'):
                print(f"  刚落库 episodic:{m['memory_id']}  {m['content'][:50]}", flush=True)

print("\n-- L4 最终状态 --", flush=True)
all_final = V.scroll_memories(user_id=CHAT_USER_ID, limit=100, include_invalid=True)
for m in all_final:
    show_mem(m, indent="  ")

# 看 promotion 相关日志(如果有)
promoted = [m for m in all_final if m.get('promoted_from')]
kv("promoted_from 非空的 core", len(promoted))
for m in promoted:
    show_mem(m, indent="    ")

print("\n\n所有链路走完 ✓", flush=True)
