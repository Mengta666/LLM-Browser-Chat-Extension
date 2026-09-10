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


def handle_tool_call(name: str, arguments: str, start_index: int = 1) -> tuple[str, list[SearchResult]]:
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
    return format_search_results(results, start_index=start_index), results


def format_search_results(results: list[SearchResult], start_index: int = 1) -> str:
    if not results:
        return "未找到相关搜索结果。请基于你已有的知识回答用户问题。"
    lines = [
        "以下是网络搜索结果,请参考回答用户问题。",
        "在回答中用 [N] 标注你引用了哪条搜索结果。\n",
    ]
    for i, r in enumerate(results):
        num = start_index + i
        lines.append(f"[{num}] {r.title}")
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


# ─── KB Search Tool (批次 G) ─────────────────────────────────────


KB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "kb_search",
        "description": (
            "从用户绑定的知识库检索相关片段。当用户的问题涉及他们上传的文档"
            "(如技术手册、合同、研究报告等)时使用。返回若干带编号的文档片段,"
            "回答时引用编号 [N]。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kb_id": {"type": "string", "description": "知识库标识"},
                "query": {"type": "string", "description": "检索关键词"}
            },
            "required": ["kb_id", "query"]
        }
    }
}


def handle_kb_search(kb_id: str, query: str, start_index: int = 1) -> tuple[str, list[dict]]:
    """执行 KB 检索,返回 (格式化结果字符串, chunks 列表)。

    chunks 列表供 chat.py _sse_search_meta 推送元数据(前端引用面板)。
    """
    try:
        from rag import kb as KB
    except ImportError:
        return "知识库模块未加载", []

    if not kb_id or not query:
        return "kb_id 或 query 为空", []

    try:
        chunks = KB.search_kb(kb_id, query.strip())
    except Exception as exc:
        return f"知识库检索失败: {exc}", []

    return _format_kb_chunks(chunks, start_index=start_index), chunks


def _format_kb_chunks(chunks: list[dict], start_index: int = 1) -> str:
    """格式化 KB chunks 为带编号的引用格式,供 LLM 引用。"""
    if not chunks:
        return "未在知识库中找到相关片段。请基于你已有的知识回答用户问题。"

    lines = [
        "以下是知识库中检索到的相关片段,请参考回答用户问题。",
        "在回答中用 [N] 标注你引用了哪个片段。",
        "不要在编号之外添加参考或来源字样。\n",
    ]
    for i, chunk in enumerate(chunks):
        num = start_index + i
        source = chunk.get("source", "unknown")
        chunk_idx = chunk.get("chunk_idx", 0)
        content = chunk.get("content", "")
        lines.append(f"[{num}] doc=\"{source}\" chunk#{chunk_idx}:\n{content}")
        lines.append("")

    return "\n".join(lines)

