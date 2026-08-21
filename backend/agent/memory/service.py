"""记忆子系统对外门面。

agent 只跟这两个函数打交道:
- retrieve_for_task:任务开始时,按任务描述+当前URL 检索相关记忆(注入用)。
- write_after_task:任务成功完成后,后台从执行轨迹抽取并写入记忆。

两个函数都对异常宽容:记忆是"锦上添花",绝不能因为它出错而拖垮 agent 主流程。
"""

from typing import Any

from agent.memory import retriever as R
from agent.memory import writer as W
from agent.memory.retriever import extract_domain
from agent.memory.config import DEFAULT_USER_ID


def retrieve_for_task(task: str, url: str = "",
                      user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """任务开始:检索相关记忆。任何异常都降级为空列表。"""
    try:
        return R.retrieve_for_task(task, url=url, user_id=user_id)
    except Exception:
        return []


def build_memory_block(memories: list[dict[str, Any]]) -> str:
    """把检索结果组装成注入 prompt 的文本块。"""
    try:
        return R.build_memory_block(memories)
    except Exception:
        return ""


def write_after_task(task: str, trajectory: str, url: str = "",
                     user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    """任务成功完成:抽取并写入记忆。任何异常都吞掉(尽力而为)。

    返回写入结果摘要(供日志)。domain 从 url 提取。
    """
    try:
        domain = extract_domain(url)
        return W.write_memory(task, trajectory, domain=domain, user_id=user_id)
    except Exception:
        return {"facts": [], "applied": [], "skipped_hash": 0}
