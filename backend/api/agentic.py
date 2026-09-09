"""Agentic Loop 实现（对齐 Anthropic agent loop）。

循环调用 LLM 直到不再请求工具，支持 Tool Clearing（零 LLM 成本）压缩上下文。
"""
import json
import time
from typing import Any, Generator

from api.chat import (
    _llm_client,
    CHAT_LLM_TIMEOUT,
    AGENTIC_MAX_ROUNDS,
    AGENTIC_TOOL_CLEAR_THRESHOLD,
    AGENTIC_KEEP_RECENT_TOOLS,
    _chat_log,
)


def run_agentic_loop(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict],
    *,
    max_rounds: int = None,
    chat_id: str = "",
    stream: bool = False
) -> Generator[dict, None, None]:
    """
    Agentic loop: 循环调用 LLM 直到不再请求工具（对齐 Anthropic）。

    Args:
        model: 模型名
        messages: 初始对话历史
        tools: 可用工具列表 [WEB_SEARCH_TOOL, KB_SEARCH_TOOL]
        max_rounds: 最大轮数（默认用全局配置）
        chat_id: 用于日志
        stream: 是否流式（暂不支持流式，正文整段返回）

    Yields:
        {"type": "enhancement_step", "step": {...}}  # 工具调用进度
        {"type": "final", "content": "...", "messages": [...]}  # 最终回答
        {"type": "error", "content": "..."}  # 错误
    """
    if max_rounds is None:
        max_rounds = AGENTIC_MAX_ROUNDS

    working_messages = list(messages)

    for round_idx in range(max_rounds):
        _chat_log.debug("agentic_round", session_id=chat_id, data={"round": round_idx + 1})

        # 1. Tool Clearing: token 超限时清除旧 tool_result（零 LLM）
        tokens_est = _estimate_tokens(working_messages)
        if tokens_est > AGENTIC_TOOL_CLEAR_THRESHOLD:
            old_count = len(working_messages)
            working_messages = _clear_old_tool_results(working_messages, keep=AGENTIC_KEEP_RECENT_TOOLS)
            _chat_log.info("agentic_tool_cleared", session_id=chat_id, data={
                "round": round_idx + 1,
                "old_count": old_count,
                "new_count": len(working_messages),
                "tokens_before": tokens_est
            })

        # 2. 最后一轮强制不给 tools（防死循环，逼 LLM 出文本）
        is_last_round = (round_idx == max_rounds - 1)
        call_tools = None if is_last_round else tools

        # 3. 调用 LLM（非流式，中间轮需判断 tool_calls）
        try:
            resp = _llm_client.chat.completions.create(
                model=model,
                messages=working_messages,
                tools=call_tools,
                stream=False,
                timeout=CHAT_LLM_TIMEOUT
            )
        except Exception as exc:
            _chat_log.error("agentic_llm_failed", session_id=chat_id, data={
                "round": round_idx + 1,
                "error": str(exc)[:200]
            })
            yield {"type": "error", "content": f"LLM 调用失败: {str(exc)[:100]}"}
            return

        msg = resp.choices[0].message
        has_tool_calls = bool(msg.tool_calls)

        # 4. 无 tool_call → 最终回答，结束循环
        if not has_tool_calls:
            final_content = msg.content or ""
            yield {"type": "final", "content": final_content, "messages": working_messages}
            return

        # 5. 有 tool_call → 执行所有工具
        # 追加 LLM 决策（assistant 消息）
        working_messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls]
        })

        for tc in msg.tool_calls:
            tool_name = tc.function.name
            tool_args_str = tc.function.arguments

            # 解析参数
            try:
                args_dict = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                query = args_dict.get("query", "")
                kb_id_arg = args_dict.get("kb_id", "") if tool_name == "kb_search" else ""
            except Exception:
                query = ""
                kb_id_arg = ""

            # 发送 enhancement_step 事件（running）
            yield {
                "type": "enhancement_step",
                "step": {
                    "type": tool_name,
                    "status": "running",
                    "query": query,
                    "kb_id": kb_id_arg
                }
            }

            # 执行工具
            result_text, search_results = _execute_tool(tool_name, tool_args_str, args_dict)

            # 发送 enhancement_step 事件（done）
            yield {
                "type": "enhancement_step",
                "step": {
                    "type": tool_name,
                    "status": "done",
                    "query": query,
                    "kb_id": kb_id_arg,
                    "result_count": len(search_results) if search_results else 0
                }
            }

            # 追加工具结果
            working_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_text
            })

        # 6. 继续下一轮（循环回到顶部）

    # 7. 达到 max_rounds 仍未结束 → 强制终止
    _chat_log.warning("agentic_max_rounds_reached", session_id=chat_id, data={"max_rounds": max_rounds})
    yield {"type": "error", "content": f"达到最大轮数 {max_rounds}，强制结束"}


def _execute_tool(tool_name: str, tool_args_str: str, args_dict: dict) -> tuple[str, list]:
    """执行单个工具，返回 (result_text, search_results)"""
    if tool_name == "web_search":
        from search.tools import handle_tool_call
        return handle_tool_call(tool_name, tool_args_str)
    elif tool_name == "kb_search":
        from search.tools import handle_kb_search
        kb_id = args_dict.get("kb_id", "")
        query = args_dict.get("query", "")
        return handle_kb_search(kb_id, query)
    else:
        return f"未知工具: {tool_name}", []


def _clear_old_tool_results(messages: list[dict], keep: int = 3) -> list[dict]:
    """
    Tool Clearing（对齐 Anthropic）：清除旧 tool 消息内容，保留最近 keep 个。
    纯字符串操作，零 LLM 调用。

    Args:
        messages: 当前对话历史
        keep: 保留最近 N 个工具结果

    Returns:
        压缩后的 messages
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= keep:
        return messages

    to_clear = set(tool_indices[:-keep])  # 除最近 keep 个
    result = []
    for i, msg in enumerate(messages):
        if i in to_clear:
            result.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": "[工具结果已清除以节省上下文]"
            })
        else:
            result.append(msg)
    return result


def _estimate_tokens(messages: list[dict]) -> int:
    """粗估 token 数（字符数 / 1.5）。统计 content + tool_calls。"""
    total_chars = 0
    for m in messages:
        # content 字段
        total_chars += len(str(m.get("content", "")))
        # tool_calls 字段（assistant 消息）
        if m.get("tool_calls"):
            import json
            total_chars += len(json.dumps(m["tool_calls"]))
    return int(total_chars / 1.5)
