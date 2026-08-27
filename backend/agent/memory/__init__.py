"""Chat 长期记忆子系统。

对齐 mem0:Qdrant 存记忆正文(事实源),SQLite 只做变更审计日志。
分层召回(对齐 MemGPT):persona/preference 常驻注入 + episodic 事件按需 recall。
对外统一走 service 门面:get_core_memories / recall_episodic / write_chat_memory。
"""
