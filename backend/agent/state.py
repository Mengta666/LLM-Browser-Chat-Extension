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


@dataclass
class AgentSession:
    """一次 Agent 自动化会话的完整状态（单 LLM 反应式循环）。"""

    session_id: str
    task: str
    model: str
    current_step: int = 0
    status: AgentStatus = AgentStatus.RUNNING
    require_confirmation: list[str] = field(default_factory=list)

    messages: list[dict[str, Any]] = field(default_factory=list)
    step_history: list[dict[str, Any]] = field(default_factory=list)
    pending_action: Optional[PageAction] = None
    summary: Optional[str] = None
    error: Optional[str] = None

    # 结构化输出：LLM 上一步的自评/记忆/意图（记进 history 供下一步参考）
    last_evaluation: str = ""
    last_memory: str = ""
    progress: str = ""                   # 供前端展示（= 最近 next_goal）

    created_at: float = field(default_factory=time.time)

    # 步数与超时防护
    max_steps: int = 40
    stale_retries: int = 0               # 连续 stale 重观察次数（防打转）
    force_done: bool = False             # 最后一步/前端超时：本步只接受 task_complete（对齐 browser-use _force_done_after_last_step）

    # 任务计划（LLM 自维护，作为结构化输出的 plan 字段；对齐 browser-use）
    plan_items: list[dict] = field(default_factory=list)     # [{content, status}]
    current_plan_item: int = -1                              # 当前进行的步骤序号(0-indexed)
