"""M4 注入接线验证:build_messages 的记忆注入。无需网络(直接构造 session)。

覆盖:
- 无记忆时 prompt 与现状一致(不破坏 agent)——回归红线
- 有记忆时首步注入、含记忆块
- 仅前两步注入(current_step<=1),后续步不注入(省 token)
- memory_context 字段默认空
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.state import AgentSession, PageState
from agent.context_builder import build_messages


def _mk_session(memory_context=None, current_step=0):
    s = AgentSession(session_id="t", task="在页面点击提交按钮", model="gpt-4o")
    s.current_step = current_step
    if memory_context is not None:
        s.memory_context = memory_context
    return s


def _mk_state():
    return PageState(url="https://x.com", title="T", interactive_elements=[],
                     viewport={"width": 800, "height": 600}, scroll_position={"x": 0, "y": 0})


def _user_text(messages):
    """取出 user 消息的文本(可能是 str 或多模态 list)。"""
    uc = messages[1]["content"]
    if isinstance(uc, str):
        return uc
    return " ".join(b.get("text", "") for b in uc if b.get("type") == "text")


def main() -> int:
    failures = []

    def check(name, cond):
        print(f"  {'✓' if cond else '✗'} {name}")
        if not cond:
            failures.append(name)

    print("=== M4 注入接线验证 ===")

    # 默认字段
    s0 = AgentSession(session_id="t", task="x", model="m")
    check("AgentSession.memory_context 默认空", s0.memory_context == [])
    check("AgentSession.memory_retrieved 默认 False", s0.memory_retrieved is False)

    ps = _mk_state()

    # 回归红线:无记忆 → 与"完全不带 memory_context"的输出一致
    s_none = _mk_session(memory_context=[], current_step=0)
    text_none = _user_text(build_messages(s_none, ps))
    check("无记忆:不含记忆块标题", "## 相关记忆" not in text_none)
    check("无记忆:仍含任务", "## 任务" in text_none)

    # 有记忆:首步注入
    mem = [
        {"memory_id": "mem_1", "content": "用户偏好键盘操作", "scope": "user", "domain": "", "score": 0.8},
        {"memory_id": "mem_2", "content": "提交按钮在页面底部", "scope": "domain", "domain": "x.com", "score": 0.7},
    ]
    s_mem = _mk_session(memory_context=mem, current_step=0)
    text_mem = _user_text(build_messages(s_mem, ps))
    check("有记忆:首步含记忆块标题", "## 相关记忆" in text_mem)
    check("有记忆:含用户偏好内容", "用户偏好键盘操作" in text_mem)
    check("有记忆:含站点事实内容", "提交按钮在页面底部" in text_mem)
    check("有记忆:domain 记忆带站点标签", "[x.com]" in text_mem)

    # 仅前两步注入:第 5 步不注入(省 token)
    s_late = _mk_session(memory_context=mem, current_step=5)
    text_late = _user_text(build_messages(s_late, ps))
    check("第5步:不再注入记忆块(省 token)", "## 相关记忆" not in text_late)

    # 第 1 步仍注入(current_step<=1)
    s_step1 = _mk_session(memory_context=mem, current_step=1)
    check("第1步:仍注入", "## 相关记忆" in _user_text(build_messages(s_step1, ps)))

    print(f"\n{'✅ 全部通过' if not failures else '❌ 失败: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
