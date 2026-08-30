# -*- coding: utf-8 -*-
"""评测脚手架:setup/teardown 工具 + 直驱 writer 绕 daemon + polling。

批次 B 所有跨会话+异步测试都基于此。核心 API:
- reset_collection(): 清库到干净
- seed(cases): 直接手动 insert seed_memories(不走 LLM 抽取)
- force_write_sync(user, asst, chat_id): 同步调 writer.write_chat_memory
- poll_until(fn, deadline): HTTP 用,等 daemon 完成
- distinct_chat_ids(memory_ids): 统计一批记忆覆盖的 chat_id 数
"""
import sys, json, time
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).absolute().parents[2]))

from agent.memory import vector as V
from agent.memory import service as S
from agent.memory.config import (
    CHAT_USER_ID, MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC,
    SCOPE_GLOBAL, MEMORY_COLLECTION,
)


def reset_collection():
    """删除 agent_memories collection 重建,回到干净状态。"""
    c = V.get_client()
    try:
        c.delete_collection(collection_name=MEMORY_COLLECTION)
    except Exception:
        pass
    V._collection_ready = False
    V.ensure_collection()


def clean(user_id: str = CHAT_USER_ID):
    """清空指定 user_id 的所有记忆(含 invalid),collection 保留。"""
    items = V.scroll_memories(user_id=user_id, limit=1000, include_invalid=True)
    for m in items:
        try:
            V.delete_memory(m["memory_id"])
        except Exception:
            pass


def seed_one(content: str, *,
             memory_type: str = MEMORY_TYPE_CORE,
             chat_id: str = "",
             confidence: float = 0.8,
             reinforce_count: int = 0,
             verified: bool = False,
             created_iso: Optional[str] = None,
             use_zero_vector: bool = True,
             tags: Optional[list[str]] = None,
             valid: bool = True,
             user_id: str = CHAT_USER_ID) -> str:
    """手动 insert 一条 seed(不走 LLM 抽取)。返回 memory_id。

    use_zero_vector=True 时用零向量占位(检索评测不适合;晋升评测的兄弟集需真向量)。
    valid=False 时插入后立即 invalidate_memory(模拟被 GC 的软失效兄弟)。
    tags 只是给评测集的元数据,不影响存储。
    """
    if use_zero_vector:
        vec = [0.0] * 4096
    else:
        from rag.embedder import embed_text
        vec = embed_text(content)
    payload = V.insert_memory(
        content, vector=vec,
        memory_type=memory_type,
        scope=SCOPE_GLOBAL,
        user_id=user_id,
        chat_id=chat_id,
        confidence=confidence,
        reinforce_count=reinforce_count,
        verified=verified,
    )
    if created_iso:
        V.get_client().set_payload(
            collection_name=MEMORY_COLLECTION,
            payload={"created_at": created_iso},
            points=[V._point_id(payload["memory_id"])], wait=True)
    if not valid:
        V.invalidate_memory(payload["memory_id"])
    return payload["memory_id"]


def seed_case(case: dict) -> dict[str, str]:
    """按 case 里的 seed_memories 结构批量 seed。返回 {tag: memory_id}(供后续断言用)。

    tag 常见值:"gold"(应被召回)、"distractor"(不该被召回)、
    "sibling_A"/"sibling_B"/... (晋升兄弟)。
    """
    tag_to_id: dict[str, str] = {}
    for s in case.get("seed_memories", []):
        # 复现类必须用真向量,别的可零向量
        use_zero = case.get("type") != "promotion"
        mid = seed_one(
            content=s["content"],
            memory_type=s.get("memory_type", MEMORY_TYPE_CORE),
            chat_id=s.get("chat_id", ""),
            confidence=s.get("confidence", 0.8),
            reinforce_count=s.get("reinforce_count", 0),
            verified=s.get("verified", False),
            created_iso=s.get("created_at"),
            use_zero_vector=use_zero,
            valid=s.get("valid", True),
        )
        for tag in s.get("tags", []):
            tag_to_id[tag] = mid
        tag_to_id[s.get("id", mid)] = mid  # case 内引用 id 也建映射
    return tag_to_id


def force_write_sync(user_msg: str, assistant_msg: str,
                     chat_id: str, history_summary: str = "") -> dict:
    """绕过 chat.py daemon,同步调 writer.write_chat_memory。

    评测的所有晋升 case 都靠这个——不需要等 30s daemon,直接拿结果。
    """
    return S.write_chat_memory(
        user_msg, assistant_msg,
        history_summary=history_summary,
        chat_id=chat_id)


def poll_until(check_fn, deadline_s: float = 60.0,
               interval_s: float = 2.0) -> bool:
    """HTTP 类 case:poll 到 check_fn 返 True 或超时。"""
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            if check_fn():
                return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


def distinct_chat_ids(memory_ids) -> set[str]:
    """统计一批 memory_id 覆盖的 distinct chat_id 数(晋升判定用)。"""
    chats = set()
    for mid in memory_ids:
        m = V.get_memory(str(mid))
        if m:
            chats.add(m.get("chat_id", ""))
    return chats


def load_cases(kind: str) -> list[dict]:
    """加载指定类型的 cases。kind ∈ retrieval/update/promotion/rejection/compaction。"""
    p = Path(__file__).parent / "cases" / f"{kind}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
