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
        return W.write_memory(task, trajectory, domain=domain, success=success, user_id=user_id)
    except Exception:
        return {"facts": [], "applied": [], "skipped_hash": 0}
