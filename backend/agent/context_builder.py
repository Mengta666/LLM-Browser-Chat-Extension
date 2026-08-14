"""Agent 上下文构建模块（单 LLM 反应式架构）。

把用户任务、页面观察（编号元素列表）、历史轨迹组装成单次 LLM 调用的 messages。
定位契约：索引直连——LLM 只输出元素编号 index，前端直取 data-agent-id 节点。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent.state import PageState, ActionResult, PageAction

if TYPE_CHECKING:
    from agent.state import AgentSession


MAX_FULL_OBSERVATION_STEPS = 3


SYSTEM_PROMPT = """你是一个浏览器自动化助手。用户给你一个任务，你通过操作页面元素完成它。

## 工作方式（反应式循环）
每一步：我给你【当前页面的可交互元素编号列表】，你选一个动作并用【编号】指定目标。
执行后我给你新的观察，你再决定下一步，直到任务完成时调用 task_complete。

## 定位方式（唯一）
每个可交互元素在观察里都有一个编号 `[N]`。你**只能**用这个编号定位元素——
在动作里填 `index: N`。不要臆想不在列表里的元素。

- 目标就在列表里 → 直接用它的编号
- 目标不在列表里 → 用 scroll 滚动查找，或 hover 展开可能藏着它的悬浮菜单
- 编号是每一轮重新分配的，**只用当前这一轮观察里的编号**

## 可用动作
- click(index): 点击元素（按钮/链接/复选框/下拉或筛选选项等）
- type(index, text): 在输入框输入文字
- select(index, option_text): 原生下拉选择；自定义下拉通常是 click 触发器展开后再 click 选项
- scroll(direction, amount): 滚动页面/容器，找出更多元素
- scroll_to_element(index): 滚动到某元素
- hover(index): 悬停展开悬浮菜单/下拉/tooltip
- focus(index) / clear(index) / press_key(key, index?)
- navigate(url): 跳转 URL
- wait(ms): 等待动态内容
- task_complete(summary, success): 任务结束时必须调用

## 核心原则
1. 先看清当前编号列表，只操作真实存在的编号。
2. 每次只做一个动作，做完等我返回新观察再决定下一步。
3. 复杂交互是多步的：触发→展开→选择。很多筛选器/下拉**点选项即时生效，没有"确定"按钮**——
   选中目标后若找不到"确定/应用"按钮，说明已生效，直接进入下一步，不要臆想确定按钮。
4. 面板已展开时（观察提示"活跃弹出层"），直接点面板内的目标编号；**不要**再点展开它的触发器（会把面板关掉）。
5. 修改已有值：先 clear 或点关闭图标，再输入。
6. 目标不在列表 → scroll 或 hover，不要瞎猜编号。
7. 最大化理解任务意图：查看类任务要真正看到内容（如进入详情页/看到 diff），不是看到标题就算完；
   操作类任务要看到结果确认（成功提示/页面跳转/列表变化）。不确定是否完成时，倾向"未完成"。
8. 任务完成或确认无法完成时，必须调用 task_complete。

## 任务规划（update_plan）
- 简单任务（1-2 步可完成）：直接执行，不要用 update_plan。
- 复杂任务（约 10 步以上）：第一步先用 update_plan 列出 3-10 个步骤，之后每完成一步就更新它的状态。
- 任务不清晰时：先探索几步，了解情况后再规划。
- 始终对照计划行动，避免偏离整体目标。
- 重要：完成所有计划项不代表任务完成——仍需确认最终目标真正达成。
"""


TEXT_MODE_FORMAT_APPENDIX = """
## 输出格式（非常重要！）
你必须且只能输出一个 JSON 对象，不要输出任何其他文字。格式：

点击编号 7：
```json
{"action": "click", "index": 7, "thought": "简短推理"}
```
输入文字：
```json
{"action": "type", "index": 2, "text": "要输入的文字", "thought": "..."}
```
滚动：
```json
{"action": "scroll", "direction": "down", "amount": 300, "thought": "..."}
```
任务完成：
```json
{"action": "task_complete", "summary": "完成了什么", "success": true, "thought": "..."}
```
更新计划（复杂任务用）：
```json
{"action": "update_plan", "items": [{"content": "进入Coding", "status": "done"}, {"content": "切换分支", "status": "current"}], "current": 1, "thought": "..."}
```

## 附加规则
- 每次只输出一个 JSON 动作
- 定位只用 index（当前观察里的编号）
- 不要在 JSON 之外添加任何文字
"""

SYSTEM_PROMPT_TEXT_MODE = SYSTEM_PROMPT + TEXT_MODE_FORMAT_APPENDIX


# ═══════════════════════════════════════════════════════════════════════════════
# 单 LLM 消息构建
# ═══════════════════════════════════════════════════════════════════════════════

def build_initial_messages(task: str, page_state: PageState, session: "AgentSession",
                           text_mode: bool = False) -> list[dict[str, str]]:
    """构造首步 messages：system + (任务 + 首屏观察)。"""
    system = SYSTEM_PROMPT_TEXT_MODE if text_mode else SYSTEM_PROMPT
    user = f"## 任务\n{task}\n\n{build_observation_message(page_state)}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


PLAN_SENTINEL = "[[plan]]"


def build_plan_block(session: "AgentSession") -> str:
    """渲染 LLM 自维护的任务计划（对齐 browser-use 标记）。为空返回空串。首行哨兵供去重。"""
    if not session.plan_items:
        return ""
    marks = {"done": "[x]", "current": "[>]", "pending": "[ ]", "skipped": "[-]"}
    lines = [f"{PLAN_SENTINEL}## 📋 任务计划（你维护的）"]
    for i, it in enumerate(session.plan_items):
        m = marks.get(it.get("status", "pending"), "[ ]")
        cur = "  ← 进行中" if i == session.current_plan_item else ""
        lines.append(f"  {m} {it.get('content', '')[:50]}{cur}")
    lines.append("（完成所有项 ≠ 任务完成，仍需确认最终目标已达成）")
    return "\n".join(lines)


TRAIL_SENTINEL = "[[trail]]"


def build_trail_hint(session: "AgentSession", page_state: PageState) -> str:
    """停滞时把"最近走过的路"摆给 LLM，让它自己判断是否在原地打转（给信息，不下命令）。

    对齐 browser-use：代码只保证历史可见，"要不要换思路"交给 LLM。首行哨兵供去重。
    """
    trail = []
    for s in session.step_history[-6:]:
        act = s.get("action", {}) or {}
        res = s.get("result", {}) or {}
        a = act.get("type", "?")
        idx = act.get("index")
        status = "✓" if res.get("success", True) else "✗"
        line = f"  s{s.get('step','?')} {a}" + (f"[{idx}]" if idx is not None else "") + f" {status}"
        det = (res.get("details", "") or "")[:30]
        if det:
            line += f" {det}"
        trail.append(line)
    trajectory = "\n".join(trail) if trail else "  （无）"

    return (
        f"{TRAIL_SENTINEL}## 📍 提示：你已连续多步停留在同一页面（{page_state.url}）\n"
        f"最近几步的操作轨迹：\n{trajectory}\n"
        f"如果这些操作没有让你更接近目标「{session.task}」，考虑换个思路："
        f"用 navigate 直接跳转 URL、hover 展开可能藏着目标的菜单、scroll 找目标、"
        f"或关掉干扰的弹层（Escape）。如果判断当前页面确实无法完成任务，调用 task_complete(success=false)。"
        f"（若你正在正常推进，忽略此提示继续即可。）"
    )


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
    """压缩旧观察步骤，只保留最近 N 步完整内容。"""
    observation_indices = [
        i for i, msg in enumerate(messages)
        if msg.get("role") == "user" and "## 动作执行结果" in msg.get("content", "")
    ]
    if len(observation_indices) <= MAX_FULL_OBSERVATION_STEPS:
        return
    for idx in observation_indices[:-MAX_FULL_OBSERVATION_STEPS]:
        content = messages[idx]["content"]
        lines = content.split("\n")
        result_line = next((l.strip() for l in lines if l.startswith("[") and ("✓" in l or "✗" in l)), "")
        if not result_line:
            result_line = lines[1] if len(lines) > 1 else "已执行"
        messages[idx] = {"role": "user", "content": f"[历史步骤] {result_line}"}


# ═══════════════════════════════════════════════════════════════════════════════
# 页面观察格式化（编号列表）
# ═══════════════════════════════════════════════════════════════════════════════

def build_observation_message(page_state: PageState) -> str:
    parts = []
    parts.append(f"## 当前页面\nURL: {page_state.url}\n标题: {page_state.title}")

    viewport_h = page_state.viewport.get('height', 0)
    scroll_y = page_state.scroll_position.get('y', 0)
    doc_height = page_state.document_height
    scroll_pct = round(scroll_y / max(doc_height - viewport_h, 1) * 100) if doc_height > viewport_h else 100
    at_bottom = scroll_pct >= 95
    parts.append(
        f"视口: {page_state.viewport.get('width', '?')}x{viewport_h} | 滚动 y={scroll_y} | "
        f"总高 {doc_height} | 进度 {scroll_pct}%{'（已到底）' if at_bottom else '（可继续下滚）'}"
    )

    container_info = getattr(page_state, 'scrollable_container', None)
    if isinstance(container_info, dict) and container_info:
        c_top = container_info.get('scroll_top', 0)
        c_total = container_info.get('scroll_height', 0)
        c_visible = container_info.get('client_height', 0)
        c_at_bottom = container_info.get('at_bottom', False)
        c_pct = round(c_top / max(c_total - c_visible, 1) * 100) if c_total > c_visible else 100
        parts.append(f"📦 容器滚动: {c_pct}%{'（已到底）' if c_at_bottom else '（可继续滚动）'}")

    if page_state.is_loading:
        parts.append("\n⏳ 页面加载中，建议 wait。")

    active_popup = getattr(page_state, 'active_popup', None)
    if isinstance(active_popup, dict) and active_popup:
        popup_type_map = {
            'date_picker': '日期选择面板', 'dropdown': '下拉列表', 'modal': '弹窗',
            'menu': '菜单', 'popup': '弹出面板', 'inline_group': '就地展开的选项组',
        }
        label = popup_type_map.get(active_popup.get('type', ''), '弹出面板')
        header = active_popup.get('header_text', '')
        parts.append(f"\n📍 活跃弹出层: {label}{f' ({header})' if header else ''}")
        parts.append("   优先操作弹出层内元素；不要再点展开它的触发器（会关闭面板），也不要点面板外空白。")

    if page_state.interactive_elements:
        popup_els = [el for el in page_state.interactive_elements if el.get("in_popup")]
        main_els = [el for el in page_state.interactive_elements if not el.get("in_popup")]
        if popup_els:
            parts.append(f"\n## 🔍 弹出面板内元素（{len(popup_els)}个，优先）")
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

    if page_state.text_content_summary:
        raw = page_state.text_content_summary
        words = raw.split()
        deduped, prev = [], ""
        for w in words:
            if w != prev:
                deduped.append(w); prev = w
        parts.append(f"\n## 页面文本摘要\n{' '.join(deduped)[:2200]}")

    return "\n".join(parts)


def build_action_result_message(action: PageAction, result: ActionResult) -> str:
    status = "✓ 成功" if result.success else ("↻ 编号失效，已重新观察" if result.stale else "✗ 失败")
    msg = f"[{status}] {action.type}"
    if action.index is not None:
        msg += f" [{action.index}]"
    if result.details:
        msg += f" | {result.details}"
    if result.error:
        msg += f" | 错误: {result.error}"
    changes = result.state_changes or {}
    if changes.get("url_changed"):
        msg += "\n⚡ 页面URL已变化。"
    if changes.get("popup_disappeared"):
        msg += "\n⚡ 弹出面板已关闭。"
    if changes.get("popup_appeared"):
        msg += "\n⚡ 弹出面板已出现。"
    return msg


# ═══════════════════════════════════════════════════════════════════════════════
# 元素格式化辅助（编号醒目，不再暴露 css 选择器）
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
    if el.get("occluded"):
        status += " [被遮挡]"
    return f"  [{eid}] <{tag}> {' '.join(parts)}{status}"


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
                group.append(elements[j]); j += 1
            else:
                break
        if len(group) >= 3:
            lines.append(_format_element_group(group)); i = j
        else:
            lines.append(_format_element(el)); i += 1
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
