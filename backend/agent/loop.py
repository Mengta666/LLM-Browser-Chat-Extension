"""Agent 主循环模块。

后续用于承载多步 tool calling、max_steps、重试和 trace 聚合。
当前 MVP 仍采用单次聊天请求内的直接工具编排。
"""
