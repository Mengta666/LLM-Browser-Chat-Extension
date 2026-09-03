"""会话历史 API(列表 / 载入 / 重命名 / 删除)。

供前端"会话列表 + 续谈":列出所有会话、载入某会话消息重建对话、重命名、软删。
数据源 storage/chat_store.py(SQLite)。存储不可用时返回 503,不影响 chat 主流程。
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage import chat_store as CS
from observability.logger import get_logger

_sess_log = get_logger("sessions_api")

router = APIRouter(prefix="/v1/sessions", tags=["会话历史"])


class SessionPatchBody(BaseModel):
    title: str


@router.get("/list")
def list_sessions() -> dict[str, Any]:
    """列出未删除会话(最近在前)。"""
    try:
        sessions = CS.list_sessions()
    except Exception as exc:
        raise HTTPException(503, f"会话历史不可用: {str(exc)[:160]}")
    return {"sessions": sessions, "count": len(sessions)}


@router.get("/{chat_id}/messages")
def get_session_messages(chat_id: str) -> dict[str, Any]:
    """载入某会话的消息(时间正序,供续谈重建对话)。含会话摘要(如有)供前端恢复上下文。"""
    try:
        messages = CS.get_messages(chat_id)
    except Exception as exc:
        raise HTTPException(503, f"会话历史不可用: {str(exc)[:160]}")
    # 会话摘要(上下文压缩产物)
    try:
        summary_info = CS.get_summary(chat_id)
    except Exception:
        summary_info = {"summary": "", "msg_count": 0}
    return {
        "chat_id": chat_id,
        "messages": messages,
        "count": len(messages),
        "summary": summary_info.get("summary", ""),
        "summary_msg_count": summary_info.get("msg_count", 0),
    }


@router.patch("/{chat_id}")
def rename_session(chat_id: str, body: SessionPatchBody) -> dict[str, Any]:
    """重命名会话。"""
    try:
        ok = CS.rename_session(chat_id, body.title)
    except Exception as exc:
        raise HTTPException(503, f"重命名失败: {str(exc)[:160]}")
    if not ok:
        raise HTTPException(404, f"会话 {chat_id} 不存在")
    _sess_log.info("session_renamed", data={"chat_id": chat_id})
    return {"chat_id": chat_id, "title": body.title}


@router.delete("/{chat_id}")
def delete_session(chat_id: str) -> dict[str, Any]:
    """软删会话(消息保留可回溯)。"""
    try:
        ok = CS.soft_delete(chat_id)
    except Exception as exc:
        raise HTTPException(503, f"删除失败: {str(exc)[:160]}")
    if not ok:
        raise HTTPException(404, f"会话 {chat_id} 不存在")
    _sess_log.info("session_deleted", data={"chat_id": chat_id})
    return {"chat_id": chat_id, "deleted": True}
