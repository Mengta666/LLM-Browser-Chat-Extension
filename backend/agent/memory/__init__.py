"""浏览器 Agent 长期记忆子系统。

对齐 mem0:Qdrant 存记忆正文(事实源),SQLite 只做变更审计日志。
对外统一走 service 门面:retrieve_for_task / write_after_task。
"""
