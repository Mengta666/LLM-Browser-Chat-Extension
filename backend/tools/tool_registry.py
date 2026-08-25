"""Agent 动作类型白名单。

结构化输出架构下，LLM 直接返回 {action:{type, index, ...}} JSON，不再用 OpenAI
function-calling schema。本模块只保留动作类型白名单供 loop 校验。动作的字段格式在
context_builder 的 system prompt 里说明。
"""

# LLM 可输出的页面动作类型。由前端执行 DOM 操作。task_complete 单独处理（终止）。
ALLOWED_ACTION_TYPES: set[str] = {
    "click", "type", "select", "scroll", "scroll_to_element", "hover",
    "focus", "clear", "press_key", "wait", "navigate",
}

