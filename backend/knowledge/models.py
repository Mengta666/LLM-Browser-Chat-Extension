"""知识库数据模型。

定义操作记录、页面指纹、操作步骤的数据结构。
这些结构与存储后端解耦，JSON 和 Qdrant+Mongo 后端都用同一套模型。
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class OperationStep:
    """一个操作步骤。"""
    action: str                  # click/type/scroll/navigate/select 等
    target_text: str = ""        # 元素可见文本
    css_selector: str = ""       # 定位选择器
    text: str = ""               # type 动作的输入内容
    url_pattern: str = ""        # 操作时的 URL 路径（去域名）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OperationStep":
        return cls(
            action=d.get("action", ""),
            target_text=d.get("target_text", ""),
            css_selector=d.get("css_selector", ""),
            text=d.get("text", ""),
            url_pattern=d.get("url_pattern", ""),
        )


@dataclass
class OperationRecord:
    """一条知识库操作记录。"""
    id: str
    task_description: str        # 向量化文本（召回主键）
    trigger_prompt: str = ""     # 用户原始指令
    source: str = "confirmed"    # "confirmed"(确认执行) | "recorded"(用户录制)
    page_fingerprint: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    user_note: str = ""          # 用户评价备注（可空）
    created_at: str = ""
    used_count: int = 0          # 被引用次数
    success_after_use: int = 0   # 引用后成功次数

    def quality_score(self) -> float:
        """质量分：引用后成功率。未被引用过默认 1.0（新记录不惩罚）。"""
        if self.used_count == 0:
            return 1.0
        return self.success_after_use / self.used_count

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OperationRecord":
        return cls(
            id=d.get("id", ""),
            task_description=d.get("task_description", ""),
            trigger_prompt=d.get("trigger_prompt", ""),
            source=d.get("source", "confirmed"),
            page_fingerprint=d.get("page_fingerprint", {}),
            steps=d.get("steps", []),
            user_note=d.get("user_note", ""),
            created_at=d.get("created_at", ""),
            used_count=d.get("used_count", 0),
            success_after_use=d.get("success_after_use", 0),
        )
