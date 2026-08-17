# Memory Writer Examples

Input: `用户默认希望中文回答，偏好简洁直接。`
Output: `user_profile`, tags `["language_preference","answer_style"]`

Input: `你以后都需要从专业角度、全面地进行回答问题。`
Output: `user_profile`, tags `["answer_style","detail_level"]`

Input: `当前关注 Sprint 4 memory。`
Output: `project_state`, tags `["progress"]`

Input: `下一步要设计 memory_items 和后台 writer。`
Output: `task_state`, tags `["next_step","todo"]`

Input: `不要为特例写特例代码；先结合代码再回答。`
Output: `procedural_feedback`, tags `["code_design_rule","workflow_rule"]`

Input: `web search 已执行但没展示，是因为最终引用过滤只保留模型实际引用 sources。`
Output: `episodic_lesson`, tags `["retrieval_issue","citation_issue"]`

Input: `LangMem 文档适合作为 memory policy 设计参考。`
Output: `external_knowledge_ref`, tags `["doc_reference","framework_reference"]`

Counterexample: `这次回答请详细一点。`
Output: `noop`, unless the user explicitly says this should apply in future turns.

Counterexample: `以后回答要专业全面。`
Output: `user_profile`, not `procedural_feedback`, because this is a durable answer style and detail-level preference.

Counterexample: `下一步做 task_state 生命周期。`
Output: `task_state`, not `procedural_feedback`, because this is a current TODO rather than a workflow correction.

Counterexample: `不要为特例写特例代码；先结合代码再回答。`
Output: `procedural_feedback`, not `task_state`, because this is a durable working rule for coding/debugging.

Counterexample: Assistant says `用户可能喜欢长回答。`
Output: `noop`, because this is assistant speculation.

Counterexample: User says `请专业全面回答。` and assistant answers with `底层 -> 架构 -> 实现 -> 优化`.
Output: `user_profile` may store only `用户偏好专业、全面的回答。`; do not store the assistant's framework.

Counterexample: Search result says `Qwen3 Embedding supports 100 languages.`
Output: `noop`, because external facts belong in sources/RAG, not user memory.
