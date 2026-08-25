"""记忆子系统对外门面。

agent 只跟这几个函数打交道(分层召回 + 写入):
- get_resident_preferences:任务开始/每步,取常驻用户偏好(注入 prompt)。
- recall_site_experience:recall 工具调用,按需召回站点经验+教训。
- count_site_memories:启发式兜底用,数某站点有几条经验。
- write_after_task:任务结束后台抽取写入(success 区分 成功→偏好/经验 / 失败→教训)。

所有函数都对异常宽容:记忆是"锦上添花",绝不能因为它出错而拖垮 agent 主流程。
"""

from typing import Any

from agent.memory import retriever as R
from agent.memory import writer as W
from agent.memory import vector as V
from agent.memory.retriever import extract_domain
from agent.memory.config import (
    DEFAULT_USER_ID, MEMORY_TYPE_SITE_EXPERIENCE,
)


def get_resident_preferences(user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """取常驻用户偏好(供每步 prompt 注入)。异常降级为空。"""
    try:
        return R.retrieve_resident_preferences(user_id=user_id)
    except Exception:
        return []


def build_preference_block(preferences: list[dict[str, Any]]) -> str:
    """常驻偏好 → 注入块文本。"""
    try:
        return R.build_preference_block(preferences)
    except Exception:
        return ""


def recall_site_experience(query: str, url: str = "",
                           user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """recall 工具:按需召回当前站点的经验+教训。异常降级为空。"""
    try:
        return R.recall_site_memories(query, domain=extract_domain(url), user_id=user_id)
    except Exception:
        return []


def build_recall_block(memories: list[dict[str, Any]]) -> str:
    """recall 结果 → 注入历史的文本块。"""
    try:
        return R.build_recall_block(memories)
    except Exception:
        return "(回忆失败)"


def count_site_memories(url: str = "", user_id: str = DEFAULT_USER_ID) -> int:
    """数某站点有几条 site_experience(启发式兜底:>0 则首轮强制 recall)。异常返回 0。"""
    try:
        domain = extract_domain(url)
        if not domain:
            return 0
        return V.count_memories(user_id=user_id, memory_type=MEMORY_TYPE_SITE_EXPERIENCE, domain=domain)
    except Exception:
        return 0


def write_after_task(task: str, trajectory: str, url: str = "",
                     success: bool = True,
                     user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    """任务结束:抽取并写入记忆。任何异常都吞掉(尽力而为)。

    success=True → 成功任务(偏好/站点经验);False → 失败任务(教训)。
    domain 从 url 提取。返回写入结果摘要(供日志)。
    """
    try:
        domain = extract_domain(url)
        result = W.write_memory(task, trajectory, domain=domain, success=success, user_id=user_id)
        # 写入后触发遗忘剪枝(容量护栏)
        try:
            if domain:
                V.prune_domain(domain, user_id=user_id)   # domain 记忆(站点经验/教训)
            # 成功任务可能写入 preference(global,domain 为空)→ 单独剪枝防堆积
            if success:
                V.prune_global_preferences(user_id=user_id)
        except Exception:
            pass
        return result
    except Exception:
        return {"facts": [], "applied": [], "skipped_hash": 0}


def reinforce_used_memories(memory_ids: list[str], *, success: bool,
                            user_id: str = DEFAULT_USER_ID) -> None:
    """任务收尾:对本轮 recall 用过的记忆按成败结算(升权/负强化)。异常吞掉。"""
    for mid in memory_ids or []:
        try:
            V.reinforce_memory(str(mid), success=success)
        except Exception:
            continue


# ═══════════════════════════════════════════════════════════════════════════════
# chat 门面(分层:core 常驻 + episodic 按需;全部异常降级为空/无操作)
# ═══════════════════════════════════════════════════════════════════════════════

def get_core_memories(user_id: str = None) -> list[dict[str, Any]]:
    """取 chat 常驻记忆(persona + preference),供每轮注入。异常降级为空。"""
    from agent.memory.config import CHAT_USER_ID
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


def recall_episodic(query: str, user_id: str = None) -> list[dict[str, Any]]:
    """chat 事件记忆按需召回(过双相关性闸门)。异常降级为空。"""
    from agent.memory.config import CHAT_USER_ID
    try:
        return R.recall_episodic_memories(query, user_id=user_id or CHAT_USER_ID)
    except Exception:
        return []


def build_episodic_block(memories: list[dict[str, Any]]) -> str:
    """chat 事件召回 → 注入块文本(空则空串,不注入)。"""
    try:
        return R.build_episodic_block(memories)
    except Exception:
        return ""


def write_chat_memory(user_msg: str, assistant_msg: str,
                      history_summary: str = "", user_id: str = None) -> dict[str, Any]:
    """chat 对话抽取并写入记忆(persona/preference/episodic)。任何异常都吞掉。"""
    from agent.memory.config import CHAT_USER_ID
    try:
        result = W.write_chat_memory(
            user_msg, assistant_msg, history_summary=history_summary,
            user_id=user_id or CHAT_USER_ID)
        # 写后剪枝 core 层(persona/preference 属 global,防无限堆积)
        try:
            V.prune_global_preferences(user_id=user_id or CHAT_USER_ID)
        except Exception:
            pass
        return result
    except Exception:
        return {"facts": [], "applied": [], "skipped_hash": 0}

