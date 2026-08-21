"""Agent 状态定义模块。

描述一次 Agent 自动化会话中的所有数据结构：
会话状态、页面观察、动作指令、执行结果。

定位契约：索引直连——动作只带 index（观察时打标的 data-agent-id 编号），
前端直取该节点。无 css/text 模糊匹配。
"""

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Optional

from pydantic import BaseModel


class AgentStatus(str, Enum):
    RUNNING = "running"
    ACTION_REQUIRED = "action_required"
    CONFIRM_REQUIRED = "confirm_required"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class PageAction(BaseModel):
    type: str  # click, type, select, scroll, hover, focus, clear, press_key, wait, navigate, task_complete
    index: Optional[int] = None          # 目标元素编号（data-agent-id）；无需元素的动作为 None
    params: dict[str, Any] = {}


class ActionResult(BaseModel):
    success: bool
    action_type: str
    details: str = ""
    error: Optional[str] = None
    timestamp: int = 0
    stale: bool = False                  # 编号失效（页面已重渲染）→ 需重新观察，不计失败
    state_changes: Optional[dict[str, Any]] = None


class PageState(BaseModel):
    url: str = ""
    title: str = ""
    viewport: dict[str, int] = {}
    scroll_position: dict[str, int] = {}
    document_height: int = 0
    scrollable_container: Optional[dict[str, Any]] = None
    active_popup: Optional[dict[str, Any]] = None
    page_fingerprint: Optional[dict[str, Any]] = None
    is_loading: bool = False
    focused_element: Optional[str] = None
    interactive_elements: list[dict[str, Any]] = []
    element_count_truncated: bool = False
    text_content_summary: str = ""
    forms: list[dict[str, Any]] = []
    screenshot: str = ""        # data:image/... base64（多模态截图 ground truth；不进 history，仅当步用）


@dataclass
class HistoryItem:
    """一步的结构化历史（对齐 browser-use HistoryItem）。

    每步只留这几个字段，不留 LLM 原始回复/完整观察——这是 token 定长的关键。
    渲染时拼成一行短文本，老步骤靠 max_history_items 滑动窗口省略。
    """
    step: int
    evaluation: str = ""        # 对上一步的自评
    memory: str = ""            # 跨步记忆
    next_goal: str = ""         # 当步意图
    action: str = ""            # 执行的动作摘要（如 "click [7] 提交"）
    result: str = ""            # 动作结果摘要（成功/失败 + 关键变化）

    def to_string(self) -> str:
        if self.step == -1:                       # compacted 摘要项
            return f"[前序步骤摘要] {self.memory}"
        parts = []
        if self.evaluation:
            parts.append(f"评估:{self.evaluation}")
        if self.action:
            parts.append(f"动作:{self.action}")
        if self.result:
            parts.append(f"结果:{self.result}")
        if self.memory:
            parts.append(f"记忆:{self.memory}")
        if self.next_goal:
            parts.append(f"目标:{self.next_goal}")
        return f"[步骤{self.step}] " + " | ".join(parts)


@dataclass
class AgentSession:
    """一次 Agent 自动化会话的完整状态（单 LLM 反应式循环）。"""

    session_id: str
    task: str
    model: str
    current_step: int = 0
    status: AgentStatus = AgentStatus.RUNNING
    require_confirmation: list[str] = field(default_factory=list)

    messages: list[dict[str, Any]] = field(default_factory=list)   # 每步重建（system+任务+历史+观察），不累积
    history_items: list[HistoryItem] = field(default_factory=list)  # 结构化历史（对齐 browser-use），token 定长的核心
    step_history: list[dict[str, Any]] = field(default_factory=list)
    pending_action: Optional[PageAction] = None
    summary: Optional[str] = None
    success: bool = True                  # 任务完成时是否真正成功（LLM task_complete 的 success；force_done 收尾为 False）
    error: Optional[str] = None

    # 结构化输出：LLM 上一步的自评/记忆/意图（记进 history 供下一步参考）
    last_evaluation: str = ""
    last_memory: str = ""
    progress: str = ""                   # 供前端展示（= 最近 next_goal）

    task_image: str = ""                 # 任务附带的视觉上下文（用户上传图/框选截图，data URL）；随首条 user 消息注入

    # 长期记忆(分层):
    # - resident_preferences: 常驻用户偏好,每步无条件注入 prompt(首步检索一次)。
    # - task_domain: 当前任务站点域名,recall 工具/启发式兜底按它过滤。
    # 空 = 无记忆或子系统不可用 → 对 agent 零影响。
    resident_preferences: list = field(default_factory=list)
    task_domain: str = ""
    memory_retrieved: bool = False       # 是否已在首步检索过常驻偏好(避免每步重复)

    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)  # 每次 /step 刷新；空闲 TTL 依据（活跃任务不被误清）
    in_flight: bool = False              # 忙标志：该会话有请求正在 run_step 处理中，拒绝同会话并发（防 current_step 竞态）

    # 步数与超时防护
    max_steps: int = 200                 # 单任务最大步数（第 199 步自动 force_done 收尾，第 200 步硬兜底 ERROR）
    stale_retries: int = 0               # 连续 stale 重观察次数（防打转）
    force_done: bool = False             # 最后一步/前端超时：本步只接受 task_complete（对齐 browser-use _force_done_after_last_step）

    # 任务计划（LLM 自维护，作为结构化输出的 plan 字段；对齐 browser-use）
    plan_items: list[dict] = field(default_factory=list)     # [{content, status}]
    current_plan_item: int = -1                              # 当前进行的步骤序号(0-indexed)
