# -*- coding: utf-8 -*-
"""跨会话晋升(批次 D 简化版)。

批次 D 后,跨会话稳定事实晋升由 writer 的 CONSOLIDATE_SYSTEM_PROMPT 一次判定
(action=promote,直接以 core 落库、target_ids 兄弟软失效),不再需要独立
检测/判定链路。本模块只保留 demote_memory 供 CRUD 层"撤销晋升"的手动回退。

批次 B 遗留的 detect_recurrence / _llm_judge_stable / promote_memory /
STABILITY_SYSTEM_PROMPT 均已删除——它们的功能被 CONSOLIDATE 吸收。
"""

from __future__ import annotations

from typing import Optional

from agent.memory import vector as V
from agent.memory import history as H
from agent.memory.config import (
    MEMORY_TYPE_EPISODIC,
    MEMORY_COLLECTION,
)


def demote_memory(memory_id: str) -> bool:
    """晋升回退:core → episodic,chat_id 恢复到 promoted_from。

    供记忆面板"撤销晋升"UI 或误升的手动修复。仅当 promoted_from 非空
    (即真的是晋升上来的)才回退。批次 D 保留此函数用于人工纠错。
    """
    m = V.get_memory(memory_id)
    if not m or not m.get("promoted_from"):
        return False
    try:
        V.get_client().set_payload(
            collection_name=MEMORY_COLLECTION,
            payload={
                "chat_id": m["promoted_from"],
                "memory_type": MEMORY_TYPE_EPISODIC,
                "promoted_from": "",
            },
            points=[V._point_id(memory_id)], wait=True)
        H.add_history(memory_id, "DEMOTE", "", m.get("content", ""))
        try:
            from agent.memory import list_cache
            list_cache.invalidate_all()
        except Exception:
            pass
        return True
    except Exception:
        return False
