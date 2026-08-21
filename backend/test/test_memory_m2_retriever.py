"""M2 检索器验证:retriever.py 对着真实 Qdrant + embedding 跑。

覆盖:相关召回、不相关不召回(threshold)、空库返回空、domain 过滤、
注入块组装格式。用独立测试 collection。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.memory.config as cfg
cfg.MEMORY_COLLECTION = "agent_memories_m2_test"

from agent.memory import vector as V
from agent.memory import retriever as R
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

    print("=== M2 检索器验证 ===")

    # 空库:检索返回空
    check("空库检索返回空列表", R.retrieve_memories("任何查询") == [])
    check("空记忆组装块返回空串", R.build_memory_block([]) == "")

    # 写入几条不同主题的记忆
    facts = [
        ("用户喜欢喝美式咖啡,不加糖", "user", ""),
        ("用户习惯用深色主题界面", "user", ""),
        ("公司报销系统的提交按钮在页面最底部", "domain", "expense.corp.com"),
    ]
    for content, scope, domain in facts:
        p = V.insert_memory(content, vector=embed_text(content), scope=scope, domain=domain)

    # 相关召回:问咖啡应召回咖啡记忆
    hits = R.retrieve_memories("我想喝什么咖啡")
    contents = [h["content"] for h in hits]
    check("相关查询召回咖啡记忆", any("美式咖啡" in c for c in contents))

    # 不相关不召回(threshold 生效):问一个完全无关的
    unrelated = R.retrieve_memories("量子物理的波函数坍缩")
    check("不相关查询被 threshold 过滤(不召回咖啡/主题)",
          not any("咖啡" in h["content"] or "深色主题" in h["content"] for h in unrelated))

    # domain 过滤:带 URL 的任务检索能召回该站点记忆
    dhits = R.retrieve_for_task("怎么提交报销", url="https://expense.corp.com/new")
    check("带 domain 的任务召回站点记忆",
          any("报销系统" in h["content"] for h in dhits))

    # 不同 domain 不串味:另一个站点不应召回 expense 的记忆
    other = R.retrieve_for_task("提交按钮在哪", url="https://other-site.com/page")
    check("其它 domain 不召回 expense 站点记忆",
          not any("报销系统" in h["content"] for h in other))

    # 注入块组装格式
    block = R.build_memory_block(hits)
    check("注入块含标题", "## 相关记忆" in block)
    check("注入块含 bullet", "- " in block)

    # domain 记忆的块带站点标签
    dblock = R.build_memory_block(dhits)
    check("domain 记忆块带 [站点] 标签", "[expense.corp.com]" in dblock)

    # extract_domain
    check("extract_domain 正常", R.extract_domain("https://a.b.com/x?y=1") == "a.b.com")
    check("extract_domain 非法返回空", R.extract_domain("not a url") == "")

    _cleanup()
    print(f"\n{'✅ 全部通过' if not failures else '❌ 失败: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
