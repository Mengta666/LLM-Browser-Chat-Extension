"""聊天历史 API，负责列出对话、读取历史消息和软删除对话。"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from storage.db import db


router = APIRouter(prefix="/api/chats", tags=["chat history"])


def parse_json_array(value: str) -> list[Any]:
    """解析 SQLite 文本字段里的 JSON 数组，失败时返回空列表。"""
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


@router.get("")
def list_chats() -> dict[str, Any]:
    """返回所有未删除对话及其最新轻量摘要。"""
    return {"chats": db.list_chats_with_latest_summary()}


@router.get("/{chat_id}/messages")
def list_chat_messages(chat_id: str) -> dict[str, Any]:
    """返回某个对话中可直接渲染的历史消息。"""
    messages = []
    for message in db.list_chat_display_messages(chat_id.strip()):
        messages.append({
            "message_id": message["message_id"],
            "turn_id": message["turn_id"],
            "role": message["role"],
            "display_content": message["display_content"],
            "sources": parse_json_array(message["sources_json"]),
            "content_format": message["content_format"],
            "status": message["status"],
            "created_at": message["created_at"],
            "updated_at": message["updated_at"],
        })
    return {"chat_id": chat_id.strip(), "messages": messages}


@router.delete("/{chat_id}")
def delete_chat(chat_id: str) -> dict[str, Any]:
    """把对话标记为已删除，使其从历史列表中隐藏。"""
    normalized_chat_id = chat_id.strip()
    if not normalized_chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")
    deleted = db.soft_delete_chat(normalized_chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="chat not found")
    return {"chat_id": normalized_chat_id, "deleted": True}
