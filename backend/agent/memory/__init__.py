"""浏览器 Agent 长期记忆子系统。

对齐 mem0:Qdrant 存记忆正文(事实源),SQLite 只做变更审计日志。
分层召回(对齐 MemGPT):偏好常驻注入 + 站点经验/教训按需 recall。
对外统一走 service 门面:get_resident_preferences / recall_site_experience / write_after_task。
"""
