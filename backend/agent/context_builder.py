"""Agent 上下文构建模块。

负责将用户任务、页面观察状态、历史动作组装成 LLM 可理解的 messages 上下文。
"""

from typing import Any

from agent.state import PageState, ActionResult, PageAction


SYSTEM_PROMPT = """你是一个浏览器自动化助手。用户会给你一个任务，你需要通过操作页面元素来完成它。

## 你的能力
你可以使用以下工具操作页面：
- click: 点击元素（按钮、链接、复选框等）
- type: 在输入框中输入文字（支持普通输入框和富文本编辑器）
- select: 从下拉框选择选项（支持原生select和自定义下拉组件）
- scroll: 滚动页面或容器（up/down/left/right）
- hover: 悬停在元素上（触发tooltip或下拉菜单）
- focus: 聚焦元素
- clear: 清空输入框（支持contenteditable）
- press_key: 按键盘按键（Enter、Tab、Escape等）
- navigate: 导航到指定URL
- wait: 等待一段时间
- wait_for_element: 等待某个元素出现在页面上
- task_complete: 任务完成时必须调用

## 元素定位方式
你有三种方式定位元素：
1. **css**: CSS选择器（如 "#username", "button.submit", "input[name='email']"）—— 最精确
2. **text**: 元素可见文本匹配（如 "登录", "Submit"）—— 最直观，会搜索所有可见元素
3. **annotation_id**: 页面观察中元素的编号ID（如 "3"）—— 直接引用观察结果中的编号

建议：优先使用 css（有明确 id/class 时），其次用 text（按钮文字明确时），annotation_id 作为补充。
务必提供 fallback 定位，提高操作成功率。

## 工作流程
1. 观察当前页面状态（我会提供可交互元素列表）
2. 分析哪个元素需要操作
3. 调用一个工具执行操作
4. 等待我返回新的页面状态（每次操作后你都会收到最新的元素列表）
5. 根据新的页面状态决定下一步，重复直到任务完成，然后调用 task_complete

## 重要策略

### 滚动查找策略
当你需要找的元素不在当前可见的元素列表中时：
1. 使用 scroll(direction="down", amount=500) 滚动页面
2. **滚动后不要急于操作**——等我返回新的页面状态，你会看到新出现的元素
3. 在新的元素列表中查找目标，如果还没找到，**继续滚动**
4. **不要猜测 annotation_id**——每次滚动后元素编号会变化，必须以最新观察为准
5. 观察"滚动进度"信息：如果显示"可继续向下滚动"，说明页面还有内容没显示出来
6. **只有到达底部（进度≥95%）且确实找不到目标时，才能判定目标不存在**
7. 对于弹窗/对话框中的列表，同样需要多次滚动直到底部

### 信息收集类任务策略
当任务是"查看"、"看一下"、"有哪些"、"列出"、"找到所有"时：
1. 这类任务需要你**浏览完整个列表/页面**后汇总信息
2. 必须反复滚动直到**到达底部**，中途看到的所有内容都要记录
3. **不要点击不相关的按钮**——只需要滚动和阅读即可
4. 每次滚动后，从元素列表和页面文本摘要中提取相关信息
5. 只有浏览完全部内容后，才调用 task_complete 并在 summary 中列出完整结果
6. **不要使用 press_key (PageDown) 来翻页**——使用 scroll 操作
7. 不要尝试勾选、点击或修改任何内容，除非任务明确要求

### 分步导航策略
当任务涉及多层级导航（如菜单→子菜单→具体内容）：
1. 每次只点击当前可见的下一层入口
2. 点击后等待新页面状态，确认导航成功
3. 再寻找下一层的入口
4. **不要跳步**——如果你还没看到目标菜单项，可能需要先滚动或展开

### annotation_id 使用规则
- annotation_id 只在当前这一轮观察中有效
- 每次执行操作后，页面可能变化导致编号重新分配
- **只引用你在本轮观察中确实看到的编号**，不要猜测
- 如果不确定编号对应什么元素，用 css 或 text 定位更安全

## 注意事项
- 每次只执行一个操作
- 如果目标元素不可见，先 scroll 再在新的观察结果中查找
- 如果操作失败，尝试用不同的定位方式（css→text→annotation_id）
- 如果元素列表显示"已截断"，需要 scroll 来发现更多元素
- 表单填写后通常需要点击提交按钮
- 页面跳转后使用 wait 或 wait_for_element 等待加载
- 不要使用 press_key 来搜索页面内容（浏览器搜索框不可控）
- 任务完成或确认无法完成时，必须调用 task_complete
"""


SYSTEM_PROMPT_TEXT_MODE = """你是一个浏览器自动化助手。用户会给你一个任务，你需要通过操作页面元素来完成它。

## 你的能力
你可以执行以下操作：
- click: 点击元素（按钮、链接、复选框等）
- type: 在输入框中输入文字
- select: 从下拉框选择选项
- scroll: 滚动页面（up/down/left/right）
- hover: 悬停在元素上
- focus: 聚焦元素
- clear: 清空输入框
- press_key: 按键盘按键（Enter、Tab、Escape等）
- wait: 等待一段时间（页面加载后使用）
- task_complete: 任务完成时必须调用

## 元素定位方式
你有三种方式定位元素：
1. **css**: CSS选择器（如 "#username", "button.submit"）—— 最精确
2. **text**: 元素可见文本匹配（如 "登录", "Submit"）—— 最直观
3. **annotation_id**: 页面观察中元素的编号ID（如 "3"）—— 直接引用观察结果

## 输出格式（非常重要！）
你必须且只能输出一个 JSON 对象，不要输出任何其他文字说明。格式如下：

点击元素：
```json
{"action": "click", "locator": {"method": "text", "value": "按钮文字"}, "thought": "你的思考"}
```

输入文字：
```json
{"action": "type", "locator": {"method": "css", "value": "#input-id"}, "text": "要输入的文字", "thought": "你的思考"}
```

下拉选择：
```json
{"action": "select", "locator": {"method": "css", "value": "select#city"}, "option_text": "北京", "thought": "你的思考"}
```

滚动页面：
```json
{"action": "scroll", "direction": "down", "amount": 300, "thought": "你的思考"}
```

按键：
```json
{"action": "press_key", "key": "Enter", "thought": "你的思考"}
```

等待：
```json
{"action": "wait", "ms": 1000, "thought": "你的思考"}
```

导航到URL：
```json
{"action": "navigate", "url": "https://example.com", "thought": "你的思考"}
```

等待元素出现：
```json
{"action": "wait_for_element", "locator": {"method": "css", "value": "#result"}, "timeout": 5000, "thought": "你的思考"}
```

任务完成：
```json
{"action": "task_complete", "summary": "完成了什么", "thought": "你的思考"}
```

## 规则
- 每次只输出一个 JSON 动作
- "thought" 字段用于简短说明你的推理
- 不要在 JSON 之外添加任何文字
- 如果操作失败，尝试用不同的定位方式
- scroll 后不要猜测元素位置，等待新的页面观察结果
- 只使用你在当前观察中确实看到的 annotation_id，不要猜测编号
- 不要使用 press_key 来搜索或翻页（用 scroll 代替 PageDown）
- 分步导航：每次只点一层，等新页面状态确认后再进入下一层
- **信息收集任务**（查看/列出/找到所有）：只需要反复 scroll 浏览到底部，然后在 task_complete 的 summary 中汇总所有看到的信息。不要点击或修改任何内容。
- 任务完成或确认无法完成时，必须输出 task_complete
"""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT


def build_observation_message(page_state: PageState) -> str:
    """将页面状态格式化为 LLM 可阅读的观察信息。"""
    parts = []

    parts.append(f"## 当前页面\nURL: {page_state.url}\n标题: {page_state.title}")

    viewport_h = page_state.viewport.get('height', 0)
    scroll_y = page_state.scroll_position.get('y', 0)
    doc_height = page_state.document_height
    scroll_pct = round(scroll_y / max(doc_height - viewport_h, 1) * 100) if doc_height > viewport_h else 100
    at_bottom = scroll_pct >= 95

    parts.append(
        f"视口: {page_state.viewport.get('width', '?')}x{viewport_h} | "
        f"滚动位置: y={scroll_y} | "
        f"页面总高度: {doc_height} | "
        f"滚动进度: {scroll_pct}%{'（已到底部）' if at_bottom else '（可继续向下滚动）'}"
    )

    # 弹窗/容器内的滚动状态
    container_info = getattr(page_state, 'scrollable_container', None)
    if not container_info and hasattr(page_state, '__dict__'):
        container_info = page_state.__dict__.get('scrollable_container')
    if isinstance(container_info, dict) and container_info:
        c_top = container_info.get('scroll_top', 0)
        c_total = container_info.get('scroll_height', 0)
        c_visible = container_info.get('client_height', 0)
        c_at_bottom = container_info.get('at_bottom', False)
        c_pct = round(c_top / max(c_total - c_visible, 1) * 100) if c_total > c_visible else 100
        parts.append(
            f"📦 弹窗/容器滚动: 位置={c_top}px, 总高={c_total}px, 可见={c_visible}px, "
            f"进度={c_pct}%{'（已到底部）' if c_at_bottom else '（可继续向下滚动）'}"
        )

    if page_state.focused_element:
        parts.append(f"当前焦点: {page_state.focused_element}")

    if page_state.interactive_elements:
        parts.append("\n## 可交互元素列表")
        for el in page_state.interactive_elements:
            line = _format_element(el)
            parts.append(line)
        if getattr(page_state, 'element_count_truncated', False):
            parts.append("  ⚠️ 元素列表已截断，页面上还有更多元素。如果找不到目标，请先 scroll 让它出现在视口内。")

    if page_state.forms:
        parts.append("\n## 表单")
        for form in page_state.forms:
            action = form.get("action", "?")
            method = form.get("method", "?").upper()
            fields = ", ".join(form.get("fields", []))
            parts.append(f"  [{method}] {action} — 字段: {fields}")

    if page_state.text_content_summary:
        summary = page_state.text_content_summary[:2000]
        parts.append(f"\n## 页面文本摘要\n{summary}")

    return "\n".join(parts)


def build_action_result_message(action: PageAction, result: ActionResult) -> str:
    """格式化动作执行结果。"""
    status = "✓ 成功" if result.success else "✗ 失败"
    msg = f"[{status}] {action.type}"
    if action.locator:
        msg += f" → {action.locator.method}:{action.locator.value}"
    if result.details:
        msg += f" | {result.details}"
    if result.error:
        msg += f" | 错误: {result.error}"
    return msg


def build_initial_messages(task: str, page_state: PageState) -> list[dict[str, str]]:
    """构造 Agent 会话的初始 messages（tool_calls 模式）。"""
    return [
        {"role": "system", "content": build_system_prompt()},
        {
            "role": "user",
            "content": f"## 任务\n{task}\n\n{build_observation_message(page_state)}",
        },
    ]


def build_initial_messages_text_mode(task: str, page_state: PageState) -> list[dict[str, str]]:
    """构造 Agent 会话的初始 messages（text_parse 模式，无 function calling）。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT_TEXT_MODE},
        {
            "role": "user",
            "content": f"## 任务\n{task}\n\n{build_observation_message(page_state)}",
        },
    ]


def append_step_messages(
    messages: list[dict[str, Any]],
    action: PageAction,
    result: ActionResult,
    new_page_state: PageState,
) -> list[dict[str, Any]]:
    """在已有 messages 基础上追加一轮动作执行结果 + 新观察。"""
    messages.append(
        {
            "role": "user",
            "content": (
                f"## 动作执行结果\n{build_action_result_message(action, result)}\n\n"
                f"{build_observation_message(new_page_state)}"
            ),
        }
    )
    return messages


def _format_element(el: dict[str, Any]) -> str:
    """格式化单个元素为可读字符串。"""
    eid = el.get("id", "?")
    tag = el.get("tag", "?")
    etype = el.get("type", "")
    text = el.get("text", "")
    name = el.get("name", "")
    placeholder = el.get("placeholder", "")
    aria = el.get("aria_label", "")
    selector = el.get("css_selector", "")
    value = el.get("value", "")
    enabled = "启用" if el.get("enabled", True) else "禁用"

    desc_parts = []
    if text:
        desc_parts.append(f'文本="{text[:50]}"')
    if name:
        desc_parts.append(f"name={name}")
    if placeholder:
        desc_parts.append(f'placeholder="{placeholder}"')
    if aria:
        desc_parts.append(f'aria="{aria}"')
    if value:
        desc_parts.append(f'value="{value[:30]}"')
    if etype:
        desc_parts.append(f"type={etype}")

    desc = ", ".join(desc_parts)
    return f"  [{eid}] <{tag}> {desc} | css={selector} | {enabled}"
