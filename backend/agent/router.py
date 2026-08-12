"""Agent 任务路由模块。

判断某个动作是否需要用户确认后才执行。
索引直连架构下，动作不再带文本 locator，危险判定按动作类型 + require_confirmation 配置。
"""

from agent.state import PageAction


def should_confirm_action(action: PageAction, require_confirmation: list[str]) -> bool:
    """判断某个动作是否需要用户确认。require_confirmation 里列出的动作类型需要确认。"""
    if action.type in require_confirmation:
        return True
    if action.type == "press_key":
        key = (action.params.get("key", "") or "").lower()
        if key == "enter" and "submit" in require_confirmation:
            return True
    return False
