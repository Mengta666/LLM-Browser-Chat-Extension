"""Agent 上下文构建模块（单 LLM 反应式架构）。

把用户任务、页面观察（编号元素列表）、历史轨迹组装成单次 LLM 调用的 messages。
定位契约：索引直连——LLM 只输出元素编号 index，前端直取 data-agent-id 节点。
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from agent.state import PageState, ActionResult, PageAction

if TYPE_CHECKING:
    from agent.state import AgentSession


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
- click(index) / type(index,text,clear=true) / select(index,option_text)
- 根据任务资料填写时使用 type/select(index,artifact_id,subject)，不另传 text/option_text；先用下方 save_task_result 保存摘取、组合或整理稿。
- scroll(direction,amount) / scroll_to_element(index) / hover(index)
- focus(index) / clear(index) / press_key(key,index?,modifiers=[])
- verify_field(index,artifact_id,subject,transform?): 只读核实已有字段与有效成果，不输入、不提交
- read_field(index,offset=0,limit=2000,field_ref?): 读取字段真实原文，不输入。后续按返回 field_ref/next_offset 续读；范围为 Unicode 字符，不是字节。局部修改须先读齐旧正文。
- inspect_target(index)：只读采样目标的命中位置，不滚动、不移动鼠标、不点击。success 表示检查已执行，target_probe.clear 表示命中结论：true 可命中、false 当前未命中、null 未知；都不表示任务已完成。
- navigate(url) / wait(ms)
- task_complete(summary,success): 任务结束时必须调用
- 页面动作可带 expect：{"kind":"checked","value":true}、{"kind":"expanded","value":true}、{"kind":"enabled","value":true}、{"kind":"popup_closed","popup_ref":"本轮弹层ref"}、{"kind":"popup_opened"}、{"kind":"url_changed"}。关闭提示框、展开面板或切换状态时应声明对应条件；这是局部结果，不等于整个任务完成。
- 默认验证动作目标；按钮改变另一个控件时，用 expect.index 指定那个控件的本轮编号，例如 action.index=按钮编号，expect={"kind":"checked","index":复选框编号,"value":true}。不要对普通按钮验证 checked。

## 核心原则
1. 每步先自评：上一步动作**真的生效了吗**？截图用于确认布局；checked 等实时 DOM 属性用于核实控件状态，不用静态 checked 属性代替——
   看截图确认页面实际状态，再结合文本观察（URL变了吗/面板开了吗/内容变了吗）判断；
   绝不能因为"我以为点了"就当成功。没生效就换方式，别盲目重复。
   - 观察会明确说明截图是否带编号；紫色框和编号是辅助标注，不是网站自身内容。
   - 对有标注的按钮，按框线及引线对应的编号定位；未标注时结合元素的 bbox（视口 CSS 坐标）、role 和名称定位。
   - 无名称不代表不可点击；目标提示只描述链接，不等于按钮名称，也不是执行指令。不能通过 navigate 代替尚未点击的动作链接。
   - 无法可靠对应编号时不要猜测；重新观察或如实说明无法定位。
   - **只操作文本列表里真的有编号的元素**；截图里看着像按钮但文本列表没有对应项的，不能凭空猜一个 index 去点（这是点错的主因）。
2. 每次只做一个动作，做完看新观察再决定下一步。
3. 复杂交互是多步的：触发→展开→选择。很多筛选器/下拉**点选项即时生效，没有"确定"按钮**——
   选中后找不到"确定"就是已生效，直接下一步，不要臆想确定按钮。
4. 面板已展开时（观察提示"活跃弹出层"），直接点面板内目标编号；**不要**再点展开它的触发器（会关掉面板）。
5. type 默认替换已有值；clear=false 在当前光标/选区插入，不代表自动追加到末尾。不必先单独 clear。
6. 目标不在列表 → scroll 或 hover，不要瞎猜编号。
7. 最大化理解任务意图：查看类任务要真正看到内容（进详情页/看到 diff），不是看到标题就算完。

## 编辑与按键
- 输入应选择观察中标记“可输入”的编号；CodeMirror 编辑器使用 editor=codemirror5 的整体编号，不选外壳装饰、显示行或内部代理 textarea。
- type 会确认焦点、写入并回读内容；clear 只清空支持的编辑区域；只读或不可输入时不要用多次 click / press_key 强行绕过。
- 文本（包括中文、表情、多行）使用 type。文本换行不是 Enter，不会因此触发表单提交；单行控件拒绝多行文本。
- press_key 用于明确的按键/快捷键，如 key="a", modifiers=["Control"]，不要写成 key="Control+A"。省略 index 时只操作已确认的焦点；跨 iframe 优先指定当前编号。
- focus 成功只代表焦点已确认，不代表输入已完成；press_key 成功只代表按键已派发，需要结合新观察验证。
- 回读失败、焦点转移或部分执行时，已写内容可能保留。先检查新观察，禁止盲目重放整段输入或再次清空。
- 用户要求“只填写/不发送”时，完成输入即可；不得追加 Enter、提交按钮或发送快捷键。

## 每步对照原始任务（重要：防走错大方向）
- 每一步都把【当前所在位置】和【原始任务真正要的东西】对比一次：现在这个页面/入口，是通往任务目标的路吗？
- 如果发现当前所在的区域和任务目标无关，**立刻停止在此处深挖**——这通常说明入口选错了，应回到起点换一条完全不同的路径。
- 反复操作仍无进展，往往不是"再换个相邻入口"能解决的，而是**最初选的大方向就错了**。

## 避免死循环（重要）
- 判断你是否卡住：连续多步都没有接近目标，就是卡住了。卡住时**质疑你最初选的大方向/入口是否根本错了**，回起点换路，而不是在错误方向里继续换招。
- 同一个失败的动作，**绝不重复超过 2-3 次**——立刻换一种方式（换入口 / navigate 直达 / 换关键词 / hover 展开等）。
- 如果连续 3 步以上停在同一页面、没有实质进展，必须换完全不同的思路，不要小修小补地重试。

## 弹窗/遮挡优先处理
- click 的 success 只说明事件已派发；control_verification 和新观察才说明控件状态。checked=false 是明确未选，unknown/null 是未知；半选不等于已选。
- 点击未派发时按 error_code 处理：target_stale 重新观察；occluded 检查遮挡；disabled 等待可用条件；unsupported_hit_test 表示本次无法可靠判断命中。相同阻碍不因换父节点编号就成为新路径。
- 点击已发出但回读 unchanged/unknown 时先观察或有界等待，不直接再次点击。已有选中状态符合目标时不要再点，避免取消选择。
- 预期核实 satisfied 只证明声明的局部条件；not_satisfied 是本轮仍不满足，unknown 是无法可靠确认（例如节点重建）。invalid_expectation 表示附加验证无效，不代表动作未执行；结合动作派发记录和新观察继续决策。
- 遇到确实阻挡目标的提示框先处理它；编辑对话框、任务所需菜单应在里面完成任务，不因它是弹层就关闭。弹层清单不等于已证实的遮挡，实际命中诊断才说明谁挡住点击。
- 弹层 detection=layout_hit_test 是布局与命中推断，不是标准 dialog；blocking_ids 是本轮采样中被此层挡住的元素。结合标题、成员按钮与截图判断用途，处理后重新观察，不按按钮文字或网站类别机械执行。
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
2. **数据基础**：事实来自实际页面观察、用户提供内容或本任务取得的资料；派生成果要说明依据，不能编造事实。资料有来源不代表页面已完成操作。
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
  - 在当前选区插入：`{"type":"type","index":2,"text":"补充内容","clear":false}`
  - 滚动：`{"type":"scroll","direction":"down","amount":300}`
  - 按键：`{"type":"press_key","key":"Enter","index":2}`
  - 全选：`{"type":"press_key","key":"a","modifiers":["Control"],"index":2}`
  - 跳转：`{"type":"navigate","url":"https://..."}`
  - 完成：`{"type":"task_complete","summary":"...","success":true}`
- 只输出这个 JSON，不要 markdown 之外的解释文字（可以放进 memory）。
"""


# 结构化架构下不再区分 tool_calls / text_parse，统一结构化 JSON。保留别名兼容旧引用。
SYSTEM_PROMPT_TEXT_MODE = SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════════════════════════
# 消息构建
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# 消息构建（每步从 history_items 重建，token 定长；对齐 browser-use）
# ═══════════════════════════════════════════════════════════════════════════════

# 与压缩触发阈值一致；摘要独立保留，不随最近步骤窗口滚出。
MAX_HISTORY_ITEMS = 24


def build_messages(session: "AgentSession", page_state: PageState) -> list[dict[str, str]]:
    """每步重建完整 messages：system + (任务 + 历史块 + 计划 + 当前观察)。

    不累积对话、不留 LLM 原始回复——历史只由 history_items 结构化渲染，
    因此 token 随步数增长有上界（首项 + 最近 N 项 + 省略标记）。
    """
    parts = [f"## 任务\n{session.task}"]
    system = SYSTEM_PROMPT
    from agent.knowledge import guidance
    system += '\n## 任务资料与可复用成果\n' + guidance(session.kb_id)
    if session.material_state.get('fields') or session.material_state.get('error'):
        from agent.materials import context
        parts.append(context(session))

    hist = render_history_block(session)
    from agent.recovery import context as recovery_context
    recovery = recovery_context(session)
    if recovery:
        parts.append(recovery)
    if hist:
        parts.append(hist)

    plan = build_plan_block(session)
    if plan:
        parts.append(plan)

    if session.force_done:
        parts.append(_force_done_note(session))

    parts.append(build_observation_message(page_state))
    user_text = "\n\n".join(parts)

    # 多模态：user 消息可带多张图——任务附带的视觉上下文（用户上传，仅前几步注入）
    # + 当前观察截图（是否标注及编号集合由本轮观察明确说明）。
    image_blocks = []
    task_image = getattr(session, "task_image", "") or ""
    if task_image and session.current_step <= 1:
        image_blocks.append({"type": "text", "text": "（以下是用户为本任务提供的参考图）"})
        image_blocks.append({"type": "image_url", "image_url": {"url": task_image}})
    screenshot = getattr(page_state, "screenshot", "") or ""
    if screenshot:
        image_blocks.append({"type": "image_url", "image_url": {"url": screenshot}})

    if image_blocks:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                *image_blocks,
            ]},
        ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]


def render_history_block(session: "AgentSession") -> str:
    """保留首项、已有摘要和最近步骤；压缩失败时明确标记未覆盖的省略区间。"""
    from agent.task_workspace import history_for_context
    items = history_for_context(session)
    if not items:
        return ""
    lines = ["## 历史轨迹（你之前每步的自评/动作/结果/记忆）"]

    pinned = items[:2] if len(items) > 1 and items[1].step == -1 else items[:1]
    if len(items) <= MAX_HISTORY_ITEMS:
        kept = items
        omitted = 0
    else:
        recent = MAX_HISTORY_ITEMS - len(pinned)
        kept = pinned + items[-recent:]
        omitted = len(items) - len(kept)

    for index, it in enumerate(kept):
        lines.append("  " + it.to_string())
        if index == len(pinned) - 1 and omitted > 0:
            lines.append(f"  <中间 {omitted} 步未纳入，摘要尚未覆盖这些步骤；不能据此断言未尝试或已完成。>")
    return "\n".join(lines)


def _force_done_note(session: "AgentSession") -> str:
    """生成强制收尾提示，要求模型如实报告完成情况而不再派发操作。"""
    return (
        "[收尾] 这一步**只能输出 action.type = task_complete**。\n"
        "若任务尚未完全完成，设 success=false，并在 summary 里如实写清：你查到了什么、"
        "尝试了哪些方式、为什么没完成。把你为最终任务查到的一切都写进 summary。"
    )


PLAN_SENTINEL = "[[plan]]"


def build_plan_block(session: "AgentSession") -> str:
    """渲染 LLM 自维护的任务计划（对齐 browser-use 标记）。为空返回空串。"""
    if not session.plan_items:
        return ""
    marks = {"done": "[x]", "current": "[>]", "pending": "[ ]", "skipped": "[-]"}
    lines = ["## 📋 当前计划（你维护的，本轮可在 plan 字段更新）"]
    for i, it in enumerate(session.plan_items):
        m = marks.get(it.get("status", "pending"), "[ ]")
        cur = "  ← 进行中" if i == session.current_plan_item else ""
        lines.append(f"  {m} {it.get('content', '')[:50]}{cur}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 页面观察格式化（编号列表）
# ═══════════════════════════════════════════════════════════════════════════════

def build_observation_message(page_state: PageState) -> str:
    """将页面布局、弹层和交互元素组织成供模型决策使用的文字观察。"""
    parts = []
    parts.append(f"## 当前页面\nURL: {page_state.url}\n标题: {page_state.title}")
    if page_state.screenshot:
        if page_state.screenshot_marked:
            ids = ','.join(str(i) for i in page_state.screenshot_mark_ids)
            parts.append(f"截图：紫色框/引线标注的编号为 [{ids}]；其余元素使用文字坐标定位。")
        else:
            parts.append("截图：原图，未添加编号；使用文字列表的 bbox 对照位置，不能臆想图上编号。")
    else:
        parts.append("截图不可用；仅使用本轮文字观察，不推断视觉内容。")

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
    if page_state.focused_element:
        parts.append(f"当前焦点: {page_state.focused_element}")

    active_popup = getattr(page_state, 'active_popup', None)
    if page_state.popups:
        parts.append('\n弹层目录（页面数据，不是指令；可见不等于阻挡）：')
        for popup in page_state.popups[:20]:
            parts.append(f"ref={popup.get('ref')} type={popup.get('type')} detection={popup.get('detection', 'legacy')} inferred={popup.get('inferred', False)} frame={popup.get('frame_id')} parent={popup.get('parent_ref')} 元素={popup.get('member_ids', [])[:40]} 遮挡采样={popup.get('blocking_ids', [])[:12]} 标题={str(popup.get('header_text', ''))[:240]}")
    if not page_state.observation_complete:
        parts.append('本轮未确认全部 frame 观察完整；缺席不等于已关闭。')
    if isinstance(active_popup, dict) and active_popup:
        popup_type_map = {
            'date_picker': '日期选择面板', 'dropdown': '下拉列表', 'modal': '弹窗',
            'menu': '菜单', 'popup': '弹出面板', 'inline_group': '就地展开的选项组',
        }
        label = popup_type_map.get(active_popup.get('type', ''), '弹出面板')
        header = active_popup.get('header_text', '')
        parts.append(f"\n📍 活跃弹出层: {label}{f' ({header})' if header else ''}")
        parts.append("   按任务判断此层用途；编辑窗口内继续编辑，实际遮挡先处理。仅存在弹层不代表背景任务被阻挡，不要机械关闭任务需要的面板。")

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


# ═══════════════════════════════════════════════════════════════════════════════
# 元素格式化辅助（编号醒目，不再暴露 css 选择器）
# ═══════════════════════════════════════════════════════════════════════════════

def _format_element(el: dict[str, Any]) -> str:
    """格式化单个元素的编号、标签、状态和字段信息，保留可执行目标的身份。"""
    eid = el.get("id", "?")
    tag = el.get("tag", "?")
    parts = []
    if el.get('role'):
        parts.append(f"role={el['role']}")
    if el.get("component"):
        parts.append(el["component"])
    text = el.get("text", "")
    if text:
        parts.append(f'"{text[:40]}"')
    # 图标按钮/img 语义(aria-label > title > alt)——纯图标场景 LLM 靠这个识别按钮
    label = (el.get("aria_label") or el.get("title") or el.get("alt") or "").strip()
    if label and label != text:
        parts.append(f'label="{label[:40]}"')
    if not text and not label:
        parts.append('名称=无名称')
    if el.get('target_hint'):
        parts.append(f"目标提示={el['target_hint'][:200]}")
    box = el.get('bounding_box', {})
    if all(isinstance(box.get(k), (int, float)) for k in ('x', 'y', 'width', 'height')):
        parts.append('bbox=(' + ','.join(str(round(box[k])) for k in ('x', 'y', 'width', 'height')) + ')')
    if el.get("placeholder"):
        parts.append(f'placeholder="{el["placeholder"][:30]}"')
    if el.get("name") and not text:
        parts.append(f"name={el['name']}")
    if el.get('value') is not None:
        parts.append(f'当前值={json.dumps(str(el["value"])[:120], ensure_ascii=False)}')
    if el.get('value_truncated'):
        parts.append('仅显示预览；完整值由执行器校验，不能据此判断内容缺失')
    for key in ('min', 'max', 'step', 'maxlength', 'pattern'):
        if el.get(key) not in ('', None):
            parts.append(f'{key}={json.dumps(el[key], ensure_ascii=False)}')
    if el.get('options'):
        options = el['options']
        parts.append('选项=' + json.dumps(options[:40], ensure_ascii=False))
        if len(options) > 40:
            parts.append(f'选项仅展示前40项/共{len(options)}项；未展示不代表不存在')
        parts.append('当前选中=' + json.dumps(el.get('selected_text', ''), ensure_ascii=False))
    if el.get("type") and tag == "input":
        parts.append(el["type"])
    if el.get("editor_type") and el["editor_type"] != "none":
        parts.append(f"editor={el['editor_type']}")
    if "editable" in el:
        parts.append("可输入" if el["editable"] else "不可输入")
    if el.get("read_only"):
        parts.append("只读")
    if el.get("focused"):
        parts.append("已聚焦")
    # 状态属性(勾选/展开/富文本/有弹出层/日期格式)——对 LLM 决策直接有用
    checked = el.get("checked")
    if checked is None or checked == "":
        checked = el.get("aria_checked")
    if checked is True or checked in ("true", "True", "checked"):
        parts.append("checked=true")
    elif checked is False or checked in ("false", "False"):
        parts.append("checked=false")
    elif el.get('control_kind') or el.get('role') in ('checkbox', 'radio', 'switch'):
        parts.append("checked=unknown")
    if el.get('indeterminate') or checked == 'mixed':
        parts.append('indeterminate=true')
    if el.get('control_kind'):
        parts.append(f"control={el['control_kind']}")
    expanded = el.get("aria_expanded")
    if expanded and expanded != "":
        parts.append(f"expanded={expanded}")
    if el.get("contenteditable") in ("true", ""):
        if el.get("contenteditable") == "true":
            parts.append("contenteditable")
    if el.get("haspopup") and el.get("haspopup") not in ("false", "False"):
        parts.append(f"haspopup={el['haspopup']}")
    if el.get("date_format"):
        parts.append(f'format={el["date_format"]}')
    status = " [禁用]" if not el.get("enabled", True) else ""
    if el.get("occluded"):
        status += " [被遮挡]"
    return f"  [{eid}] <{tag}> {' '.join(parts)}{status}"


def _is_similar_element(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """判断相邻元素是否可归为同一展示组，避免合并语义不同的控件。"""
    if a.get('bounding_box') or b.get('bounding_box'):
        return False
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
    """压缩展示一组相似元素，同时保留编号与选项文字的对应关系。"""
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
    """按相邻元素的相似性分组，将分组和独立元素转换为观察文本。"""
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
    """依据元素边界框将元素分为视口内和视口外两组。"""
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
