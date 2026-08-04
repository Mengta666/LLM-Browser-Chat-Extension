"""工具注册表模块。

定义 Agent 自动化可用的所有页面操作工具的 OpenAI function calling schema。
"""

from typing import Any

LOCATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "定位页面元素的方式。method 可选 css(CSS选择器)、text(可见文本匹配)、annotation_id(元素编号)",
    "properties": {
        "method": {"type": "string", "enum": ["css", "text", "annotation_id"]},
        "value": {"type": "string", "description": "选择器值、文本内容或元素编号"},
        "fallback": {
            "type": "object",
            "description": "主定位失败时的备选方案",
            "properties": {
                "method": {"type": "string", "enum": ["css", "text", "annotation_id"]},
                "value": {"type": "string"},
            },
            "required": ["method", "value"],
        },
    },
    "required": ["method", "value"],
}

ACTION_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "点击页面上的元素（按钮、链接、复选框等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                },
                "required": ["locator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "在输入框或文本域中输入文字",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                    "text": {"type": "string", "description": "要输入的文本"},
                    "clear": {
                        "type": "boolean",
                        "description": "输入前是否清空已有内容，默认true",
                        "default": True,
                    },
                },
                "required": ["locator", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select",
            "description": "从下拉框中选择一个选项",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                    "value": {"type": "string", "description": "选项的value属性值"},
                    "option_text": {
                        "type": "string",
                        "description": "选项的可见文本（当value未知时用此字段）",
                    },
                },
                "required": ["locator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "滚动页面",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": "滚动方向",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "滚动像素数，默认300",
                        "default": 300,
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover",
            "description": "悬停在元素上（触发 tooltip 或下拉菜单）",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                },
                "required": ["locator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus",
            "description": "聚焦到某个元素（不点击）",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                },
                "required": ["locator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear",
            "description": "清空输入框或文本域的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                },
                "required": ["locator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "按下键盘按键（Enter、Escape、Tab、ArrowDown 等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": {
                        **LOCATOR_SCHEMA,
                        "description": "目标元素（可选，不填则作用于当前焦点元素）",
                    },
                    "key": {
                        "type": "string",
                        "description": "按键名称，如 Enter、Escape、Tab、ArrowDown",
                    },
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ctrl", "shift", "alt", "meta"]},
                        "description": "组合键修饰符",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "等待指定时间（用于页面加载或动态内容出现后）",
            "parameters": {
                "type": "object",
                "properties": {
                    "ms": {
                        "type": "integer",
                        "description": "等待毫秒数（最大5000）",
                        "default": 1000,
                        "maximum": 5000,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "导航到指定URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要导航到的完整URL",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_element",
            "description": "等待某个元素出现在页面上（轮询检测，超时返回失败）",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                    "timeout": {
                        "type": "integer",
                        "description": "最大等待时间毫秒数（默认5000，最大10000）",
                        "default": 5000,
                        "maximum": 10000,
                    },
                },
                "required": ["locator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll_to_element",
            "description": "滚动页面使指定元素出现在视口中央。当你知道元素存在但不在当前可见区域时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "locator": LOCATOR_SCHEMA,
                },
                "required": ["locator"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "标记任务已完成或无法完成。必须在任务结束时调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "任务完成情况的简要总结",
                    },
                    "success": {
                        "type": "boolean",
                        "description": "任务是否成功完成",
                    },
                },
                "required": ["summary", "success"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_complete",
            "description": "当前目标已完成。调用后系统会结合新页面状态拆解下一个目标。当你确认当前目标的所有步骤已经达成时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "当前目标完成情况的简要说明",
                    },
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_retry",
            "description": "回退到之前的某个目标重新执行。当你发现前面的操作导致当前目标无法继续时调用。系统会携带错误原因在新页面上重新拆解该目标。最多回退2次。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_index": {
                        "type": "integer",
                        "description": "要回退到的目标编号（从0开始）",
                    },
                    "reason": {
                        "type": "string",
                        "description": "回退原因：之前的目标哪里做错了，这次应该怎么改",
                    },
                },
                "required": ["target_index", "reason"],
            },
        },
    },
]

ALLOWED_ACTION_TYPES: set[str] = {
    "click", "type", "select", "scroll", "scroll_to_element", "hover",
    "focus", "clear", "press_key", "wait", "navigate",
    "wait_for_element", "task_complete", "goal_complete", "goal_retry",
}
