"""M3 写入器验证:writer.py 两阶段 + 反幻觉。用 mock LLM(不走网络)。

覆盖计划 M3 单测点:
- 新事实 ADD、矛盾 DELETE、同义 NONE、更新命中真实 id
- 反幻觉:mock LLM 返回真实 UUID 当 id,验证代码只认临时整数 mapping,伪造 id 不生效
- hash 命中直接跳过不调决策 LLM
只有 embedding 走真实网络(写入/检索需要向量);LLM 全 mock。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.memory.config as cfg
cfg.MEMORY_COLLECTION = "agent_memories_m3_test"

from agent.memory import vector as V
from agent.memory import writer as W


def _cleanup():
    try:
        V.get_client().delete_collection(collection_name=cfg.MEMORY_COLLECTION)
    except Exception:
        pass


def make_llm(extract_facts_out, decision_out):
    """构造 mock LLM:根据 system prompt 区分抽取/决策阶段,返回预设结果。

    decision_out 可以是 dict(固定)或 callable(按调用次数变化)。
    记录调用,便于断言"是否调了决策 LLM"。
    """
    calls = {"extract": 0, "decision": 0}

    def llm(system_prompt, user_prompt):
        if "记忆整理器" in system_prompt:  # 抽取阶段
            calls["extract"] += 1
            return extract_facts_out
        else:  # 决策阶段
            calls["decision"] += 1
            if callable(decision_out):
                return decision_out(calls["decision"], user_prompt)
            return decision_out

    llm.calls = calls
    return llm


def main() -> int:
    failures = []

    def check(name, cond):
        print(f"  {'✓' if cond else '✗'} {name}")
        if not cond:
            failures.append(name)

    print("=== M3 写入器验证(mock LLM)===")

    # ── 场景1:空库 + 新事实 → ADD ──
    _cleanup(); V._collection_ready = False
    llm = make_llm(
        {"facts": [{"content": "用户偏好键盘操作", "scope": "user", "domain": ""}]},
        {"memory": [{"id": "0", "text": "用户偏好键盘操作", "event": "ADD"}]},
    )
    r = W.write_memory("填个表单", "Tab 切换填写", llm=llm)
    check("空库新事实 → ADD 落库", V.count_memories() == 1)
    check("applied 记录 ADD", any(a.startswith("ADD") for a in r["applied"]))

    # ── 场景2:同义事实 → NONE(不新增)──
    llm2 = make_llm(
        {"facts": [{"content": "用户喜欢用键盘", "scope": "user", "domain": ""}]},
        {"memory": [{"id": "0", "text": "用户偏好键盘操作", "event": "NONE"}]},
    )
    W.write_memory("又填表单", "还是 Tab", llm=llm2)
    check("同义事实 → NONE,count 仍为 1", V.count_memories() == 1)

    # ── 场景3:更新命中真实 id ──
    # 取现有那条的真实 id
    existing = V.search_memories(__import__("rag.embedder", fromlist=["embed_text"]).embed_text("键盘"),
                                 top_k=5, scope="user")
    real_id = existing[0]["memory_id"]
    llm3 = make_llm(
        {"facts": [{"content": "用户偏好键盘,尤其 Ctrl 组合键", "scope": "user", "domain": ""}]},
        {"memory": [{"id": "0", "text": "用户偏好键盘,尤其 Ctrl 组合键", "event": "UPDATE",
                     "old_memory": "用户偏好键盘操作"}]},
    )
    W.write_memory("x", "y", llm=llm3)
    updated = V.get_memory(real_id)
    check("UPDATE 命中真实 id,内容更新", updated and "Ctrl" in updated["content"])
    check("UPDATE 后 count 仍为 1(未新增)", V.count_memories() == 1)

    # ── 场景4(核心):反幻觉 —— LLM 返回真实 UUID / 越界 id,不得生效 ──
    # LLM 决策里 UPDATE 一个"看起来像真实 memory_id"的伪造 id(不在临时 mapping "0" 里)
    fake_id = "mem_deadbeefdeadbeefdeadbeefdeadbeef"
    llm4 = make_llm(
        {"facts": [{"content": "用户偏好键盘,尤其 Ctrl 组合键", "scope": "user", "domain": ""}]},
        # LLM 越权:用真实/伪造 UUID 当 id 去 UPDATE + 一个越界整数 DELETE
        {"memory": [
            {"id": fake_id, "text": "被劫持的内容", "event": "UPDATE"},
            {"id": "99", "text": "", "event": "DELETE"},
        ]},
    )
    before = V.get_memory(real_id)["content"]
    r4 = W.write_memory("z", "w", llm=llm4)
    after = V.get_memory(real_id)
    check("反幻觉:伪造 UUID 的 UPDATE 不生效(真实记忆未被篡改)",
          after is not None and after["content"] == before)
    check("反幻觉:越界整数 id 的 DELETE 不生效(count 不变)", V.count_memories() == 1)
    check("反幻觉:伪造 id 操作不进 applied", not any(fake_id in a for a in r4["applied"]))

    # ── 场景5:hash 命中直接跳过,不调决策 LLM ──
    # 先拿到当前那条的确切正文,构造完全相同的抽取结果
    cur = V.get_memory(real_id)["content"]
    llm5 = make_llm(
        {"facts": [{"content": cur, "scope": "user", "domain": ""}]},
        {"memory": []},  # 若被调用会返回空
    )
    r5 = W.write_memory("dup", "dup", llm=llm5)
    check("hash 命中 → skipped_hash 计数", r5["skipped_hash"] >= 1)
    check("hash 命中 → 未调决策 LLM", llm5.calls["decision"] == 0)

    # ── 场景6:抽取返回空 → 什么都不做 ──
    llm6 = make_llm({"facts": []}, {"memory": []})
    r6 = W.write_memory("无可记的任务", "随便点点", llm=llm6)
    check("抽取空 → applied 为空", r6["applied"] == [])
    check("抽取空 → 未调决策 LLM", llm6.calls["decision"] == 0)

    _cleanup()
    print(f"\n{'✅ 全部通过' if not failures else '❌ 失败: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
