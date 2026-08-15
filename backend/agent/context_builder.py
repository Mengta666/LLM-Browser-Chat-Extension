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


# ═══════════════════════════════════════════════════════════════════════════════
# 结构化输出 verify-first 架构（对齐 browser-use）
# 每步 LLM 返回一个 JSON：先自评上一步(对照观察) → 记忆进度 → 声明意图 → 可选计划 → 动作
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个浏览器自动化助手。用户给你一个任务，你通过操作页面元素、一步步完成它。

## 工作方式（反应式验证循环）
每一步我给你【当前页面的可交互元素编号列表 + 上一步执行结果】，你必须**先判断上一步是否成功**，
再决定下一个动作。你的每次回复都是**一个 JSON 对象**（见下方输出格式），不是纯动作。

## 定位方式（唯一）
每个可交互元素在观察里都有编号 `[N]`。你**只能**用编号定位——在动作里填 `index: N`。
- 目标在列表里 → 用它的编号；不在 → scroll 滚动查找，或 hover 展开悬浮菜单
- 编号每轮重新分配，**只用当前这轮观察里的编号**，不要臆想不存在的元素

## 可用动作（action.type）
- click(index) / type(index,text) / select(index,option_text)
- scroll(direction,amount) / scroll_to_element(index) / hover(index)
- focus(index) / clear(index) / press_key(key,index?)
- navigate(url) / wait(ms)
- task_complete(summary,success): 任务结束时必须调用

## 核心原则
1. 每步先自评：上一步动作**真的生效了吗**？对照新观察（URL变了吗/面板开了吗/内容变了吗）判断，
   绝不能因为"我以为点了"就当成功。没生效就换方式，别盲目重复。
2. 每次只做一个动作，做完看新观察再决定下一步。
3. 复杂交互是多步的：触发→展开→选择。很多筛选器/下拉**点选项即时生效，没有"确定"按钮**——
   选中后找不到"确定"就是已生效，直接下一步，不要臆想确定按钮。
4. 面板已展开时（观察提示"活跃弹出层"），直接点面板内目标编号；**不要**再点展开它的触发器（会关掉面板）。
5. 修改已有值：先 clear 或点关闭图标，再输入。
6. 目标不在列表 → scroll 或 hover，不要瞎猜编号。
7. 最大化理解任务意图：查看类任务要真正看到内容（进详情页/看到 diff），不是看到标题就算完。

## 避免死循环（重要）
- 同一个失败的动作，**绝不重复超过 2-3 次**——立刻换一种方式（换入口 / 换分支 / navigate 直达 / 换关键词 / hover 展开）。
- 如果连续 3 步以上停在同一页面、没有实质进展，必须换完全不同的思路，不要小修小补地重试。

## 弹窗/遮挡优先处理
- 遇到弹窗、报错框、遮罩挡住主页面时，**先处理它再做别的**：找关闭按钮（×、关闭、取消、Skip、No thanks）点掉。
- 报错弹窗（如"请求失败""INTERNAL error"）挡住时，先关掉它再继续；若 Escape 无效就找并点击关闭按钮（×）。

## 何时判定"无法完成"（务必果断）
- 若某信息在页面上确实找不到，**如实说明**（"经多次尝试未找到 X"），绝不编造或猜测一个值。
- 试过 **2-3 种不同方式/入口**后，目标仍反复显示"无结果 / 无匹配 / 不存在"，很可能它确实不存在——
  这时应调用 task_complete(success=false)，summary 说明你尝试了什么、结论是什么。
- **部分结果 + success=false 远比谎报成功有价值。** 不要为了"完成任务"而无限重试或编造。

## 任务规划（可选字段 plan）
- 简单任务（1-3 步可完成）：不用规划，plan 留空直接做。
- 复杂任务（约 10 步以上）：第一步就在 plan 里列出 3-10 个步骤，之后每完成一步更新其 status。
- 任务不清晰时：先探索几步了解情况，再补 plan。
- 始终对照计划行动，避免偏离整体目标。

## 调用 task_complete 前的自检（pre-done 验证，务必执行）
在 action.type == "task_complete" 且 success=true 前，逐项确认：
1. 重读原始任务，逐项核对是否都完成了。
2. **数据基础**：你在 summary 里报告的每个值（URL/名称/数字/内容）必须**逐字出现在你看到的页面观察里**——绝不编造或猜测。
3. 若用了筛选/搜索，确认条件真的生效了（结果页确实按条件过滤了）。
4. 确认关键动作真的发生了（对照观察，不是"我以为点了"）。
5. 完成所有计划项 ≠ 任务完成，必须对照原始任务确认。
任一项不满足/不确定 → success=false。

## 输出格式（非常重要，必须严格遵守）
你必须且只能输出**一个 JSON 对象**，不要输出任何其他文字。字段：
```json
{
  "evaluation_previous_goal": "上一步想做什么 + 对照观察判断结果。必须以 成功/失败/不确定 结尾。首步填『任务开始』。",
  "memory": "1-3句：当前进度、已试过什么、关键信息（如已翻N页、已找到X）。用于跨步记忆。",
  "next_goal": "这一步要达成的具体目标（一句话）。",
  "plan": [{"content": "步骤描述", "status": "pending|current|done|skipped"}],
  "current_plan_item": 1,
  "action": {"type": "click", "index": 7}
}
```
- `evaluation_previous_goal`、`memory`、`next_goal`、`action` 必填；`plan`、`current_plan_item` 可选（简单任务省略）。
- action 示例：
  - 点击：`{"type":"click","index":7}`
  - 输入：`{"type":"type","index":2,"text":"关键词"}`
  - 滚动：`{"type":"scroll","direction":"down","amount":300}`
  - 按键：`{"type":"press_key","key":"Enter","index":2}`
  - 跳转：`{"type":"navigate","url":"https://..."}`
  - 完成：`{"type":"task_complete","summary":"...","success":true}`
- 只输出这个 JSON，不要 markdown 之外的解释文字（可以放进 memory）。
"""


# 结构化架构下不再区分 tool_calls / text_parse，统一结构化 JSON。保留别名兼容旧引用。
SYSTEM_PROMPT_TEXT_MODE = SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════════════════════════
# 消息构建
# ═══════════════════════════════════════════════════════════════════════════════

def build_initial_messages(task: str, page_state: PageState, session: "AgentSession",
                           text_mode: bool = False) -> list[dict[str, str]]:
    """构造首步 messages：system + (任务 + 首屏观察)。"""
    user = f"## 任务\n{task}\n\n{build_observation_message(page_state)}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


PLAN_SENTINEL = "[[plan]]"


def build_plan_block(session: "AgentSession") -> str:
    """渲染 LLM 自维护的任务计划（对齐 browser-use 标记）。为空返回空串。首行哨兵供去重。"""
    if not session.plan_items:
        return ""
    marks = {"done": "[x]", "current": "[>]", "pending": "[ ]", "skipped": "[-]"}
    lines = [f"{PLAN_SENTINEL}## 📋 当前计划（你维护的，本轮可在 plan 字段更新）"]
    for i, it in enumerate(session.plan_items):
        m = marks.get(it.get("status", "pending"), "[ ]")
        cur = "  ← 进行中" if i == session.current_plan_item else ""
        lines.append(f"  {m} {it.get('content', '')[:50]}{cur}")
    return "\n".join(lines)


def append_step_messages(
    messages: list[dict[str, Any]],
    action: PageAction,
    result: ActionResult,
    new_page_state: PageState,
    prev_eval: str = "",
    prev_memory: str = "",
) -> list[dict[str, Any]]:
    """追加一轮：LLM 上一步的自评/记忆（assistant）+ 动作结果+新观察（user）。滑动窗口压缩旧步骤。"""
    _compress_old_observations(messages)
    # 把 LLM 上一步的判断记进历史（结构化架构：eval/memory 是状态在无状态调用间流动的载体）
    if prev_eval or prev_memory:
        note = []
        if prev_eval:
            note.append(f"评估: {prev_eval[:120]}")
        if prev_memory:
            note.append(f"记忆: {prev_memory[:150]}")
        messages.append({"role": "assistant", "content": " | ".join(note)})
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
