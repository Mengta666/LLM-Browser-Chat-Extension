"""Agent 上下文构建模块。

负责将用户任务、页面观察状态、历史动作组装成 LLM 可理解的 messages 上下文。
双次调用架构：每步先调规划 LLM，再调执行 LLM。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent.state import PageState, ActionResult, PageAction

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
- 不要仅凭某个值看起来像就点击——确保周围上下文也是对的

### 4. 修改前先清除
- 需要更改一个已有值时，先清除旧值再设置新值
- 常见清除方式：点击关闭图标(×)、点击"重置"按钮、使用 clear 操作

### 5. 复杂交互是多步的
- 很多组件需要"触发→弹出→选择"的多步流程
- 如果直接操作无效，思考是否缺少某个中间步骤

### 6. 滚动查找
- 目标不在可见列表中时，使用 scroll 而非猜测
- 观察滚动进度信息，只有到底部后才能判定目标不存在

### 7. 分步执行，不要跳跃
- 每次只执行一个操作
- annotation_id 每次操作后可能变化，只用当前轮次的

## 注意事项
- 每次只执行一个操作
- 如果操作失败，尝试用不同的定位方式（css→text→annotation_id）
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

滚动页面：
```json
{"action": "scroll", "direction": "down", "amount": 300, "thought": "你的思考"}
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


# ═══════════════════════════════════════════════════════════════════════════════
# 每步规划 Prompt（双次调用架构）
# ═══════════════════════════════════════════════════════════════════════════════

STEP_PLANNING_PROMPT = """你是一个浏览器自动化规划助手。根据用户的总任务和当前页面状态，判断目标进度并决定下一步方向。

## 第一步：指令-元素匹配（必须先做）
从用户任务描述中找出与当前页面可操作元素**直接对应**的关键词：
- 用户说"在XX里面"/"去XX"/"打开XX" → XX 很可能对应页面上某个按钮或链接
- 匹配时忽略大小写、中英文差异、空格差异
- 如果有匹配，第一步必须优先点击匹配的元素

## 第二步：基于匹配结果规划
- 如果匹配到了元素：current_goal 和 next_action_hint 必须围绕该元素展开
- 如果没有匹配：按正常逻辑自由规划
- **重要**：如果参考经验或任务明确提到某入口（如"Coding"），但当前元素列表里找不到它，不要反复点击/搜索去凑——它很可能藏在悬浮菜单里。此时用 hover 动作悬浮"更多应用/菜单"类触发器，展开后目标会出现在下一次观察中，再点击。绝不要在找不到入口时原地打转。

## 输出格式
只输出 JSON，不要其他文字：
```json
{
  "matched_elements": ["用户关键词 → 匹配的页面元素"],
  "task_done": false,
  "current_goal": "当前正在执行的目标（基于当前页面能做的事）",
  "next_action_hint": "下一步应该做什么操作（给执行器的提示）",
  "completed_goals": ["已完成的目标1", "已完成的目标2"],
  "remaining": "后续还需要做什么（一句话概括，如果没有则为空字符串）"
}
```

## 匹配示例
用户: "帮我在coding里面找y-eval" + 页面元素含"Coding"
→ matched_elements: ["coding → Coding"]
→ next_action_hint: "点击 Coding"

用户: "去需求管理看bug" + 页面元素含"需求管理"
→ matched_elements: ["需求管理 → 需求管理"]
→ next_action_hint: "点击需求管理"

## 规划规则
1. `current_goal` 必须是当前页面上可以直接操作的目标
2. `next_action_hint` 是对下一步操作的简短提示
3. `completed_goals` 是已经成功完成的目标列表（根据页面状态判断）
4. `remaining` 是还没做完的部分，一句话概括
5. 当整个任务完成时设置 `task_done: true`
6. 如果上一步操作失败了，next_action_hint 应该给出不同的策略

## 判断任务完成的标准
- "查看/查找/查询"类任务：页面上已经**显示了目标的详细信息**就算完成
  - "查看提交内容" → 必须看到 diff/代码变更，不仅仅是提交标题
  - "查找 X" → 必须进入 X 的详情页面
- "操作/填写/提交"类任务：必须看到操作结果确认
- 当你不确定是否完成时，倾向于"未完成"
"""


JUDGMENT_PROMPT = """你是一个浏览器自动化执行的评判助手。一个自动化任务在执行了多步后仍未完成当前目标，你需要分析失败原因。

## 输出格式
只输出 JSON，不要其他文字：
```json
{
  "failure_reason": "简要说明为什么这一轮没有完成目标（1-2句话，要具体）",
  "avoid": "下一轮应该避开的关键词/路径/操作（逗号分隔）"
}
```

## 规则
1. failure_reason 要具体——不是"没找到"，而是"通过XX路径进入了YY系统，该系统没有ZZ功能"
2. avoid 列出具体的菜单名、按钮名、URL 路径片段，让下一轮能明确避开
3. 不需要给出"正确方向"——下一轮会根据实际页面重新规划
"""


# ═══════════════════════════════════════════════════════════════════════════════
# 构建函数
# ═══════════════════════════════════════════════════════════════════════════════

def _build_knowledge_hint(session: "AgentSession", page_state: PageState) -> str:
    """查询知识库，返回参考经验注入文本。只在首步或目标切换时查询。"""
    # 只在首步查询（current_step <= 1），避免每步都调 embedding
    if session.current_step > 1:
        # 复用已缓存的召回结果（存在 session 上）
        cached = getattr(session, "_kb_hint_cache", None)
        return cached or ""

    fingerprint = page_state.page_fingerprint or {}
    if not fingerprint:
        return ""

    try:
        from rag.embedder import embed_text
        from knowledge.store import query_records

        vector = embed_text(session.task)
        results = query_records(vector, fingerprint)
    except Exception as exc:
        try:
            from observability.logger import get_logger
            get_logger("agent").warn("kb_recall_error", session_id=session.session_id,
                                     data={"error": str(exc)[:200]})
        except Exception:
            pass
        return ""

    if not results:
        session._kb_hint_cache = ""
        try:
            from observability.logger import get_logger
            get_logger("agent").info("kb_recall_miss", session_id=session.session_id,
                                     data={"task": session.task[:80]})
        except Exception:
            pass
        return ""

    score, rec = results[0]
    try:
        from observability.logger import get_logger
        get_logger("agent").info("kb_recall_hit", session_id=session.session_id,
                                 data={"score": round(score, 3), "record_id": rec.id,
                                       "matched_task": rec.task_description[:80]})
    except Exception:
        pass
    # 优先展示 intent（高层意图），无则回退到 action:target
    step_lines = []
    for i, s in enumerate(rec.steps[:8]):
        intent = s.get("intent", "").strip()
        if intent:
            desc = intent
        else:
            desc = f"{s.get('action', '?')}"
            if s.get("target_text"):
                desc += f" {s['target_text'][:12]}"
            if s.get("value"):
                desc += f" = {s['value']}"
            elif s.get("text"):
                desc += f" 输入'{s['text'][:15]}'"
        step_lines.append(f"  {i+1}. {desc}")
    lines = [
        f"\n## 📖 参考经验（用户确认的成功路径，相似度{score:.2f}）",
        f"相似任务: {rec.task_description}",
        "操作步骤:",
        *step_lines,
    ]
    if rec.user_note:
        lines.append(f"💡 用户提示: {rec.user_note}")
    lines.append("注意: 仅供参考，请根据当前页面实际元素灵活调整。")

    hint = "\n".join(lines)
    session._kb_hint_cache = hint
    # 记录被引用的 record_id，供后续回报使用
    session._kb_referenced_id = rec.id
    return hint


def build_step_planning_messages(
    session: "AgentSession", page_state: PageState
) -> list[dict[str, str]]:
    """构造每步规划的 messages。包含页面文本摘要供判断任务完成。"""
    parts = [f"## 用户任务\n{session.task}"]

    # 知识库参考经验（只在首步或目标切换时注入，避免每步查询）
    kb_hint = _build_knowledge_hint(session, page_state)
    if kb_hint:
        parts.append(kb_hint)

    if session.completed_goals:
        parts.append(f"\n## 已完成: {' → '.join(session.completed_goals)}")

    if session.current_goal:
        parts.append(f"\n## 当前目标: {session.current_goal}")

    if session.step_history:
        # 压缩轨迹格式：每步一行极短摘要
        recent = session.step_history[-5:]
        trail = []
        for s in recent:
            action = s.get("action", {})
            result = s.get("result", {})
            a_type = action.get("type", "?")
            target = (action.get("locator") or {}).get("value", "")[:12]
            if not target and action.get("params", {}).get("text"):
                target = action["params"]["text"][:10]
            status = "✓" if result.get("success", True) else "✗"
            url_changed = "→" if result.get("state_changes", {}).get("url_changed") else ""
            trail.append(f"{a_type}:{target}{status}{url_changed}")
        parts.append(f"\n## 轨迹: {' | '.join(trail)}")

    # 页面信息（精简：URL + 标题 + 去重压缩后的文本）
    parts.append(f"\n## 页面: {page_state.url}")
    if page_state.title:
        parts.append(f"标题: {page_state.title}")

    if page_state.text_content_summary:
        # 去重压缩：去掉连续重复词、多余空白，截取关键500字
        raw = page_state.text_content_summary
        words = raw.split()
        deduped = []
        prev = ""
        for w in words:
            if w != prev:
                deduped.append(w)
                prev = w
        compressed = " ".join(deduped)[:800]
        parts.append(f"\n## 页面文本（压缩）: {compressed}")

    # 元素摘要：去重，只取唯一文本的前15个
    if page_state.interactive_elements:
        seen_texts = set()
        key_elements = []
        for el in page_state.interactive_elements[:50]:
            text = el.get("text", "").strip()
            if text and len(text) <= 30 and text not in seen_texts:
                seen_texts.add(text)
                key_elements.append(text)
            elif el.get("placeholder") and el["placeholder"] not in seen_texts:
                seen_texts.add(el["placeholder"])
                key_elements.append(f'[{el["placeholder"][:20]}]')
            if len(key_elements) >= 15:
                break
        if key_elements:
            parts.append(f"\n## 可操作元素: {', '.join(key_elements)}")

    # 反思（压缩为一行）
    if session.blacklisted_approaches:
        parts.append(f"\n## ⚠️ 无效路径: {'; '.join(session.blacklisted_approaches[-3:])}")

    if session.failed_attempts:
        recent_fails = session.failed_attempts[-3:]
        fail_summary = "; ".join(f"{f.action_type}:{f.target[:10]}失败" for f in recent_fails)
        parts.append(f"\n## 最近失败: {fail_summary}")

    # 评判结论（压缩）
    if session.failed_paths:
        parts.append(f"\n## ⚠️ 重试第{session.goal_retry_count}次")
        for fp in session.failed_paths:
            parts.append(f"  原因:{fp.failure_reason[:50]} 避开:{fp.avoid}")
        parts.append("\n请基于当前页面元素选择一条与上述失败路径完全不同的方向。")

    return [
        {"role": "system", "content": STEP_PLANNING_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def build_execution_goal_context(session: "AgentSession") -> str:
    """构建注入到执行 LLM system prompt 中的目标上下文。"""
    if not session.current_goal:
        return ""

    parts = [f"\n## 当前目标\n{session.current_goal}"]

    if session.next_action_hint:
        parts.append(f"\n## 操作提示\n{session.next_action_hint}")

    if session.completed_goals:
        parts.append(f"\n已完成: {', '.join(session.completed_goals)}")

    if session.remaining_goal:
        parts.append(f"后续待做: {session.remaining_goal}")

    parts.append("\n请执行一个操作来推进当前目标。每次只执行一个操作。")
    return "\n".join(parts)


def build_reflection_prompt(session: "AgentSession") -> str:
    """当检测到重复失败时，生成反思提示。"""
    if not session.blacklisted_approaches and len(session.failed_attempts) < 3:
        return ""

    parts = []

    if session.blacklisted_approaches:
        parts.append("## ⚠️ 反思提醒")
        parts.append("以下方式已多次尝试失败，**禁止再使用**：")
        for approach in session.blacklisted_approaches[-5:]:
            parts.append(f"  ✗ {approach}")

    recent_fails = [
        a for a in session.failed_attempts[-3:]
        if a.step >= session.current_step - 3
    ]
    if len(recent_fails) >= 3:
        parts.append("")
        parts.append("连续多次失败，请换一种完全不同的方式。")

    return "\n".join(parts) if parts else ""


def build_system_prompt(goal_context: str = "") -> str:
    return SYSTEM_PROMPT + goal_context


def build_system_prompt_text_mode(goal_context: str = "") -> str:
    return SYSTEM_PROMPT + goal_context + TEXT_MODE_FORMAT_APPENDIX


def build_initial_messages(task: str, page_state: PageState, goal_context: str = "") -> list[dict[str, str]]:
    """构造执行 LLM 的初始 messages（tool_calls 模式）。"""
    return [
        {"role": "system", "content": build_system_prompt(goal_context)},
        {"role": "user", "content": f"## 任务\n{task}\n\n{build_observation_message(page_state)}"},
    ]


def build_initial_messages_text_mode(task: str, page_state: PageState, goal_context: str = "") -> list[dict[str, str]]:
    """构造执行 LLM 的初始 messages（text_parse 模式）。"""
    return [
        {"role": "system", "content": build_system_prompt_text_mode(goal_context)},
        {"role": "user", "content": f"## 任务\n{task}\n\n{build_observation_message(page_state)}"},
    ]


MAX_FULL_OBSERVATION_STEPS = 3


def append_step_messages(
    messages: list[dict[str, Any]],
    action: PageAction,
    result: ActionResult,
    new_page_state: PageState,
) -> list[dict[str, Any]]:
    """追加一轮动作结果 + 新观察。滑动窗口压缩旧步骤。"""
    _compress_old_observations(messages)
    messages.append({
        "role": "user",
        "content": (
            f"## 动作执行结果\n{build_action_result_message(action, result)}\n\n"
            f"{build_observation_message(new_page_state)}"
        ),
    })
    return messages


def _compress_old_observations(messages: list[dict[str, Any]]) -> None:
    """压缩旧的观察步骤，只保留最近 N 步完整内容。"""
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


# ═══════════════════════════════════════════════════════════════════════════════
# 页面观察格式化
# ═══════════════════════════════════════════════════════════════════════════════

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
        f"滚动位置: y={scroll_y} | 页面总高度: {doc_height} | "
        f"滚动进度: {scroll_pct}%{'（已到底部）' if at_bottom else '（可继续向下滚动）'}"
    )

    container_info = getattr(page_state, 'scrollable_container', None)
    if isinstance(container_info, dict) and container_info:
        c_top = container_info.get('scroll_top', 0)
        c_total = container_info.get('scroll_height', 0)
        c_visible = container_info.get('client_height', 0)
        c_at_bottom = container_info.get('at_bottom', False)
        c_pct = round(c_top / max(c_total - c_visible, 1) * 100) if c_total > c_visible else 100
        parts.append(
            f"📦 容器滚动: {c_pct}%{'（已到底部）' if c_at_bottom else '（可继续滚动）'}"
        )

    if page_state.is_loading:
        parts.append("\n⏳ 页面正在加载中，建议 wait 等待。")

    active_popup = getattr(page_state, 'active_popup', None)
    if isinstance(active_popup, dict) and active_popup:
        popup_type_map = {
            'date_picker': '日期选择面板', 'dropdown': '下拉列表',
            'modal': '弹窗', 'menu': '菜单', 'popup': '弹出面板',
        }
        popup_label = popup_type_map.get(active_popup.get('type', ''), '弹出面板')
        header = active_popup.get('header_text', '')
        header_str = f" ({header})" if header else ""
        parts.append(f"\n📍 活跃弹出层: {popup_label}{header_str}")
        parts.append("   请优先操作弹出层内元素。")

    if page_state.interactive_elements:
        popup_els = [el for el in page_state.interactive_elements if el.get("in_popup")]
        main_els = [el for el in page_state.interactive_elements if not el.get("in_popup")]

        if popup_els:
            parts.append(f"\n## 🔍 弹出面板内元素（共{len(popup_els)}个，优先操作）")
            parts.extend(_group_and_format_elements(popup_els))
            if main_els:
                in_vp, out_vp = _split_by_viewport(main_els, page_state.viewport)
                if in_vp:
                    parts.append(f"\n## 主页面 - 视口内（{len(in_vp)}个）")
                    parts.extend(_group_and_format_elements(in_vp))
                if out_vp:
                    parts.append(f"\n## 主页面 - 视口外（{len(out_vp)}个，需scroll）")
                    parts.extend(_group_and_format_elements(out_vp[:10]))
                    if len(out_vp) > 10:
                        parts.append(f"  ... 还有 {len(out_vp) - 10} 个未显示")
        else:
            in_vp, out_vp = _split_by_viewport(page_state.interactive_elements, page_state.viewport)
            if in_vp:
                parts.append(f"\n## 可交互元素 - 视口内（{len(in_vp)}个）")
                parts.extend(_group_and_format_elements(in_vp))
            if out_vp:
                parts.append(f"\n## 可交互元素 - 视口外（{len(out_vp)}个，需scroll）")
                parts.extend(_group_and_format_elements(out_vp[:10]))
                if len(out_vp) > 10:
                    parts.append(f"  ... 还有 {len(out_vp) - 10} 个未显示")

        if getattr(page_state, 'element_count_truncated', False):
            parts.append("  ⚠️ 元素已截断，scroll 查看更多。")

    if page_state.forms:
        parts.append("\n## 表单")
        for form in page_state.forms:
            fields = ", ".join(form.get("fields", []))
            parts.append(f"  [{form.get('method','?').upper()}] {form.get('action','?')} — {fields}")

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

    changes = result.state_changes if result.state_changes else {}
    if changes.get("url_changed"):
        msg += "\n⚡ 页面URL已变化。"
    if changes.get("popup_disappeared"):
        msg += "\n⚡ 弹出面板已关闭。"
    if changes.get("popup_appeared"):
        msg += "\n⚡ 弹出面板已出现。"

    return msg


# ═══════════════════════════════════════════════════════════════════════════════
# 元素格式化辅助
# ═══════════════════════════════════════════════════════════════════════════════

def _format_element(el: dict[str, Any]) -> str:
    eid = el.get("id", "?")
    tag = el.get("tag", "?")
    parts = []
    if el.get("component"):
        parts.append(el["component"])
    text = el.get("text", "")
    if text:
        parts.append(f'"{text[:40]}"')
    if el.get("placeholder"):
        parts.append(f'placeholder="{el["placeholder"][:30]}"')
    if el.get("name") and not text:
        parts.append(f"name={el['name']}")
    if el.get("value"):
        parts.append(f'当前值="{el["value"][:20]}"')
    if el.get("type") and tag == "input":
        parts.append(el["type"])

    status = " [禁用]" if not el.get("enabled", True) else ""
    selector = el.get("css_selector", "")
    css_part = ""
    if selector and (selector.startswith("#") or "[name=" in selector or "[data-testid=" in selector):
        css_part = f" | css={selector}"

    return f"  [{eid}] <{tag}> {' '.join(parts)}{css_part}{status}"


def _is_similar_element(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("tag") != b.get("tag") or a.get("role") != b.get("role"):
        return False
    a_text = a.get("text", "").strip()
    b_text = b.get("text", "").strip()
    if a_text.isdigit() and b_text.isdigit():
        return True
    if len(a_text) > 0 and len(b_text) > 0 and abs(len(a_text) - len(b_text)) <= 5:
        if a_text[:2] == b_text[:2]:
            return True
    return False


def _format_element_group(group: list[dict[str, Any]]) -> str:
    first_id = group[0].get("id", "?")
    last_id = group[-1].get("id", "?")
    tag = group[0].get("tag", "?")
    role = group[0].get("role", "")
    count = len(group)
    texts = [el.get("text", "").strip() for el in group]
    disabled = [el for el in group if not el.get("enabled", True)]
    role_str = f" {role}" if role else ""

    if all(t.isdigit() for t in texts if t):
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
        text_summary = " ".join(f"{el.get('text','').strip()[:10]}=[{el.get('id','?')}]" for el in group)
    else:
        parts = [f"{el.get('text','').strip()[:10]}=[{el.get('id','?')}]" for el in group[:3]]
        parts.append("...")
        parts.extend(f"{el.get('text','').strip()[:10]}=[{el.get('id','?')}]" for el in group[-3:])
        text_summary = " ".join(parts)

    status = ""
    if disabled:
        d_items = [f"{el.get('text','').strip()}=[{el.get('id','?')}]" for el in disabled[:3]]
        status = f" (禁用: {', '.join(d_items)})"

    return f"  [{first_id}-{last_id}] <{tag}>{role_str} ×{count}: {text_summary}{status}"


def _group_and_format_elements(elements: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    i = 0
    while i < len(elements):
        el = elements[i]
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
    vh = viewport.get("height", 900)
    vw = viewport.get("width", 1920)
    in_vp, out_vp = [], []
    for el in elements:
        box = el.get("bounding_box", {})
        y, h = box.get("y", 0), box.get("height", 0)
        x, w = box.get("x", 0), box.get("width", 0)
        if y + h > 0 and y < vh and x + w > 0 and x < vw:
            in_vp.append(el)
        else:
            out_vp.append(el)
    return in_vp, out_vp
