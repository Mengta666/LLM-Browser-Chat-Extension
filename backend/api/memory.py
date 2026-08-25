"""记忆管理 API(用户可见/可编辑/可删,对齐 ChatGPT/Claude 的记忆控制)。

针对 chat 记忆(CHAT_USER_ID 命名空间):列出、看单条、改内容、删(默认软删可回溯)、
手动新增。删除默认走时间失效(invalidate,可回溯);hard=true 才物理删。

所有写操作在 Qdrant 不可用时返回 503,不影响 chat 主流程(chat 侧自身降级)。
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from agent.memory import vector as V
from agent.memory.config import (
    CHAT_USER_ID, SCOPE_GLOBAL,
    MEMORY_TYPE_PERSONA, MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_EPISODIC,
)
from rag.embedder import embed_text
from observability.logger import get_logger

_mem_log = get_logger("memory_api")

router = APIRouter(prefix="/v1/memory", tags=["记忆管理"])

_CHAT_TYPES = {MEMORY_TYPE_PERSONA, MEMORY_TYPE_PREFERENCE, MEMORY_TYPE_EPISODIC}


class MemoryCreate(BaseModel):
    content: str
    memory_type: str = MEMORY_TYPE_EPISODIC


class MemoryPatch(BaseModel):
    content: str


def _view(payload: dict[str, Any]) -> dict[str, Any]:
    """裁剪成前端友好的记忆视图(不暴露向量/hash 等内部字段)。"""
    return {
        "memory_id": payload.get("memory_id", ""),
        "content": payload.get("content", ""),
        "memory_type": payload.get("memory_type", ""),
        "created_at": payload.get("created_at", ""),
        "updated_at": payload.get("updated_at", ""),
        "valid": payload.get("valid", True),
        "invalid_at": payload.get("invalid_at", ""),
    }


@router.get("/list")
def list_memories(memory_type: Optional[str] = Query(None),
                  include_invalid: bool = Query(False)) -> dict[str, Any]:
    """列出 chat 记忆。可按 memory_type 过滤;include_invalid=true 含已失效(回溯)。"""
    try:
        items = V.scroll_memories(
            user_id=CHAT_USER_ID, memory_type=memory_type, scope=SCOPE_GLOBAL,
            limit=1000, include_invalid=include_invalid)
    except Exception as exc:
        raise HTTPException(503, f"记忆库不可用: {str(exc)[:160]}")
    views = [_view(m) for m in items]
    # 新的在前
    views.sort(key=lambda m: str(m.get("created_at", "")), reverse=True)
    return {"memories": views, "count": len(views)}


@router.get("/{memory_id}")
def get_memory(memory_id: str) -> dict[str, Any]:
    try:
        payload = V.get_memory(memory_id)
    except Exception as exc:
        raise HTTPException(503, f"记忆库不可用: {str(exc)[:160]}")
    if payload is None:
        raise HTTPException(404, f"记忆 {memory_id} 不存在")
    return _view(payload)


@router.post("")
def create_memory(item: MemoryCreate) -> dict[str, Any]:
    """用户手动新增一条记忆(对齐 ChatGPT『记住…』)。"""
    content = item.content.strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    mtype = item.memory_type if item.memory_type in _CHAT_TYPES else MEMORY_TYPE_EPISODIC
    try:
        payload = V.insert_memory(
            content, vector=embed_text(content),
            memory_type=mtype, scope=SCOPE_GLOBAL, domain="",
            user_id=CHAT_USER_ID, confidence=1.0, verified=True,  # 用户手动 = 已验证
        )
    except Exception as exc:
        raise HTTPException(503, f"写入失败: {str(exc)[:160]}")
    _mem_log.info("memory_created", data={"memory_id": payload.get("memory_id"), "type": mtype})
    return _view(payload)


@router.patch("/{memory_id}")
def patch_memory(memory_id: str, item: MemoryPatch) -> dict[str, Any]:
    """编辑记忆正文(内部重算 embedding)。"""
    content = item.content.strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    try:
        updated = V.update_memory(memory_id, content, vector=embed_text(content))
    except Exception as exc:
        raise HTTPException(503, f"更新失败: {str(exc)[:160]}")
    if updated is None:
        raise HTTPException(404, f"记忆 {memory_id} 不存在")
    _mem_log.info("memory_patched", data={"memory_id": memory_id})
    return _view(updated)


@router.delete("/{memory_id}")
def delete_memory(memory_id: str, hard: bool = Query(False)) -> dict[str, Any]:
    """删除记忆:默认软删(失效,可回溯);hard=true 物理删除。"""
    try:
        if hard:
            V.delete_memory(memory_id)
            _mem_log.info("memory_deleted", data={"memory_id": memory_id, "hard": True})
            return {"memory_id": memory_id, "deleted": True, "hard": True}
        result = V.invalidate_memory(memory_id)
        if result is None:
            raise HTTPException(404, f"记忆 {memory_id} 不存在")
        _mem_log.info("memory_invalidated", data={"memory_id": memory_id})
        return {"memory_id": memory_id, "invalidated": True, "hard": False}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"删除失败: {str(exc)[:160]}")
