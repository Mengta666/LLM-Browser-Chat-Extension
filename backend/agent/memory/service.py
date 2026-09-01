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
    """chat 对话抽取并写入记忆(core/episodic)。任何异常都吞掉。

    chat_id 用于 episodic 会话隔离;写后剪枝:全局 core 防堆积 + 本会话 episodic 容量 GC。
    """
    uid = user_id or CHAT_USER_ID
    try:
        result = W.write_chat_memory(
            user_msg, assistant_msg, history_summary=history_summary,
            user_id=uid, chat_id=chat_id)
        # 写后剪枝 core 层(只清 valid=false 僵尸条,活跃 core 不物理删)
        try:
            V.prune_global_preferences(user_id=uid)
        except Exception:
            pass
        # 写后剪枝本会话 episodic(容量上限,软失效可回溯;只扫这一个会话)
        if chat_id:
            try:
                V.prune_episodic(chat_id, user_id=uid)
            except Exception:
                pass
        # B5 core 摘要(异步,不阻塞主链):超预算才触发 LLM 分组摘要
        # 记忆是"锦上添花",压缩失败静默吞
        # 注:rethink 冲突整理**不在这里触发**——每次写记忆都跑 LLM 全库扫过于激进,
        # 且 chat 首次配置阶段就会带来不必要的 LLM 消耗。
        # 只在两处触发:①用户手动点前端"整理"按钮 ②后台 daemon 每 24h 周期
        try:
            import threading
            from agent.memory import summarize
            threading.Thread(
                target=summarize.maybe_compact_core, args=(uid,),
                daemon=True, name="core-compact").start()
        except Exception:
            pass
        return result
    except Exception:
        return {"facts": [], "applied": [], "skipped_hash": 0}
