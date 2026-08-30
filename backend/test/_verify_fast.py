# -*- coding: utf-8 -*-
"""P0 + 类型合并 快批验证:B2 B3 B4 B5 B6 B7 C1 C2 D5 F2 H1 H3"""
import sys, io
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

from agent.memory import vector as V, retriever as R
from agent.memory.config import (CHAT_USER_ID, MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC,
                                 CORE_CHAR_BUDGET, EPISODIC_CAP)

ZV = [0.0] * 4096  # 零向量占位,不测检索质量,只测存储层/排序/预算

def add(content, mtype=MEMORY_TYPE_CORE, chat_id="", confidence=0.8,
        reinforce=0, verified=False, created_iso=None):
    p = V.insert_memory(content, vector=ZV, memory_type=mtype,
                        user_id=CHAT_USER_ID, chat_id=chat_id,
                        confidence=confidence, reinforce_count=reinforce,
                        verified=verified)
    if created_iso:
        V.get_client().set_payload(
            collection_name="agent_memories",
            payload={"created_at": created_iso},
            points=[V._point_id(p["memory_id"])], wait=True)
    return p["memory_id"]

def clean():
    for m in V.scroll_memories(user_id=CHAT_USER_ID, limit=1000, include_invalid=True):
        V.delete_memory(m["memory_id"])

results = []
def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    results.append((tag, name))
    line = f"{tag} {name}"
    if detail:
        line += f" | {detail}"
    print(line, flush=True)

def stage(msg):
    print(f"[stage] {msg}", flush=True)

# B2 次级 reinforce_count
stage("B2 reinforce次级")
clean()
add("A imp0.8 rc0", confidence=0.8, reinforce=0)
add("B imp0.8 rc5", confidence=0.8, reinforce=5)
core = R.retrieve_core_memories()
check("B2 reinforce次级", core[0]["content"] == "B imp0.8 rc5",
      f"top={core[0]['content']}")

# B3 末级 created_at 降序(新在前)
stage("B3 created_at 降序")
clean()
add("OLD 2020", confidence=0.8, reinforce=0, created_iso="2020-01-01T00:00:00+00:00")
add("NEW 2026", confidence=0.8, reinforce=0, created_iso="2026-08-29T00:00:00+00:00")
core = R.retrieve_core_memories()
same_key = [m for m in core if abs(m["confidence"]-0.8)<1e-6 and m["reinforce_count"]==0]
check("B3 created_at 降序(新在前)", same_key[0]["content"].startswith("NEW"),
      f"top-same-key={same_key[0]['content']}")

# B7 弃 top_k=6
stage("B7 弃top_k=6 (8 upserts)")
clean()
for i in range(8):
    add(f"core_{i:02d} short", confidence=0.5+i*0.05)
core = R.retrieve_core_memories()
block = R.build_core_block(core)
n_items = block.count("- core_")
check("B7 弃top_k=6", n_items > 6, f"注入条数={n_items}")

# B4 字符预算填充
stage("B4 字符预算 (10 upserts, 每条 ~300 字符, 总 3000 > 1500)")
clean()
big = "x" * 300
for i in range(10):
    add(f"{big}_{i:02d}", confidence=0.99-i*0.01)
core = R.retrieve_core_memories()
block = R.build_core_block(core)
body_len = len(block) - len("## 关于用户(始终参考)\n")
n_lines = block.count("\n- ")
check("B4 字符预算约束(< 1500 + 一条余量)", body_len <= CORE_CHAR_BUDGET + 350,
      f"body_len={body_len}, budget={CORE_CHAR_BUDGET}")
check("B4 收敛非空", n_lines >= 3, f"注入条数={n_lines}")

# B5 单条超预算保底
stage("B5 单条超预算")
clean()
add("y" * 2000, confidence=0.9)
core = R.retrieve_core_memories()
block = R.build_core_block(core)
check("B5 单条超预算保底非空", "- yyy" in block, f"block长度={len(block)}")

# B6 手动条(0.7) 不霸窗
stage("B6 手动条不霸窗")
clean()
V.insert_memory("手动条", vector=ZV, memory_type=MEMORY_TYPE_CORE,
                user_id=CHAT_USER_ID, confidence=0.7, verified=True)
V.insert_memory("抽取高价值", vector=ZV, memory_type=MEMORY_TYPE_CORE,
                user_id=CHAT_USER_ID, confidence=0.9)
core = R.retrieve_core_memories()
check("B6 手动条不霸窗", core[0]["content"] == "抽取高价值",
      f"top={core[0]['content']}")

# C1 只清超宽限的 valid=false 僵尸(宽限期内保留供回溯)
stage("C1 僵尸物删(超宽限)")
clean()
mid_alive = add("活着的 core", confidence=0.8)
mid_dead_recent = add("刚失效的 core", confidence=0.8)
mid_dead_old = add("远古失效 core", confidence=0.8)
V.invalidate_memory(mid_dead_recent)  # 刚失效,应在宽限期内保留
V.invalidate_memory(mid_dead_old)
# 手工把 mid_dead_old 的 invalid_at 改到远古(超 24h 宽限)
V.get_client().set_payload(
    collection_name="agent_memories",
    payload={"invalid_at": "2020-01-01T00:00:00+00:00"},
    points=[V._point_id(mid_dead_old)], wait=True)

removed = V.prune_global_preferences(user_id=CHAT_USER_ID)
alive = V.scroll_memories(user_id=CHAT_USER_ID, limit=100, include_invalid=True)
alive_ids = {m["memory_id"] for m in alive}
check("C1 超宽限僵尸被物删", mid_dead_old not in alive_ids, f"removed={removed}")
check("C1 宽限内 invalid 保留(供回溯)", mid_dead_recent in alive_ids)
check("C1 活跃 core 未动", mid_alive in alive_ids)

# C2 不按 reinforce 物删活跃 core
stage("C2 15 条活跃 core (功能等价 60 条,快跑)")
clean()
N_ACTIVE = 15
for i in range(N_ACTIVE):
    add(f"core_{i:03d}", confidence=0.5, reinforce=0)
removed = V.prune_global_preferences(user_id=CHAT_USER_ID)
count = len(V.scroll_memories(user_id=CHAT_USER_ID, limit=1000))
check(f"C2 {N_ACTIVE} 条活跃 core 全保留(不按 reinforce 物删)",
      removed == 0 and count == N_ACTIVE,
      f"removed={removed}, remaining={count}")

# D5 prune_episodic 独立
stage("D5 60条 episodic (60 upserts + 60 set_payload)")
clean()
for i in range(60):
    p = V.insert_memory(f"epi_{i:03d}", vector=ZV, memory_type=MEMORY_TYPE_EPISODIC,
                        user_id=CHAT_USER_ID, chat_id="sessX", confidence=0.5)
    V.get_client().set_payload(collection_name="agent_memories",
        payload={"created_at": "2020-01-01T00:00:00+00:00"},
        points=[V._point_id(p["memory_id"])], wait=True)
inv = V.prune_episodic("sessX", user_id=CHAT_USER_ID)
alive_epi = len(V.scroll_memories(user_id=CHAT_USER_ID, chat_id="sessX",
                                  memory_type=MEMORY_TYPE_EPISODIC, limit=1000))
check("D5 episodic 软失效尾部", alive_epi <= 40 and inv >= 20,
      f"invalidated={inv}, alive={alive_epi}, cap={EPISODIC_CAP}")

# H1 payload 全字段
clean()
mid = add("sample", confidence=0.8)
p = V.get_memory(mid)
required = {"memory_id","content","hash","memory_type","scope","domain","user_id",
            "chat_id","created_at","updated_at","confidence","verified","entry_url",
            "keywords","reinforce_count","last_accessed_at","valid"}
missing = required - set(p.keys())
check("H1 payload 字段齐全", not missing, f"missing={missing}")

# H3 dense+sparse 双向量
info = V.get_client().get_collection(collection_name="agent_memories")
vconf = info.config.params.vectors
sconf = info.config.params.sparse_vectors
check("H3 dense 向量存在", "dense" in vconf, f"dense_size={vconf['dense'].size}")
check("H3 sparse 向量存在", bool(sconf and "text" in sconf))

# F2 空库降级
clean()
core = R.retrieve_core_memories()
block = R.build_core_block(core)
check("F2 空库返回空", core == [] and block == "")

print()
print("=" * 40)
passed = sum(1 for t,_ in results if t == "PASS")
print(f"快批: {passed}/{len(results)} pass")
for t, n in results:
    if t == "FAIL":
        print(f"  ! {n}")
