"""工具注册表模块。

定义 Agent 自动化可用的所有页面操作工具的 OpenAI function calling schema。
定位契约：索引直连——LLM 只给元素编号 index（对应观察时打标的 data-agent-id），
前端直取该节点，无 css/text 模糊匹配。
"""

from typing import Any

# 索引定位：index = 观察列表里的元素编号（data-agent-id）。这是唯一定位方式。
INDEX_PARAM: dict[str, Any] = {
    "type": "integer",
    "description": "目标元素的编号（观察列表中每个元素前的 [N]）。只能使用当前这一轮观察里列出的编号。",
}

ACTION_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "点击页面上的元素（按钮、链接、复选框、下拉/筛选选项等）。用观察列表里的编号定位。",
            "parameters": {
                "type": "object",
                "properties": {"index": INDEX_PARAM},
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "在输入框或文本域中输入文字（支持普通输入框和富文本编辑器）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": INDEX_PARAM,
                    "text": {"type": "string", "description": "要输入的文本"},
                    "clear": {
                        "type": "boolean",
                        "description": "输入前是否清空已有内容，默认true",
                        "default": True,
                    },
                },
                "required": ["index", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select",
            "description": "从下拉框选择选项。若为自定义下拉，通常先 click 触发器展开、再 click 选项编号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": INDEX_PARAM,
                    "option_text": {
                        "type": "string",
                        "description": "要选择的选项可见文本",
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "滚动页面或容器。目标不在当前编号列表时用它找出更多元素。",
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
            "name": "scroll_to_element",
            "description": "滚动使指定编号的元素出现在视口中央。",
            "parameters": {
                "type": "object",
                "properties": {"index": INDEX_PARAM},
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover",
            "description": "悬停在元素上以展开悬浮菜单/下拉/tooltip。当任务需要的入口（某应用、菜单项）不在当前编号列表中时，它可能藏在悬浮菜单里——先 hover『更多应用』『菜单』等触发器，展开后目标会出现在下一次观察中。",
            "parameters": {
                "type": "object",
                "properties": {"index": INDEX_PARAM},
                "required": ["index"],
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
                "properties": {"index": INDEX_PARAM},
                "required": ["index"],
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
                "properties": {"index": INDEX_PARAM},
                "required": ["index"],
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
                    "index": {
                        **INDEX_PARAM,
                        "description": "目标元素编号（可选，不填则作用于当前焦点元素）",
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
                    "url": {"type": "string", "description": "要导航到的完整URL"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": (
                "维护任务计划清单（可选，仅复杂任务用）：\n"
                "- 简单任务（1-2步可完成）：不要用，直接执行动作。\n"
                "- 清晰的多步任务（约10步以上）：一开始就调用，列出 3-10 个步骤。\n"
                "- 任务不清晰：先探索几步了解情况，再调用规划。\n"
                "完成某步后再次调用更新其状态。"
                "重要：完成所有计划项 ≠ 任务完成，仍需确认最终目标已达成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "3-10 个任务步骤",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "步骤描述"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "current", "done", "skipped"],
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                    "current": {
                        "type": "integer",
                        "description": "当前进行的步骤序号（0-indexed）",
                    },
                },
                "required": ["items"],
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
]

ALLOWED_ACTION_TYPES: set[str] = {
    "click", "type", "select", "scroll", "scroll_to_element", "hover",
    "focus", "clear", "press_key", "wait", "navigate", "update_plan", "task_complete",
}
