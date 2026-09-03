"""Chat 单会话对话压缩(Context Compaction)。

与 core 记忆摘要不同——这里是"消息数组瘦身",不是长期记忆合并。
触发点:api/chat.py 转发前(同步兜底或后台预压缩)。
产物:chat_sessions.summary(字符串,含 5 段结构化中文摘要)。

增量融合:用 summary_msg_count 作游标(单调递增),每次只压新增消息,
旧摘要作为上下文传给 LLM 做融合更新。LLM 失败不推进游标,下次重试。
"""

import os
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from agent.memory.config import (
    CHAT_COMPACT_KEEP_PAIRS,
    CHAT_COMPACT_SUMMARY_MAX_TOKENS,
    CHAT_CONTEXT_LENGTH,
    CHAT_COMPACT_TRIGGER_RATIO,
    CHAT_COMPACT_HARD_RATIO,
)
from agent.token_utils import estimate_tokens, estimate_text_tokens
from observability.logger import get_logger

_log = get_logger("chat_compact")

__env_path = Path(__file__).resolve().parents[2] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

_MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_COMPACT_MODEL = os.getenv("MEMORY_MODEL") or os.getenv("AGENT_MODEL") or "gpt-4o"

_llm_client = OpenAI(base_url=_MODEL_BASE_URL, api_key=_OPENAI_API_KEY)


# ═══════════════════════════════════════════════════════════════════════════════
# 压缩 Prompt
# ═══════════════════════════════════════════════════════════════════════════════

CHAT_COMPACT_SYSTEM_PROMPT = f"""你是对话压缩器,把一段用户与AI助手的历史对话压缩成结构化摘要,供未来对话继续时作为上下文。

如果给你了"上一版摘要",请在此基础上"融合更新",不要丢失其中的关键信息;
如果没有上一版摘要,则从零生成。

输出必须严格分为以下 5 段(不要多,不要少),每段以 ## 开头:

## 对话目标
用户的核心诉求、正在做的事、想解决的问题(1-2 句)。

## 关键交互
按时间顺序,列出重要的问答/决策/技术选型(每条一行,不要罗列寒暄)。
必须原文保留:文件名、函数名、库名、URL、错误信息、用户明确要求。

## 重要细节
用户偏好、约束、上下文(如"用中文回答"、"项目用 Go"、"已经试过 X 方案")。
决策的理由,尤其是"为什么不选 Y"。

## 用户纠正
用户明确纠正或改变主意的时刻(原文引用最有价值的几处)。
这一段防止未来对话再犯同样错误。

## 待做事项
用户提到但未完成的任务、下一步计划、悬而未决的问题。
如果没有,写"无"。

要求:
- 用中文;
- 客观陈述,不要评论、不要"总结起来";
- 不要生成指令性内容(不要"接下来应该""建议"),只是记录;
- 严禁使用 ``` 代码块包裹(会破坏后续拼装);
- 总长控制在 {CHAT_COMPACT_SUMMARY_MAX_TOKENS} tokens 以内。"""


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 调用(纯文本输出,非 JSON)
# ═══════════════════════════════════════════════════════════════════════════════

def _summarize_llm(system_prompt: str, user_prompt: str) -> str:
    """调 LLM 生成摘要(纯文本)。失败返回空串。"""
    try:
        resp = _llm_client.chat.completions.create(
            model=_COMPACT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            timeout=90,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        _log.error("compact_llm_failed", data={"error": str(exc)[:200]})
        return ""


def _build_compact_user_prompt(prev_summary: str, evicted_msgs: list[dict]) -> str:
    """拼装 user prompt:上一版摘要 + 被驱逐消息列表。"""
    parts: list[str] = []
    if prev_summary:
        parts.append("【上一版摘要(请在此基础上融合更新)】")
        parts.append(prev_summary)
        parts.append("")

    parts.append("【需要压缩的新增对话】")
    for m in evicted_msgs:
        role = "用户" if m.get("role") == "user" else "助手"
        content = str(m.get("content", "")).strip()
        if content:
            parts.append(f"{role}:{content[:2000]}")

    parts.append("")
    parts.append("请输出 5 段结构化摘要。")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# 并发控制
# ═══════════════════════════════════════════════════════════════════════════════

_compact_in_progress: set[str] = set()
_compact_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# 核心压缩函数
# ═══════════════════════════════════════════════════════════════════════════════

def compact_chat(chat_id: str, *, force: bool = False) -> dict[str, Any]:
    """核心入口。用 summary_msg_count 作游标,增量融合。

    force=True 直接压缩(同步兜底 / 后台预压缩);
    force=False 先判 token 是否达阈值。
    返回 {compacted, reason?, before_tokens?, after_tokens?, msg_count_before?, msg_count_after?}。
    """
    from storage import chat_store

    if not chat_id:
        return {"compacted": False, "reason": "no_chat_id"}

    # 并发保护:同 chat_id 只允许一个在跑
    with _compact_lock:
        if chat_id in _compact_in_progress:
            _log.info("chat_compact_skipped", data={"chat_id": chat_id, "reason": "in_progress"})
            return {"compacted": False, "reason": "in_progress"}
        _compact_in_progress.add(chat_id)

    t0 = time.time()
    try:
        all_msgs = chat_store.get_messages(chat_id, limit=5000)
        prev = chat_store.get_summary(chat_id)
        n = len(all_msgs)

        if n == 0:
            return {"compacted": False, "reason": "empty"}

        # 起止位置
        start = prev["msg_count"]
        keep_tail = CHAT_COMPACT_KEEP_PAIRS * 2
        end = n - keep_tail

        if end <= start:
            _log.info("chat_compact_skipped", data={
                "chat_id": chat_id, "reason": "no_evicted",
                "total": n, "start": start, "end": end, "keep_tail": keep_tail})
            return {"compacted": False, "reason": "no_evicted"}

        evicted = all_msgs[start:end]

        # 非 force 时检查 token 阈值
        if not force:
            tok = estimate_tokens(
                [{"role": "system", "content": prev["summary"]}] + all_msgs
                if prev["summary"] else all_msgs
            )
            ratio = tok["total"] / max(1, CHAT_CONTEXT_LENGTH)
            if ratio < CHAT_COMPACT_TRIGGER_RATIO:
                return {"compacted": False, "reason": "below_threshold",
                        "ratio": round(ratio, 3), "tokens": tok["total"]}

        # 调 LLM 压缩
        _log.info("chat_compact_triggered", data={
            "chat_id": chat_id, "mode": "force" if force else "threshold",
            "total_msgs": n, "evicted_count": len(evicted),
            "prev_msg_count": start, "new_end": end})

        user_prompt = _build_compact_user_prompt(prev["summary"], evicted)
        new_summary = _summarize_llm(CHAT_COMPACT_SYSTEM_PROMPT, user_prompt)

        if not new_summary:
            _log.warn("chat_compact_failed", data={
                "chat_id": chat_id, "error": "llm_returned_empty"})
            return {"compacted": False, "reason": "llm_failed"}

        # 写入(覆盖式)
        chat_store.set_summary(chat_id, new_summary, end)

        # 计算压缩后 token
        recent = all_msgs[end:]
        after_tok = estimate_tokens(
            [{"role": "system", "content": new_summary}] + recent
        )
        before_tok = estimate_tokens(all_msgs)

        latency_ms = int((time.time() - t0) * 1000)
        _log.info("chat_compact_done", data={
            "chat_id": chat_id,
            "before_tokens": before_tok["total"],
            "after_tokens": after_tok["total"],
            "msg_count_before": start,
            "msg_count_after": end,
            "latency_ms": latency_ms})

        return {
            "compacted": True,
            "before_tokens": before_tok["total"],
            "after_tokens": after_tok["total"],
            "msg_count_before": start,
            "msg_count_after": end,
            "latency_ms": latency_ms,
        }

    except Exception as exc:
        _log.error("chat_compact_failed", data={
            "chat_id": chat_id, "error": str(exc)[:200]})
        return {"compacted": False, "reason": f"error: {str(exc)[:100]}"}
    finally:
        with _compact_lock:
            _compact_in_progress.discard(chat_id)


def schedule_background_compact(chat_id: str) -> None:
    """后台线程触发一次压缩。同 chat_id 并发时 skip 后来者。"""
    if not chat_id:
        return

    def _worker():
        compact_chat(chat_id, force=True)

    try:
        threading.Thread(
            target=_worker, daemon=True,
            name=f"chat-compact-{chat_id[:16]}",
        ).start()
    except Exception:
        pass


def needs_compact(messages: list[dict], summary: str = "") -> str:
    """判断消息列表是否需要压缩。返回 "" / "async" / "sync"。

    "" = 不需要;
    "async" = 达到 70%,后台预压缩;
    "sync" = 达到 90%,同步兜底。
    """
    all_msgs = messages
    if summary:
        all_msgs = [{"role": "system", "content": summary}] + list(messages)
    tok = estimate_tokens(all_msgs)
    ratio = tok["total"] / max(1, CHAT_CONTEXT_LENGTH)
    if ratio >= CHAT_COMPACT_HARD_RATIO:
        return "sync"
    if ratio >= CHAT_COMPACT_TRIGGER_RATIO:
        return "async"
    return ""
