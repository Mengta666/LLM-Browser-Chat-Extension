# 审计复核与可执行用例

本文保留第一轮的环境与结果；后续真实服务测试见 [LIVE_RERANK_RESULTS.md](LIVE_RERANK_RESULTS.md)，第二轮 44 项累计用例及新增结论见 [DEEP_AUDIT_RESULTS.md](DEEP_AUDIT_RESULTS.md)。不要将下方第一轮的空知识库、配置和测试数量当成当前状态。

2026-09-16：F2 / F11 / F12 / F13 和关联 F4 已完成修复及回归，最新范围、测试数量和真实流程见 [KB_LIFECYCLE_RESULTS.md](KB_LIFECYCLE_RESULTS.md)；下方保留原始审计证据。

审计基线：`agent-slim@c23f882`。只新增测试，未修复业务代码，未修改真实 `.env`。

## 运行

在仓库根目录执行：

```powershell
.\backend\.venv\Scripts\python.exe -X utf8 -B -m pytest backend/test/audit_review/test_backend.py backend/test/audit_review/test_frontend.py -q --runxfail -p no:cacheprovider --tb=short
```

本次结果：**25 个用例，11 passed、14 failed**。14 个失败是实际的正确行为断言不满足，覆盖多个场景，并非 14 个独立缺陷；没有导入、网络或测试装置错误。

默认去掉 `--runxfail` 时，已知缺陷标记为严格的 `xfail`：结果为 `11 passed、14 xfailed`。这不表示修复完成；修复后相应用例会出现严格 `XPASS`，需同步移除标记。

检查已经启动的后端：

```powershell
.\backend\.venv\Scripts\python.exe -X utf8 -B backend/test/audit_review/live_readonly.py
```

依赖：项目后端依赖、pytest、httpx、lxml，以及 Node.js。本次未安装依赖。

## 测试边界

- 后端：直接导入真实 `api.chat`、`api.agentic`、`rag.kb`、`rag.reranker`、`agent.memory.vector`、`chat_compact` 和 SQLite 存储模块，不摘抄或改写业务函数。
- HTTP 路由通过 FastAPI TestClient 调用，模型响应通过 OpenAI SDK 的 `httpx.MockTransport` 注入；因此测到了真实请求组装、SDK 响应解析和 SSE 错误行为，但不是实际模型端到端测试。
- 存储使用真实临时 SQLite 和 Qdrant Client 的 `:memory:` 实现，不使用运行中服务的数据。
- `.env` 加载被禁用；外部 socket 连接被禁止，仅放行 Windows 事件循环自身的 socketpair；模型使用无效测试凭据和保留域名。
- 全局记忆注入、会话标题/异步记忆写入不在本次用例范围，已禁用。索引并发测试保留真实上传路由、TXT 解析、后台线程、删除路由和存储，仅替换切片与嵌入边界，以 Event 确定线程交错时序。当前测试解释器缺少 `langchain_text_splitters`，没有把这个环境问题计为业务缺陷。
- 前端：Node VM 执行原始完整函数，替换 Chrome 消息、fetch 和时钟；常量仍为原始 120000 毫秒，没有改业务函数。HTML 结果另由 lxml 解析验证。
- 未运行完整 Chrome 扩展，也未进行 CSP 绕过或实际恶意搜索站点实验；不声称可执行 XSS。
- 临时目录为系统临时目录下独立的 `browser-agent-audit-*`，仅包含合成测试数据。可通过 `AUDIT_TEST_TEMP` 指定已有的测试输出根目录；不覆盖或清理用户现有数据。
- 未运行旧的真实模型/远端 Qdrant 写入测试。

## 对上一轮结论的逐项复核

| 编号 | 用例证据 | 修正后的结论 |
| --- | --- | --- |
| F1 压缩后仍发送原始历史 | 20/60 条已存消息分别完成摘要，游标为 14/54；新请求仍发送 21/61 条原始消息，本应仅余摘要和 7 条未压缩消息。重新打开会话、按摘要游标恢复 tail 的对照正常。 | 确认缺陷，但不是所有压缩路径都无效；不能据此声称当前模型已经爆上下文。 |
| F2 删除索引中的文档 | 在实际上传线程等待嵌入时，通过 DELETE 路由删除；恢复线程后，SQLite 文档已软删、列表为空，但 Qdrant 仍有 1 条 valid=true 片段。已完成索引后再删除的对照正常。 | 确认并发缺陷，触发条件是删除和索引交错，不是普通删除必然失败。 |
| F3 120 秒超时 | 原始后台和面板函数在第 40/80 秒收到进度；100 秒最终回答正常，130 秒最终回答在第 120 秒被中断/停止监听。 | 确认总时限行为。若产品本就规定总请求必须 120 秒内完成，它是限制而非超时值本身的 bug；与长 Agentic 任务是否冲突需明确产品预算。不能认定当前模型必然超时，撤回直接标为 P1 的笼统判断。 |
| F4 邻居扩展 | 真正写入 Qdrant 的 0/1/2 三个片段可正常 scroll；查询邻居 1 却返回 None；完整 search_kb 的 window_size=1。 | 确认缺少 query_vector 导致邻居扩展失效。 |
| F5 精排分数 | 真实 reranker 函数接收 relevance_score=0，完整检索仍以原始 RRF 0.5 放行。另一个量测确认余弦 0 与 RRF 0.5 可同时出现。 | 零分回退是确认缺陷。当前 `.env` 为 KB_RERANK_ENABLED=false，所以精排分支未启用。RRF 量测只证明排名分不是余弦分，不能仅凭合成向量判定真实语义检索错误；绝对阈值应视为需标定的设计风险。 |
| F6 非法工具参数 | 正常工具参数在流式、非流式均经过两轮完成；首轮损坏 JSON 时，非流式 502，流式 200 但 SSE finish_reason=error，均含 args_dict 未赋值异常。 | 确认错误处理缺陷，前提是模型返回非法参数；没有声称真实模型已经返回过这类参数。 |
| F7 同步压缩丢提示 | 真实同步压缩完成后，捕获实际 OpenAI SDK 发出的 messages，KB ID 和基础提示均不在其中；短对话正常保留。 | 确认请求组装缺陷。是否让特定模型检索失败没有用真实模型验证。 |
| F8 引用 HTML | 带引号的合成 URL 可在真实引用函数输出中注入额外 span，lxml 确认其为节点；已有 href 中的 [1] 也被替换并损坏。正常 URL 对照通过。 | 确认 HTML 拼接及替换范围错误。恶意 URL 的真实来源可达性、Chrome CSP 下脚本执行均未证实，不升级为可执行 XSS。 |

## 本轮新增确认：F9 同步压缩丢失一条未摘要消息

代码位置：`backend/api/chat.py:282-284`，与 `backend/agent/memory/chat_compact.py:162-172` 配合产生。

数据库有 20 条旧消息，保留 3 对，则摘要输入覆盖旧消息 0–13。此时请求已经加入最新用户消息，共 21 条；从请求截最后 6 条只能拿到旧消息 15–19 和最新用户消息。**旧消息 14 既没进入摘要，也没发给模型**。

`test_sync_compact_does_not_drop_unsummarized_boundary_message` 同时检查真实摘要输入与最终请求，确认仅 `audit-14` 在两边都缺失。这是数据遗漏，不能用“压缩允许丢细节”解释，因为该消息根本未经过摘要器。

## 已启动后端的只读检查

本轮检查前后：`/openapi.json`、`/v1/kb`、`/v1/sessions/list` 均为 HTTP 200；OpenAPI 有 23 条路径，知识库 0 个，会话 6 个。检索/压缩完成的内存事件查询为空。因此不存在可对照的当前 KB 检索实例，不把隔离测试结果冒充生产现场故障。

真实 `.env` 只读取白名单非敏感字段：`SEARCH_ENABLED=0`、`KB_RERANK_ENABLED=false`、`KB_RECALL_MIN_SCORE=0.5`。这是磁盘配置，不保证运行进程没有额外环境变量覆盖。

这是一轮针对先前发现的回归复核，不是全仓库安全认证，也没有验证模型实际输出质量、Qdrant 服务器版本差异或 GPU 上下文承载能力。
