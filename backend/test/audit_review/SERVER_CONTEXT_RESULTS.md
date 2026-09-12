# 长会话改造与验收

日期：2026-09-12。分支：`agent-slim`，基线：`c23f882`。本次修改尚未提交或推送。

## 结论

本次范围是普通聊天的服务端会话上下文，不是浏览器自动化循环或全部知识库缺陷的重写。
新协议、真实模型摘要、真实知识库问答、实际 Chromium 扩展恢复续聊均已验证。
后端最终重启进程为 **8772**，监听 `http://127.0.0.1:8000`。
全仓旧测试并非全绿，已知失败和失效测试单独列在下方。

## 已实现

- 前端探测 `/v1/sessions/capabilities`；本项目后端使用服务端模式，每次只提交当前用户输入。第三方接口保留旧直连模式。
- SQLite 分配稳定的 `seq`，历史消息是上下文的事实来源。新模式不再依赖前端截取、拼接完整历史。
- `request_id` 保证请求幂等；重复成功请求回放原答案和引用，不重复推理、写消息或提取记忆。
- `expected_last_seq` 检测历史冲突。同一会话只接受一个运行中的请求，失败重试增加 `attempt`，旧尝试不能补写答案。
- 用户输入先落库；回答成功落库后才发 `session_meta.persisted=true`。断流、失败、超时不能被前端当成已保存。
- 请求状态查询、失败重试、进程重启后的 `interrupted` 恢复。后面已有新消息时，不允许回头重试旧失败请求。
- 摘要使用 `summary_upto_seq` 和版本比较更新；已摘要部分不再重复发送，保留最近 3 个完整问答和当前输入。
- 长消息完整分批处理，不再截取每条前 2000 字。任意批失败不发布摘要、不推进游标、不删除原文。
- 摘要超长时仅额外尝试一次精简，仍不合格则放弃本次发布；不是直接截断正文。
- 统一拼装唯一开头 system，保留基础提示、记忆提示、知识库绑定和摘要；工具参数、结果、schema 计入最终预算。
- 工具循环每次调用模型前再次检查预算；超预算拒绝发送。模型返回 `finish_reason=length` 不确认成完整成功答案。
- 服务端历史分页，每页最多 200 条；面板默认载入最近 100 条，可加载更早消息。旧接口无分页调用不再截在前 200 条。
- 手动搜索保留原来“自动搜索关闭也可主动搜索”的行为，并保存/回放引用；后续工具引用编号接续。

长期记忆/core 提取仍为独立流程，不把本会话摘要提升成跨会话 core；本次没有修改记忆晋升规则。

## 实际配置核验

远端 `/v1/models` 对 `qwen3.8-27b` 返回 `max_model_len=65536`。
原 `.env` 未显式设置 `CHAT_CONTEXT_LENGTH`，实际使用代码默认的 128000。
本次只在真实 `.env` 新增 **`CHAT_CONTEXT_LENGTH=65536`**，其他已有值不变，未编辑 `.env.example`。

| 参数 | 当前值/默认值 | 用途 |
| --- | ---: | --- |
| `CHAT_CONTEXT_LENGTH` | 65536，真实 env 显式设置 | 当前部署模型窗口 |
| `CHAT_MAX_OUTPUT_TOKENS` | 4096，新增默认 | 普通回答生成预算 |
| `CHAT_CONTEXT_SAFETY_TOKENS` | 2048，新增默认 | 请求安全余量 |
| `CHAT_COMPACT_MAX_OUTPUT_TOKENS` | 4096，新增默认 | 摘要模型生成预算，包含可能的推理开销 |
| `CHAT_COMPACT_SUMMARY_MAX_TOKENS` | 800 | 最终摘要正文估算上限 |

最终进程日志确认 `input_budget=59392`；软阈值为输入预算的 70%，硬阈值为 90%。
只有服务端普通会话的扩展后台总等待上限改为 600 秒，面板为 605 秒；旧直连和浏览器自动化时限未改。

真实测试发现：把模型生成预算也限制为 800 时，模型返回 `length`、正文为空。
因此将生成预算与摘要正文预算分离；并非把最终摘要放宽到 4096。

## 验收结果

### 隔离回归

- `pytest backend/test/audit_review`：**54 passed，19 xfailed**。其中新增服务端专项 **29 项全部通过**。
- 新前端协议测试：**6/6**；保留的知识库刷新按钮测试：**8/8**。
- 两个扩展 JavaScript 文件语法检查通过；`git diff --check` 无空白错误。
- 服务端专项涵盖：真实 SQLite 迁移与备份、幂等、并发排他、失败重试、旧尝试拒绝、写库故障、SSE 完成确认顺序、断流、重启恢复、201/260/520/5002 条分页、摘要版本竞争、中间批失败、长文本尾部、同步压缩的基础/KB 提示、图片当前轮保留、工具参数与结果预算、超长输入和截断输出、手动搜索引用回放。
- 模型边界使用真实 OpenAI SDK 的 MockTransport；存储用临时 SQLite 和内存 Qdrant，并阻止外部网络。不冒充真实模型测试。

### 真实服务与浏览器

- 最终重启后，`audit_context_026dcb965183` 完成两轮真实模型对话：早期编号和颜色可回忆、重复请求原样回放、仅 4 条消息、旧 seq 请求返回 409。
- `audit_context_kb_ebf92506373d` 查询原合成测试库 `kb_171d1dfb`：流式答案命中轴承 `RB-208`，工具事件正常，答案确认落库。
- 在独立 Chromium 中加载原扩展文件，经过真实 service worker、Chrome 消息通道和本地 HTTP 后端；不是网页 API 桩替代。第一轮后重新加载面板，通过会话抽屉恢复，再问第二轮，正确回答 `UI-204` 和蓝色。
- 扩展测试会话 `audit_context_ui_20260912` 4 条消息、`last_seq=4`、无运行占用；控制台 **0 errors / 0 warnings**。
- 截图：`output/playwright/context-resumed.png`。个人浏览器资料和正在使用的扩展配置未动。
- 真实分批摘要连续两次通过。最终 65536 配置的一次：约 **17662 → 940** 个估算 token，4 批处理、摘要约 681 token、覆盖到 seq 2、保留尾部 6 条。随后真实问答正确恢复 `ZX-491`、紫色及禁止删除的约束。
- 大文本摘要使用独立临时 SQLite，不写真实会话/长期记忆库。临时测试目录前缀为 `browser-agent-compress-`。
- `/openapi.json`、`/v1/kb`、`/v1/sessions/list` 均 HTTP 200。以上结果不等价于 GPU 极限压力测试。

## 数据保护

- 修改前备份：`backend/data/chat_history.before_server_context.20260912T162534Z.sqlite3`；迁移还会自动保留独立备份。
- 原来的 **38 条消息逐字段比对一致**；原摘要字段保留，不直接信任旧计数游标为新摘要边界。
- 最终主历史库 60 条消息，新增 22 条均来自本次合成验收会话；未删除用户历史、原知识库或文档。
- SQLite `integrity_check=ok`，全部消息均有 seq，会话 last_seq 与消息最大 seq 一致。
- 真实 `.env` 和 SQLite 文件均被 Git 忽略；没有提交、推送或上传凭据。
- 不自动恢复旧备份覆盖当前库，否则会丢失迁移后产生的消息。若回退，先停后端并另存当前库，再决定数据保留方案。

## 尚未解决/未覆盖的边界

- **19 个 xfail 是仍然失败的旧问题，不是通过**：包括旧 client 协议的压缩路径、旧请求总时限、知识库并发/精排/删除一致性、非法工具参数、引用 HTML 等，详见原审计报告。扩展接入本项目后端会走新 server 协议，但外部调用者仍需主动升级协议。
- 扩展检查之外另运行了旧页面测试：`test_page_identity.py` 在收集时缺少 `common.page_identity`；`test_rag_refresh_flow.py` 7 项缺少 `storage.db`、`tools.page_retrieval`、`api.pages`。已用 Git 树确认这些模块在基线分支中也不存在，没有删模块或掩盖失败。
- 未运行会预先删除固定远端 collection 的历史 M1/M2 手工脚本，也未重写这些失效旧测试。不能宣称整个仓库测试全部通过。
- token 数仍是估算，不是 Qwen 精确 tokenizer；图片使用共享估算，未做大图/GPU 极限压力验证。切换部署模型后需要重新核对窗口配置。
- 历史图片不持久化；本轮图片可送入模型，含图失败请求关闭面板后需重新提供图片。文本失败请求可通过状态接口恢复原请求。
- 当前恢复机制面向单后端进程；不支持多个 worker 共用该 SQLite 时的跨进程请求协调。
- 摘要会损失细节，保留完整历史并不等价于模型每轮能访问全部历史；不承诺任意旧细节都能精准回忆。

## 复现命令

在仓库根目录执行（真实测试会新建合成会话）：

```powershell
.\backend\.venv\Scripts\python.exe -X utf8 -B -m pytest backend/test/audit_review -q -p no:cacheprovider
node backend/test/audit_review/server_frontend.test.cjs
node backend/test/audit_review/kb_refresh.test.cjs
.\backend\.venv\Scripts\python.exe -X utf8 -B backend/test/audit_review/live_server_context.py
.\backend\.venv\Scripts\python.exe -X utf8 -B backend/test/audit_review/live_context_compression.py
.\backend\.venv\Scripts\python.exe -X utf8 -B backend/test/audit_review/verify_context_state.py
```

你的日常扩展需要在扩展管理页点一次“重新加载”，再重新打开侧栏；这不影响已保存的聊天历史。
