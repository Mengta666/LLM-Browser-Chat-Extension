"""Agent 上下文构建模块。

负责将用户任务、页面观察状态、历史动作组装成 LLM 可理解的 messages 上下文。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent.state import PageState, ActionResult, PageAction, SubTask

if TYPE_CHECKING:
    from agent.state import AgentSession


SYSTEM_PROMPT = """你是一个浏览器自动化助手。用户会给你一个任务，你需要通过操作页面元素来完成它。

## 你的能力
你可以使用以下工具操作页面：
- click: 点击元素（按钮、链接、复选框等）
- type: 在输入框中输入文字（支持普通输入框和富文本编辑器）
- select: 从下拉框选择选项（支持原生select和自定义下拉组件）
- scroll: 滚动页面或容器（up/down/left/right）
- scroll_to_element: 直接滚动到指定元素使其出现在视口中
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

## 核心操作原则

### 1. 先观察，后行动
- 每次操作前，仔细阅读当前元素列表和页面状态
- 只操作你在当前观察中确实看到的元素
- 如果目标不在列表中，先通过 scroll 让它出现

### 2. 操作后必须等待和确认
- 任何点击/操作后，等我返回新的页面状态
- 通过对比前后状态确认操作是否生效
- 如果页面没有变化，可能需要换一种方式重试

### 3. 验证上下文再操作
- 当面板/弹出层/列表打开后，先确认显示的内容是否匹配目标
- 如果当前显示的范围/分类/页码不对，先导航到正确的上下文
- 例如：面板显示的月份不对就先翻月，列表显示的分类不对就先切换分类
- 不要仅凭某个值看起来像就点击——确保周围上下文也是对的

### 3. 修改前先清除
- 需要更改一个已有值时，先清除旧值再设置新值
- 观察当前已选中/已填写的内容，判断是否需要先清除
- 常见清除方式：点击元素旁的关闭图标(×)、点击"重置"按钮、使用 clear 操作、取消勾选

### 4. 复杂交互是多步的
- 很多组件需要"触发→弹出→选择"的多步流程
- 第一步：点击触发器打开弹出层/面板
- 第二步：等待弹出层出现（wait）
- 第三步：在弹出层中找到并点击目标选项
- 如果直接操作无效，思考是否缺少某个中间步骤

### 5. 滚动查找
- 目标不在可见列表中时，使用 scroll 而非猜测
- 滚动后等待新观察结果再决策
- 观察滚动进度信息，只有到底部后才能判定目标不存在
- 对弹窗/面板内的列表同样适用

### 6. 分步执行，不要跳跃
- 每次只执行一个操作
- 多层级导航逐步点击，不要跳步
- annotation_id 每次操作后可能变化，只用当前轮次的

### 7. 任务理解
- "查看/列出/有哪些" → 信息收集：只需滚动阅读，不修改页面，最后汇总
- "筛选/搜索/查询" → 修改条件：先清除旧条件，设置新条件，提交查询
- "点击/进入/打开" → 导航操作：逐步点击进入目标页面
- "填写/输入/提交" → 表单操作：定位输入框，输入内容，提交

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


TEXT_MODE_FORMAT_APPENDIX = """
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

## 附加规则
- 每次只输出一个 JSON 动作
- "thought" 字段简短说明你的推理
- 不要在 JSON 之外添加任何文字
"""

SYSTEM_PROMPT_TEXT_MODE = SYSTEM_PROMPT + TEXT_MODE_FORMAT_APPENDIX


PLANNING_PROMPT = """你是一个浏览器自动化规划助手。用户给你一个目标任务，你需要结合当前页面的实际元素和状态，将目标拆解为有序的子任务列表。

## 规则
1. 每个子任务必须是一个可以在当前页面（或操作后的页面）上直接执行的具体动作目标
2. 子任务顺序要合理：先清除/准备 → 再设置条件 → 最后提交/确认
3. 每个子任务用一句话描述，要具体明确（"选择日期范围为本月" 而非 "设置日期"）
4. 通常 2-7 个子任务，不要过细也不要过粗
5. 如果任务本身很简单（单步可完成的操作），返回 1 个子任务即可
6. 只基于当前页面实际可见的元素来规划，不要假设不存在的功能

## 输出格式
只输出 JSON，不要其他文字：
```json
{"sub_tasks": ["子任务1描述", "子任务2描述", ...]}
```"""


def build_sub_task_context(
    task: str, sub_tasks: list[SubTask], current_index: int
) -> str:
    """构建子任务进度上下文，注入到每轮的 system prompt 中。"""
    if not sub_tasks:
        return ""

    total = len(sub_tasks)
    current = current_index + 1
    current_task = sub_tasks[current_index]
    parts = [
        f"\n## 任务执行计划",
        f"总目标: {task}",
        f"当前子任务 [{current}/{total}]: {current_task.description}",
    ]

    # 回退重试提示
    if current_task.retry_count > 0 and current_task.retry_reason:
        parts.append(f"\n⚠️ 这是第 {current_task.retry_count} 次重试此子任务")
        parts.append(f"上次失败原因: {current_task.retry_reason}")
        parts.append("请用不同的方式完成，避免重复上次的错误。")

    completed = [st for st in sub_tasks[:current_index] if st.status == "completed"]
    if completed:
        parts.append("已完成: " + ", ".join(f"✓ {st.description}" for st in completed))

    skipped = [st for st in sub_tasks if st.status == "skipped"]
    if skipped:
        parts.append("已跳过: " + ", ".join(f"⊘ {st.description}" for st in skipped))

    remaining = sub_tasks[current_index + 1:]
    if remaining:
        parts.append("待完成: " + ", ".join(st.description for st in remaining))

    parts.append("\n请专注完成当前子任务。完成后调用 sub_task_complete 切换到下一个。")
    parts.append("如果发现之前的子任务没做对导致当前无法继续，调用 sub_task_retry 回退重试。")
    return "\n".join(parts)


def build_planning_messages(task: str, page_state: PageState) -> list[dict[str, str]]:
    """构造规划阶段的 messages（用于任务分解）。"""
    return [
        {"role": "system", "content": PLANNING_PROMPT},
        {
            "role": "user",
            "content": f"## 目标任务\n{task}\n\n{build_observation_message(page_state)}",
        },
    ]


def build_reflection_prompt(session: "AgentSession") -> str:
    """当检测到重复失败时，生成反思提示注入到对话中。

    返回空字符串表示无需反思。
    """
    if not session.blacklisted_approaches and len(session.failed_attempts) < 3:
        return ""

    parts = []

    # 有黑名单时注入禁止重复
    if session.blacklisted_approaches:
        parts.append("## ⚠️ 反思提醒")
        parts.append("以下方式已多次尝试失败，**禁止再使用相同方式**，必须换一种不同的策略：")
        for approach in session.blacklisted_approaches[-5:]:
            parts.append(f"  ✗ {approach}")

    # 连续 3 步失败时给出更强的方向性引导
    recent_fails = [
        a for a in session.failed_attempts[-3:]
        if a.step >= session.current_step - 3
    ]
    if len(recent_fails) >= 3:
        parts.append("")
        parts.append("你已连续多次操作失败，请退一步重新思考：")
        parts.append("- 目标元素是否真的在当前视口内？尝试 scroll 查看更多内容")
        parts.append("- 是否需要先完成前置操作（如打开面板、切换标签页、关闭当前弹窗）？")
        parts.append("- 尝试完全不同的交互路径（不同的按钮、不同的菜单入口）")
        parts.append("- 用 text 定位替代 css，或反过来")
        parts.append("- 如果确认目标无法完成，调用 task_complete 报告失败原因")

    return "\n".join(parts) if parts else ""


def build_system_prompt(sub_task_context: str = "") -> str:
    return SYSTEM_PROMPT + sub_task_context


def build_system_prompt_text_mode(sub_task_context: str = "") -> str:
    return SYSTEM_PROMPT + sub_task_context + TEXT_MODE_FORMAT_APPENDIX


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

    if page_state.is_loading:
        parts.append("\n⏳ 页面正在加载中，建议使用 wait 等待加载完成后再操作。")

    if page_state.focused_element:
        parts.append(f"当前焦点: {page_state.focused_element}")

    # 弹出层信息
    active_popup = getattr(page_state, 'active_popup', None)
    if not active_popup and hasattr(page_state, '__dict__'):
        active_popup = page_state.__dict__.get('active_popup')
    if isinstance(active_popup, dict) and active_popup:
        popup_type_map = {
            'date_picker': '日期选择面板',
            'dropdown': '下拉选择列表',
            'modal': '弹窗对话框',
            'menu': '菜单',
            'popup': '弹出面板',
        }
        popup_label = popup_type_map.get(active_popup.get('type', ''), '弹出面板')
        header = active_popup.get('header_text', '')
        header_str = f" ({header})" if header else ""
        parts.append(f"\n📍 当前活跃弹出层: {popup_label}{header_str}")
        parts.append("   请优先在此弹出层内操作。操作完成后面板通常会自动关闭。")

    if page_state.interactive_elements:
        popup_els = [el for el in page_state.interactive_elements if el.get("in_popup")]
        main_els = [el for el in page_state.interactive_elements if not el.get("in_popup")]

        if popup_els:
            parts.append(f"\n## 🔍 弹出面板内的元素（共{len(popup_els)}个，优先操作）")
            parts.extend(_group_and_format_elements(popup_els))

            if main_els:
                in_vp, out_vp = _split_by_viewport(main_els, page_state.viewport)
                if in_vp:
                    parts.append(f"\n## 主页面元素 - 视口内（共{len(in_vp)}个）")
                    parts.extend(_group_and_format_elements(in_vp))
                if out_vp:
                    parts.append(f"\n## 主页面元素 - 视口外（共{len(out_vp)}个，需scroll）")
                    # 视口外只展示前10个，节省 token
                    parts.extend(_group_and_format_elements(out_vp[:10]))
                    if len(out_vp) > 10:
                        parts.append(f"  ... 还有 {len(out_vp) - 10} 个元素未显示，请 scroll 查看")
        else:
            in_vp, out_vp = _split_by_viewport(page_state.interactive_elements, page_state.viewport)
            if in_vp:
                parts.append(f"\n## 可交互元素 - 视口内（共{len(in_vp)}个）")
                parts.extend(_group_and_format_elements(in_vp))
            if out_vp:
                parts.append(f"\n## 可交互元素 - 视口外（共{len(out_vp)}个，需scroll）")
                parts.extend(_group_and_format_elements(out_vp[:10]))
                if len(out_vp) > 10:
                    parts.append(f"  ... 还有 {len(out_vp) - 10} 个元素未显示，请 scroll 查看")

        if getattr(page_state, 'element_count_truncated', False):
            parts.append("  ⚠️ 元素列表已截断，页面上还有更多元素。如果找不到目标，请先 scroll 让它出现在视口内。")

    if page_state.forms:
        parts.append("\n## 表单")
        for form in page_state.forms:
            action = form.get("action", "?")
            method = form.get("method", "?").upper()
            fields = ", ".join(form.get("fields", []))
            parts.append(f"  [{method}] {action} — 字段: {fields}")

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

    # 状态变化提示
    changes = result.state_changes if result.state_changes else {}
    if changes.get("url_changed"):
        msg += "\n⚡ 页面URL已变化，你现在在新页面上。请重新观察当前元素。"
    if changes.get("popup_disappeared"):
        msg += "\n⚡ 弹出面板已关闭。如果还需操作面板内容，需要重新打开。"
    if changes.get("popup_appeared"):
        msg += "\n⚡ 弹出面板已出现，请优先操作面板内的元素。"

    return msg


def build_initial_messages(task: str, page_state: PageState, sub_task_context: str = "") -> list[dict[str, str]]:
    """构造 Agent 会话的初始 messages（tool_calls 模式）。"""
    return [
        {"role": "system", "content": build_system_prompt(sub_task_context)},
        {
            "role": "user",
            "content": f"## 任务\n{task}\n\n{build_observation_message(page_state)}",
        },
    ]


def build_initial_messages_text_mode(task: str, page_state: PageState, sub_task_context: str = "") -> list[dict[str, str]]:
    """构造 Agent 会话的初始 messages（text_parse 模式，无 function calling）。"""
    return [
        {"role": "system", "content": build_system_prompt_text_mode(sub_task_context)},
        {
            "role": "user",
            "content": f"## 任务\n{task}\n\n{build_observation_message(page_state)}",
        },
    ]


MAX_FULL_OBSERVATION_STEPS = 3


def append_step_messages(
    messages: list[dict[str, Any]],
    action: PageAction,
    result: ActionResult,
    new_page_state: PageState,
) -> list[dict[str, Any]]:
    """在已有 messages 基础上追加一轮动作执行结果 + 新观察。

    使用滑动窗口策略：只保留最近 MAX_FULL_OBSERVATION_STEPS 步的完整观察，
    更早的步骤压缩为一行摘要，减少 token 消耗和上下文污染。
    """
    _compress_old_observations(messages)

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


def _compress_old_observations(messages: list[dict[str, Any]]) -> None:
    """压缩旧的观察步骤，只保留最近 N 步的完整内容。"""
    observation_indices = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "user" and "## 动作执行结果" in msg.get("content", ""):
            observation_indices.append(i)

    if len(observation_indices) <= MAX_FULL_OBSERVATION_STEPS:
        return

    to_compress = observation_indices[:-MAX_FULL_OBSERVATION_STEPS]
    for idx in to_compress:
        content = messages[idx]["content"]
        lines = content.split("\n")
        result_line = ""
        for line in lines:
            if line.startswith("[") and ("✓" in line or "✗" in line):
                result_line = line.strip()
                break
        if not result_line:
            result_line = lines[1] if len(lines) > 1 else "已执行"
        messages[idx] = {"role": "user", "content": f"[历史步骤] {result_line}"}


def _format_element(el: dict[str, Any]) -> str:
    """精简格式化单个元素。css 只在有唯一标识时展示。"""
    eid = el.get("id", "?")
    tag = el.get("tag", "?")

    parts = []
    component = el.get("component", "")
    if component:
        parts.append(component)
    text = el.get("text", "")
    if text:
        parts.append(f'"{text[:40]}"')
    placeholder = el.get("placeholder", "")
    if placeholder:
        parts.append(f'placeholder="{placeholder[:30]}"')
    name = el.get("name", "")
    if name and not text:
        parts.append(f"name={name}")
    value = el.get("value", "")
    if value:
        parts.append(f'当前值="{value[:20]}"')
    etype = el.get("type", "")
    if etype and tag == "input":
        parts.append(etype)

    enabled = el.get("enabled", True)
    status = " [禁用]" if not enabled else ""

    selector = el.get("css_selector", "")
    css_part = ""
    if selector and (
        selector.startswith("#")
        or "[name=" in selector
        or "[data-testid=" in selector
    ):
        css_part = f" | css={selector}"

    desc = " ".join(parts)
    return f"  [{eid}] <{tag}> {desc}{css_part}{status}"


def _is_similar_element(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """判断两个元素是否可以归为一组。"""
    if a.get("tag") != b.get("tag"):
        return False
    if a.get("role") != b.get("role"):
        return False
    a_text = a.get("text", "").strip()
    b_text = b.get("text", "").strip()
    # 都是纯数字（日期格、分页等）
    if a_text.isdigit() and b_text.isdigit():
        return True
    # 文本长度接近且标签相同（如选项列表）
    if len(a_text) > 0 and len(b_text) > 0:
        if abs(len(a_text) - len(b_text)) <= 5 and a.get("tag") == b.get("tag"):
            # 排除文本完全不同的按钮（如"查询" vs "重置"）
            if a_text[:2] == b_text[:2]:
                return True
    return False


def _format_element_group(group: list[dict[str, Any]]) -> str:
    """将一组相似元素压缩为摘要行，保留 text→id 映射供定位。"""
    first_id = group[0].get("id", "?")
    last_id = group[-1].get("id", "?")
    tag = group[0].get("tag", "?")
    role = group[0].get("role", "")
    count = len(group)

    texts = [el.get("text", "").strip() for el in group]
    disabled = [el for el in group if not el.get("enabled", True)]

    role_str = f" {role}" if role else ""

    # 构建 text=[id] 映射
    if all(t.isdigit() for t in texts if t):
        # 数字序列：首3 + ... + 末3 + 禁用项
        mapping_parts = []
        disabled_ids = {el.get("id") for el in disabled}
        for i, el in enumerate(group):
            t = el.get("text", "").strip()
            eid = el.get("id", "?")
            if i < 3 or i >= len(group) - 3 or eid in disabled_ids:
                mapping_parts.append(f"{t}=[{eid}]")
            elif i == 3:
                mapping_parts.append("...")
        text_summary = " ".join(mapping_parts)
    elif count <= 8:
        text_summary = " ".join(
            f"{el.get('text', '').strip()[:10]}=[{el.get('id', '?')}]"
            for el in group
        )
    else:
        parts = []
        for el in group[:3]:
            parts.append(f"{el.get('text', '').strip()[:10]}=[{el.get('id', '?')}]")
        parts.append("...")
        for el in group[-3:]:
            parts.append(f"{el.get('text', '').strip()[:10]}=[{el.get('id', '?')}]")
        text_summary = " ".join(parts)

    status = ""
    if disabled:
        d_items = [
            f"{el.get('text', '').strip()}=[{el.get('id', '?')}]"
            for el in disabled[:3]
        ]
        status = f" (禁用: {', '.join(d_items)})"

    return f"  [{first_id}-{last_id}] <{tag}>{role_str} ×{count}: {text_summary}{status}"


def _group_and_format_elements(elements: list[dict[str, Any]]) -> list[str]:
    """对元素列表进行分组压缩，相似元素合并为摘要行。"""
    lines: list[str] = []
    i = 0
    while i < len(elements):
        el = elements[i]
        # 向后找连续相似元素
        group = [el]
        j = i + 1
        while j < len(elements):
            if _is_similar_element(el, elements[j]):
                group.append(elements[j])
                j += 1
            else:
                break

        if len(group) >= 3:
            lines.append(_format_element_group(group))
            i = j
        else:
            lines.append(_format_element(el))
            i += 1

    return lines


def _split_by_viewport(
    elements: list[dict[str, Any]], viewport: dict[str, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按视口拆分元素：视口内 + 视口外。"""
    vh = viewport.get("height", 900)
    vw = viewport.get("width", 1920)

    in_vp: list[dict[str, Any]] = []
    out_vp: list[dict[str, Any]] = []

    for el in elements:
        box = el.get("bounding_box", {})
        y = box.get("y", 0)
        h = box.get("height", 0)
        x = box.get("x", 0)
        w = box.get("width", 0)
        if y + h > 0 and y < vh and x + w > 0 and x < vw:
            in_vp.append(el)
        else:
            out_vp.append(el)

    return in_vp, out_vp
