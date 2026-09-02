# -*- coding: utf-8 -*-
"""联网搜索 tool 定义:schema(注册给 LLM)+ 执行分发 + 结果格式化。

chat.py 只 import 四个东西:
- WEB_SEARCH_TOOL: tool schema dict
- handle_tool_call(name, arguments): 执行 tool,返回 (结果字符串, SearchResult 列表)
- format_search_results_for_manual(results): 手动搜索注入 system 的格式
- SEARCH_ENABLED: 开关
"""

import json

from search import search_web, SearchResult, SEARCH_ENABLED, SEARCH_RESULT_COUNT

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current/real-time information. "
                       "Use when the user asks about recent events, prices, weather, news, "
                       "or facts you're not confident about.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query keywords (concise, suitable for search engine)"
                }
            },
            "required": ["query"]
        }
    }
}


def handle_tool_call(name: str, arguments: str) -> tuple[str, list[SearchResult]]:
    if name != "web_search":
        return f"未知工具: {name}", []
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (json.JSONDecodeError, TypeError):
        return "搜索参数解析失败", []
    query = str(args.get("query", "")).strip()
    if not query:
        return "搜索词为空", []
    results = search_web(query, count=SEARCH_RESULT_COUNT)
    return format_search_results(results), results


def format_search_results(results: list[SearchResult]) -> str:
    if not results:
        return "未找到相关搜索结果。请基于你已有的知识回答用户问题。"
    lines = [
        "以下是网络搜索结果,请参考回答用户问题。",
        "在回答中用 [1][2] 等标注你引用了哪条搜索结果。\n",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.title}")
        if r.snippet:
            lines.append(f"    {r.snippet}")
        lines.append(f"    URL: {r.url}")
        lines.append("")
    return "\n".join(lines)


def format_search_results_for_manual(results: list[SearchResult]) -> str:
    if not results:
        return "用户请求搜索,但未找到相关结果。请基于你已有的知识回答。"
    lines = [
        "用户主动搜索了以下信息,请参考回答。用 [1][2] 标注引用来源。\n",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.title}")
        if r.snippet:
            lines.append(f"    {r.snippet}")
        lines.append(f"    URL: {r.url}")
        lines.append("")
    return "\n".join(lines)
