# Browser Agent Chrome 扩展

本目录是 Browser Agent 的 Manifest V3 侧边栏扩展。完整架构、后端配置、接口和回归说明见 [项目 README](../README.md)，已知问题汇总于仓库根目录下的本地 `docs/TODO.md`（暂不提交）。

扩展提供普通聊天、图片 / 框选截图、历史会话、知识库管理及浏览器自动化。完整功能需要连接本项目 FastAPI 后端；直接连接第三方模型接口时，不会自动获得本项目的会话、知识库或自动化 API。

## 安装与连接

1. 按 [后端启动说明](../README.md#快速开始) 启动服务。
2. 打开 `chrome://extensions`，开启“开发者模式”。
3. 点击“加载已解压的扩展程序”，选择**当前 extension 目录**（包含 `manifest.json`），不要选择仓库根目录。
4. 打开扩展侧边栏，进入设置页。
5. 使用本机后端时，API Base URL 填 `http://127.0.0.1:8000/v1`，模型填上游实际可用的模型名。本机 / 内网后端允许前端 Key 留空。
6. 保存后打开普通网页进行测试；浏览器内置页、扩展页和部分受限页面不支持读取、截图或自动化。

前端 Base URL 是扩展要连接的服务地址，不是后端调用上游模型的地址。上游模型地址及密钥在 `backend/config/.env` 配置；前端 API Key 不会覆盖后端的 `OPENAI_API_KEY`。

扩展不需要 npm 构建，生产运行通过 Chrome 的 CDP 接口执行自动化，不需要安装 Playwright。修改扩展文件后，要在扩展管理页重新加载，再关闭并重新打开侧边栏；只刷新目标网页不够。

自动化协议已升级到 v2，需要同时重启新版后端并重新加载扩展。观察失败自动恢复、不沿用旧状态；停止会阻止后续输入，无法确认是否完成的旧动作不会自动重放。任务始终绑定启动时的标签页，具体预算与限制见项目 README。

## 主要操作

- **普通聊天**：关闭自动化，发送文本或图片；支持 Markdown、公式和流式响应。具体图片 / 工具能力由模型服务决定。
- **网页与截图**：可读取网页文本辅助提问，或框选截图作为附件；这不是旧版网页快照自动索引功能。
- **浏览器自动化**：启用自动化并输入任务。扩展负责观察、执行动作和回传结果，后端逐步决策；需要保持扩展流程运行。
- **历史会话**：连接本项目后端时，使用服务端保存的历史及恢复协议。第三方模型服务的兼容聊天不具备相同保证。
- **知识库**：创建库、上传文档，等待索引状态变为 `indexed`，在聊天输入区域选择对应知识库。外部新建的库可用刷新按钮同步。
- **搜索与记忆**：联网搜索需要后端 SearXNG 配置；长期记忆依赖后端存储及模型服务。它们不是浏览器自动化动作的自动扩展。

目前未提供通用拖拽、操作系统鼠标控制或验证码自动处理流程。点击已发送、人工验证后页面显示成功，都不能直接证明工具完成了相应业务。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| [manifest.json](manifest.json) | 扩展入口、权限及资源声明 |
| [background.js](background.js) | CDP 连接、页面观察、浏览器输入及导航 |
| [agent_observation.js](agent_observation.js) | 自动化观察辅助与截图标注 |
| [agent_execution.js](agent_execution.js) | 标签页执行占用、动作去重、取消与执行记录 |
| [agent_runner.js](agent_runner.js) | 自动观察恢复、决策状态查询和时间预算 |
| [sidepanel.html](sidepanel.html) | 侧边栏页面 |
| [sidepanel.js](sidepanel.js) | 聊天、自动化循环、历史恢复及知识库界面 |

## 权限与数据

以 [manifest.json](manifest.json) 的实际声明为准：

- `sidePanel`：显示侧边栏。
- `storage`：保存设置和会话相关前端状态。
- `activeTab` / `scripting`：页面读取、框选截图等操作。
- `debugger`：通过 CDP 观察页面、定位元素并执行浏览器动作。
- `permissions` / `alarms`：权限申请和后台定时机制。
- 主机权限包括 `<all_urls>`，并声明可选 HTTP / HTTPS 主机权限；不是仅允许访问一个模型域名。

当前 manifest 没有 `contextMenus`，旧说明中的右键菜单功能不作为当前可用能力。

API 地址、模型等设置使用 `chrome.storage.local`，API Key 使用 `chrome.storage.session`。自动化运行期间会逐步采集并发送页面信息和截图；普通聊天的输入 / 图片也会发送到所配置的服务。

后端会持久化聊天、知识库、长期记忆和日志，可能另存调试截图；“Key 不长期保存”不等于“业务数据不落盘”。自动化停止不保证中断在途动作，也不会回滚网页操作。当前尚未为所有高风险动作开启逐次确认，请仅对已审查的任务授权。

旧 [PRIVACY.md](PRIVACY.md) 及发布材料尚需按当前实现复核，不能直接当作完整、最新的数据处理承诺。详细数据位置、网络暴露风险和限制见 [项目 README](../README.md#数据权限与当前限制)。

## 开发检查

从**仓库根目录**执行：

```powershell
node --check extension/background.js
node --check extension/sidepanel.js
node --check extension/agent_observation.js
node --check extension/agent_execution.js
node --check extension/agent_runner.js

node backend/test/audit_review/server_frontend.test.cjs
node backend/test/audit_review/kb_refresh.test.cjs
node backend/test/audit_review/svg_controls.test.cjs
node backend/test/audit_review/empty_controls.test.cjs
node backend/test/audit_review/agent_execution.test.cjs
node backend/test/audit_review/agent_runner.test.cjs
node backend/test/audit_review/agent_panel.test.cjs
```

这些是语法与离线回归检查，不替代真实浏览器、模型、Qdrant 和 reranker 的端到端验收。更多测试边界和历史报告见 [项目 README](../README.md#开发检查与回归)。
