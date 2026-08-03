"""Agent 状态定义模块。

描述一次 Agent 自动化会话中的所有数据结构：
会话状态、页面观察、动作指令、执行结果。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class AgentStatus(str, Enum):
    RUNNING = "running"
    ACTION_REQUIRED = "action_required"
    CONFIRM_REQUIRED = "confirm_required"
    PLAN_READY = "plan_ready"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class ElementLocator(BaseModel):
    method: str  # "css" | "text" | "annotation_id"
    value: str
    fallback: Optional["ElementLocator"] = None


class PageAction(BaseModel):
    type: str  # click, type, select, scroll, hover, focus, clear, press_key, wait, task_complete
    locator: Optional[ElementLocator] = None
    params: dict[str, Any] = {}


class ActionResult(BaseModel):
    success: bool
    action_type: str
    details: str = ""
    error: Optional[str] = None
    timestamp: int = 0
    state_changes: Optional[dict[str, Any]] = None


class PageState(BaseModel):
    url: str = ""
    title: str = ""
    viewport: dict[str, int] = {}
    scroll_position: dict[str, int] = {}
    document_height: int = 0
    scrollable_container: Optional[dict[str, Any]] = None
    active_popup: Optional[dict[str, Any]] = None
    is_loading: bool = False
    focused_element: Optional[str] = None
    interactive_elements: list[dict[str, Any]] = []
    element_count_truncated: bool = False
    text_content_summary: str = ""
    forms: list[dict[str, Any]] = []


@dataclass
class SubTask:
    """Agent 任务分解后的子任务。"""
    description: str
    status: str = "pending"  # "pending" | "in_progress" | "completed" | "skipped"
    retry_count: int = 0
    retry_reason: str = ""


@dataclass
class FailedAttempt:
    """记录一次失败的操作尝试。"""
    action_type: str
    target: str
    error: str
    step: int


@dataclass
class AgentSession:
    """一次 Agent 自动化会话的完整状态。"""

    session_id: str
    task: str
    model: str
    max_steps: int = 15
    current_step: int = 0
    status: AgentStatus = AgentStatus.RUNNING
    require_confirmation: list[str] = field(default_factory=list)

    messages: list[dict[str, Any]] = field(default_factory=list)
    step_history: list[dict[str, Any]] = field(default_factory=list)
    pending_action: Optional[PageAction] = None
    summary: Optional[str] = None
    error: Optional[str] = None
    call_mode: Optional[str] = None  # "tool_calls" | "text_parse" | None(auto)

    # 任务分解
    sub_tasks: list[SubTask] = field(default_factory=list)
    current_sub_task_index: int = 0
    planning_done: bool = False

    # 反思机制
    failed_attempts: list[FailedAttempt] = field(default_factory=list)
    blacklisted_approaches: list[str] = field(default_factory=list)
