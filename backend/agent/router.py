"""Agent 任务路由模块。

判断用户请求是否需要 Agent 自动化，以及操作是否需要用户确认。
"""

from agent.state import PageAction


AGENT_KEYWORDS: list[str] = [
    "点击", "点一下", "按一下", "click",
    "输入", "填写", "填入", "type", "fill",
    "提交", "submit",
    "滚动", "scroll", "翻页",
    "选择", "select", "选中",
    "导航", "navigate", "打开", "跳转",
    "拖拽", "drag",
    "悬停", "hover",
    "勾选", "取消勾选", "check", "uncheck", "toggle",
    "按下", "press", "回车",
    "帮我操作", "自动", "自动化",
]

DANGEROUS_KEYWORDS: list[str] = [
    "submit", "提交", "delete", "删除", "remove", "移除",
    "login", "登录", "signup", "注册", "register",
    "confirm", "确认", "pay", "支付", "purchase", "购买",
]


def classify_task(task: str) -> str:
    """判断用户输入应走 agent 还是普通 chat。

    Returns: "agent" | "chat"
    """
    task_lower = task.lower()
    for keyword in AGENT_KEYWORDS:
        if keyword in task_lower:
            return "agent"
    return "chat"


def should_confirm_action(
    action: PageAction, require_confirmation: list[str]
) -> bool:
    """判断某个动作是否需要用户确认后才执行。"""
    if action.type in require_confirmation:
        return True

    if action.type == "click" and action.locator:
        locator_text = action.locator.value.lower()
        for keyword in DANGEROUS_KEYWORDS:
            if keyword in locator_text:
                if "submit" in require_confirmation or "navigate" in require_confirmation:
                    return True

    if action.type == "press_key":
        key = action.params.get("key", "").lower()
        if key == "enter" and "submit" in require_confirmation:
            return True

    return False
