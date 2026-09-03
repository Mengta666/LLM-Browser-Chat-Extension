"""记忆子系统对外门面(chat 长期记忆)。

chat 只跟这几个函数打交道(分层:core 常驻 + episodic 按需):
- get_core_memories:每轮对话取常驻记忆(core),注入 prompt。
- recall_episodic:按需召回事件记忆(过相关性闸门)。
- write_chat_memory:对话结束后台抽取写入(core/episodic)。
- build_core_block / build_episodic_block:记忆 → 注入文本块。

所有函数都对异常宽容:记忆是"锦上添花",绝不能因为它出错而拖垮 chat 主流程。
"""

from typing import Any

from agent.memory import retriever as R
from agent.memory import writer as W
from agent.memory import vector as V
from agent.memory.config import CHAT_USER_ID
from observability.logger import get_logger

_log = get_logger("memory")


def get_core_memories(user_id: str = None) -> list[dict[str, Any]]:
    """取 chat 常驻记忆(core),供每轮注入。异常降级为空。"""
    try:
        return R.retrieve_core_memories(user_id=user_id or CHAT_USER_ID)
    except Exception:
        return []


def build_core_block(memories: list[dict[str, Any]]) -> str:
    """chat 常驻记忆 → 注入块文本。"""
    try:
        return R.build_core_block(memories)
    except Exception:
        return ""


def recall_episodic(query: str, user_id: str = None, chat_id: str = "") -> list[dict[str, Any]]:
    """chat 事件记忆按需召回(仅本会话 chat_id,过双闸门 + 三因子重排)。异常降级为空。"""
    try:
        return R.recall_episodic_memories(
            query, user_id=user_id or CHAT_USER_ID, chat_id=chat_id)
    except Exception:
        return []


def build_episodic_block(memories: list[dict[str, Any]]) -> str:
    """chat 事件召回 → 注入块文本(空则空串,不注入)。"""
    try:
        return R.build_episodic_block(memories)
    except Exception:
        return ""


def write_chat_memory(user_msg: str, assistant_msg: str,
                      history_summary: str = "", user_id: str = None,
                      chat_id: str = "") -> dict[str, Any]:
    """chat 对话抽取并写入记忆(core/episodic)。任何异常都吞掉。"""
    uid = user_id or CHAT_USER_ID
    try:
        result = W.write_chat_memory(
            user_msg, assistant_msg, history_summary=history_summary,
            user_id=uid, chat_id=chat_id)
        try:
            pruned = V.prune_global_preferences(user_id=uid)
            if pruned:
                _log.info("memory_prune_core", session_id=chat_id,
                          data={"pruned_count": pruned if isinstance(pruned, int) else 0})
        except Exception:
            pass
        if chat_id:
            try:
                pruned_e = V.prune_episodic(chat_id, user_id=uid)
                if pruned_e:
                    _log.info("memory_prune_episodic", session_id=chat_id,
                              data={"pruned_count": pruned_e if isinstance(pruned_e, int) else 0})
            except Exception:
                pass
        try:
            import threading
            from agent.memory import summarize
            threading.Thread(
                target=summarize.maybe_compact_core, args=(uid,),
                daemon=True, name="core-compact").start()
        except Exception:
            pass
        return result
    except Exception as exc:
        _log.error("memory_service_failed", session_id=chat_id,
                   data={"error": str(exc)[:200]})
        return {"facts": [], "applied": [], "skipped_hash": 0}
