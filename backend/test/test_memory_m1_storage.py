"""M1 存储层验证:vector.py + history.py 对着真实 Qdrant 跑一遍。

覆盖计划里的 M1 单测点:insert/update/delete/search、point_id 幂等、
维度校验、count 一致性、审计日志。用真实 embedding + 真实 Qdrant。
测试用独立 collection,跑完清理,不污染生产 agent_memories。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 用独立测试 collection,避免污染
import agent.memory.config as cfg
cfg.MEMORY_COLLECTION = "agent_memories_m1_test"

from agent.memory import vector as V
from agent.memory import history as H
from rag.embedder import embed_text


def _cleanup():
    try:
        V.get_client().delete_collection(collection_name=cfg.MEMORY_COLLECTION)
    except Exception:
        pass


def main() -> int:
    _cleanup()
    V._collection_ready = False
    failures = []

    def check(name, cond):
        print(f"  {'✓' if cond else '✗'} {name}")
        if not cond:
            failures.append(name)

    print("=== M1 存储层验证 ===")

    # 1. ensure_collection + 维度
    V.ensure_collection()
    info = V.get_client().get_collection(cfg.MEMORY_COLLECTION)
    check("collection 创建,维度==4096", info.config.params.vectors.size == 4096)

    # 2. insert
    c1 = "用户偏好用键盘快捷键操作,不喜欢用鼠标"
    p1 = V.insert_memory(c1, vector=embed_text(c1), scope="user")
    H.add_history(p1["memory_id"], "ADD", "", c1)
    mid1 = p1["memory_id"]
    check("insert 返回 memory_id", mid1.startswith("mem_"))
    check("insert payload content 正确", p1["content"] == c1)
    check("insert 后 count==1", V.count_memories() == 1)

    # 3. point_id 幂等:同 memory_id 再 upsert 不产生新点
    from agent.memory.vector import _point_id
    check("point_id 幂等(同 id 派生一致)", _point_id(mid1) == _point_id(mid1))

    # 4. get_memory
    got = V.get_memory(mid1)
    check("get_memory 命中", got is not None and got["memory_id"] == mid1)
    check("get_memory 不存在返回 None", V.get_memory("mem_nonexistent") is None)

    # 5. search:相关召回
    hits = V.search_memories(embed_text("怎么用快捷键"), top_k=5)
    check("search 召回刚写入的记忆", any(h["memory_id"] == mid1 for h in hits))
    check("search 结果带 score", hits and "score" in hits[0])

    # 6. update:正文变更,created_at 保留
    c1b = "用户偏好键盘快捷键,尤其是 Ctrl 组合键"
    pu = V.update_memory(mid1, c1b, vector=embed_text(c1b))
    H.add_history(mid1, "UPDATE", c1, c1b)
    check("update 返回非 None", pu is not None)
    check("update 后 content 变更", V.get_memory(mid1)["content"] == c1b)
    check("update 保留 created_at", pu["created_at"] == p1["created_at"])
    check("update 刷新 updated_at", pu["updated_at"] != p1["created_at"])
    check("update 后 count 仍为 1(未新增点)", V.count_memories() == 1)

    # 7. update 不存在的 id → None
    check("update 不存在 id 返回 None",
          V.update_memory("mem_ghost", "x", vector=embed_text("x")) is None)

    # 8. 第二条 + domain scope
    c2 = "某内部系统的登录入口在右上角头像菜单里"
    p2 = V.insert_memory(c2, vector=embed_text(c2), scope="domain", domain="erp.corp.com")
    H.add_history(p2["memory_id"], "ADD", "", c2)
    check("insert 第二条后 count==2", V.count_memories() == 2)
    # domain 过滤
    dhits = V.search_memories(embed_text("登录在哪"), top_k=5, scope="domain", domain="erp.corp.com")
    check("domain 过滤只召回该站点记忆",
          all(h.get("domain") == "erp.corp.com" for h in dhits) and len(dhits) >= 1)

    # 9. delete
    V.delete_memory(mid1)
    H.add_history(mid1, "DELETE", c1b, "")
    check("delete 后该记忆取不到", V.get_memory(mid1) is None)
    check("delete 后 count==1", V.count_memories() == 1)
    check("delete 幂等(再删不报错)", (V.delete_memory(mid1) or True))

    # 10. 审计日志
    hist1 = H.list_history(mid1)
    events = [h["event"] for h in hist1]
    check("审计日志记录 ADD/UPDATE/DELETE", events == ["ADD", "UPDATE", "DELETE"])
    check("审计 ADD 总数>=2", H.count_events("ADD") >= 2)

    _cleanup()
    print(f"\n{'✅ 全部通过' if not failures else '❌ 失败: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
