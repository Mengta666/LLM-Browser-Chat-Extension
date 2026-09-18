# Browser Agent

基于 Chrome Manifest V3 侧边栏和 FastAPI 的浏览器 AI 助手，包含两条独立主流程：**浏览器自动化**和**聊天 / 文档知识库 / 长期记忆**。扩展负责观察页面及执行浏览器动作，后端负责模型调用、任务决策、会话管理和检索。


## 当前能力

| 模块 | 已实现的内容 | 使用边界 |
| --- | --- | --- |
| 浏览器自动化 | 页面观察、逐步决策、点击、输入、选择、滚动、导航等 | 需要扩展持续执行；后端不单独控制浏览器 |
| 控件定位 | DOM / 可访问性信息、截图、SVG 与无文本控件识别、点击前几何与遮挡检查 | 复杂跨域 iframe、页面变化、受限页面仍可能失败 |
| 侧边栏聊天 | 文本、图片、框选截图、Markdown / KaTeX、流式响应 | 图片理解、工具调用取决于所选模型能力 |
| 服务端长会话 | 历史持久化、序号分页、请求幂等、恢复、分批摘要、上下文预算保护 | 仅本项目服务端会话协议支持；不是无限上下文 |
| 文档知识库 | 建库、上传、后台索引、检索、可选精排、回收站和列表刷新 | 需要 embedding 与 Qdrant；扫描 PDF 暂无 OCR |
| 长期记忆 | core / episodic 记忆、提取、检索、管理和定期 rethink | 与会话摘要不同，会产生额外模型调用和存储 |
| 联网搜索 | 基于 SearXNG 的搜索及聊天工具调用 | 需单独部署并配置 SearXNG |

知识库和联网搜索接入的是聊天工具链，不是浏览器自动化的动作注册表。旧版“当前网页快照 RAG”、页面身份 / 快照绑定说明已不适用。

## 工作方式

### 浏览器自动化

1. 用户在侧边栏启用自动化，输入任务。
2. 扩展采集当前页面状态，包括元素信息和截图，发送给后端。
3. 后端模型输出本步判断、下一目标和结构化动作；扩展通过 Chrome DevTools Protocol（CDP）执行。
4. 扩展回传执行结果并重新观察，后端决定继续、结束或报错。

自动化使用协议 v2，后端与扩展必须一起更新。任务绑定启动时的标签页，切换活动标签不会改变操作目标。观察失败时自动重新获取页面状态，不进入人工暂停，也不会用旧观察推进或重做上一动作；持续失败超过恢复预算才结束并报告原因。动作超时先确认原执行状态，不把等待超时当作“没有执行”。

默认预算：单次观察 30 秒、一次观察恢复阶段 90 秒；单动作 120 秒、额外确认动作结束最多 30 秒；单次后端决策 180 秒；任务总时限 1 小时。预算耗尽不伪造成功。执行状态始终无法确认时，该标签页会阻止新输入；检查页面后可关闭该测试标签并重新打开，不应盲目重放有副作用的任务。

当前注册的动作：`click`、`type`、`select`、`scroll`、`scroll_to_element`、`hover`、`focus`、`clear`、`press_key`、`wait`、`navigate`。

输入支持原生 input / textarea、普通 contenteditable，以及已确认实例的 CodeMirror 5。`type` 默认替换全文，`clear=false` 在当前光标 / 选区插入；`press_key` 的组合键使用独立 `modifiers` 数组，例如 `key="a", modifiers=["Control"]`。观察提供编辑器类型、可输入 / 只读状态和焦点；写入后回读确认，失败或部分执行不会自动重放。文本中的换行不模拟 Enter；单行框拒绝多行文本。

CodeMirror 5 通过其公开接口更新文档，不把代理 textarea 的值当作全文，也不直接清除展示 DOM。普通富文本使用浏览器文本插入并按纯文本语义回读；不保证任意富文本框架的内部模型同步。CodeMirror 6 / Monaco 等尚未单独适配，多光标插入及无法可靠映射的富文本选区会明确拒绝。更新后需重新加载扩展、关闭重开侧边栏，并重启后端加载输入规则。

模型通过结构化 JSON 描述动作，不依赖聊天的 Function Calling 开关。点击路径使用浏览器输入事件；页面脚本用于观察、焦点和已支持的编辑器适配，不应把“脚本返回成功”视为业务成功。

目前没有通用拖拽动作、任意截图坐标操作工具、操作系统桌面鼠标控制或验证码自动处理流程。人工完成验证后页面出现“成功”，也不代表自动化具备了验证能力。

### 聊天、上下文与记忆

- 本项目后端通过能力探测启用服务端会话：前端只提交当前轮用户消息，后端从 SQLite 恢复历史。
- 服务端使用 `chat_id`、`request_id` 和 `expected_last_seq` 做会话定位、请求幂等与并发冲突检查。
- 长会话保留近期对话，对较早内容分批摘要，并在最终模型请求处检查上下文预算；摘要不是原文的无损替代。
- 长期记忆会按配置提取并保存到独立的记忆存储；它不等同于完整聊天历史，也不等同于会话摘要。
- 直接连接其他 OpenAI-compatible 服务时仍可使用兼容聊天模式，但第三方服务通常不提供本项目的会话、知识库和自动化接口。

## 快速开始

以下命令用于 Windows PowerShell，从仓库根目录执行。其他系统需要调整虚拟环境路径。

### 1. 准备运行环境和服务

- Python：当前本地核对环境为 3.13.2；依赖以 [backend/requirements.txt](backend/requirements.txt) 为准，不能只安装 FastAPI 和 OpenAI SDK。
- Chrome：需要支持侧边栏及 Manifest V3，允许加载未打包扩展。
- 模型服务：提供 OpenAI-compatible 接口。聊天 / 自动化使用的模型在扩展中选择；截图理解需要视觉能力，聊天自动调用知识库 / 搜索需要兼容的工具调用能力。
- 完整知识库和长期记忆还需要 embedding 服务与 Qdrant；reranker、SearXNG 按需部署。
- Node.js 仅用于下文前端回归检查；当前本地核对版本为 22.15.0。扩展运行不需要 npm 构建，也不需要安装 Playwright。

仓库不负责自动部署上述模型与检索服务。只启动 FastAPI，不代表所有依赖服务已就绪。

### 2. 安装后端依赖并准备配置

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath 'config/.env')) {
    Copy-Item -LiteralPath 'config/.env.example' -Destination 'config/.env'
}
```

编辑 `backend/config/.env`，按下文配置自己的服务地址、模型和密钥。已有真实配置时不要覆盖。

> `config/.env.example` 尚包含旧字段，也缺少部分新字段，只能作为起点，不能原样当作已验证配置。本次文档更新未修改模板或真实 `.env`。有效字段应结合下文及 [配置源码](backend/agent/memory/config.py) 核对。

### 3. 启动后端

仍在 `backend` 目录：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

建议先仅监听本机、使用单个 worker。当前部分锁和运行状态保存在进程内，不能把多 worker 当作已经验证的部署方案。

也可运行 `python app.py`，但该入口默认监听 `0.0.0.0:8000`，会扩大网络暴露范围。当前后端没有完整的请求鉴权 / 多租户隔离，CORS 也较宽松；不要直接公开到互联网。

启动后可检查：

- [Swagger 接口文档](http://127.0.0.1:8000/docs)
- [OpenAPI 路由清单](http://127.0.0.1:8000/openapi.json)
- [服务端会话能力探测](http://127.0.0.1:8000/v1/sessions/capabilities)

这些检查只能确认接口是否挂载，不能证明模型、Qdrant 或 reranker 健康。部分模块允许加载失败后继续启动；遇到功能接口 404 时检查启动日志中的 `app_startup_partial` / `modules_failed`。

### 4. 加载扩展

1. 打开 `chrome://extensions`，开启“开发者模式”。
2. 选择“加载已解压的扩展程序”，选中仓库里的 **extension 目录**，不是仓库根目录。
3. 打开扩展侧边栏，在设置中配置：

| 前端设置 | 使用本项目本机后端时 |
| --- | --- |
| API Base URL | `http://127.0.0.1:8000/v1`，不要填完整的 `/chat/completions` 地址 |
| API Key | 本机 / 内网后端允许留空；此项不是后端的上游模型密钥配置 |
| 模型名 | 填写上游实际部署并可调用的模型，不要盲用界面默认值 |

前端地址指向“本项目后端”，`.env` 中的 `MODEL_BASE_URL` 则指向“后端调用的模型服务”，两者不要混淆。后端上游密钥仍由 `OPENAI_API_KEY` 配置，前端填写的 Key 不会替换它。

修改后端代码或 `.env` 后需重启实际运行的后端；PyCharm 中应停止后重新运行。修改扩展文件后需在扩展管理页重新加载，并关闭再打开侧边栏，仅刷新网页不够。扩展详细说明见 [extension/README.md](extension/README.md)。

## 配置参考

### 模型、向量与存储

| 字段 | 当前用途 / 注意事项 |
| --- | --- |
| `MODEL_BASE_URL` | 后端上游模型接口的 Base URL，通常以 `/v1` 结尾 |
| `OPENAI_API_KEY` | 后端调用上游模型的密钥；无鉴权自建服务也需满足 SDK 的非空 Key 要求 |
| `MEMORY_MODEL` | 会话标题、摘要及记忆等辅助任务使用的模型，应显式配置为可用模型；不替代扩展选择的聊天 / 自动化模型 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | 文档与记忆向量化使用的服务 |
| `QDRANT_URL` / `QDRANT_API_KEY` | Qdrant 地址及可选鉴权 |
| `QDRANT_MEMORY_COLLECTION` | 当前记忆与文档知识库使用的集合，默认 `agent_memories` |
| `MEMORY_VECTOR_SIZE` | 当前 dense 向量维度，代码默认 `4096`；必须匹配 embedding 的实际输出 |
| `QDRANT_DISTANCE` | 距离度量，默认 `Cosine` |

当前向量存储使用命名向量 `dense` / `text`。旧模板中的 `QDRANT_VECTOR_SIZE`、`KNOWLEDGE_VECTOR_SIZE`、`QDRANT_COLLECTION` 不是当前知识库 / 记忆维数及集合的控制项。

修改 `MEMORY_VECTOR_SIZE` 不会迁移已有集合。更换 embedding 模型或维度时，应先核对实际输出和集合结构，再安排备份及重新索引；即使维度相同，不同模型的向量也不应直接混用。

模板里的 `AGENT_MODE`、`KNOWLEDGE_BACKEND`、旧 `KNOWLEDGE_*`、`MONGODB_*` 等字段不要当作当前主流程的有效切换开关。

### 长会话与记忆

下列数值是代码默认值，不是对任意模型都适用的推荐值；模板可能未包含所有键。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `CHAT_CONTEXT_LENGTH` | 128000 | 上下文窗口预算，必须按模型服务实际配置调整 |
| `CHAT_MAX_OUTPUT_TOKENS` | 4096 | 为回复预留的输出预算 |
| `CHAT_CONTEXT_SAFETY_TOKENS` | 2048 | 安全余量 |
| `CHAT_COMPACT_TRIGGER_RATIO` | 0.70 | 开始压缩历史的预算比例 |
| `CHAT_COMPACT_HARD_RATIO` | 0.90 | 硬预算比例 |
| `CHAT_COMPACT_KEEP_PAIRS` | 3 | 压缩时保留的近期完整对话轮数 |
| `CHAT_COMPACT_SUMMARY_MAX_TOKENS` | 800 | 保留摘要的目标上限 |
| `CHAT_COMPACT_MAX_OUTPUT_TOKENS` | 4096 | 摘要生成请求的输出上限，与保留摘要长度不同 |
| `CHAT_WRITE_EVERY_N_TURNS` | 3 | 聊天长期记忆提取的轮次间隔 |
| `MEMORY_RETHINK_DAEMON_ENABLED` | 1 | 定期记忆整理开关，设为 `0` 关闭后台任务 |

当前 token 计算仍是估算，不等于目标部署模型的精确 tokenizer。服务端预算保护不意味着旧兼容聊天和浏览器自动化已获得相同的长会话方案。

环境变量读取不会默认覆盖进程里已有的同名变量；如果改了 `.env` 却不生效，检查启动终端 / IDE 的环境配置后重启。

### 文档知识库、精排与搜索

- 支持 `.pdf`、`.md`、`.markdown`、`.txt`。扫描 PDF 暂不提供 OCR，正文过少时可能拒绝索引；文本文件应使用 UTF-8。
- 上传后通过文档状态确认索引完成：`pending` → `indexed` / `failed`。上传成功不等于可检索。
- 新文档分批写入不可检索的暂存版本，完整校验后发布；检索还会核对 SQLite 中的库、文档和已发布版本，失败或已删除文档不能仅凭向量的 `valid=true` 被返回。
- 删除先逻辑隐藏，向量同步失败时返回 `sync_pending=true`，后台每 30 秒尝试收敛。恢复必须完成向量校验和同步后才重新开放；暂时失败返回 503，可重试，不表示已还原。单独删除的文档不会随整库还原。
- 首次升级会在元数据数据库旁生成 `*.kb-lifecycle-*.sqlite3` 备份，并以事务添加生命周期字段；旧索引先核对完整性再开放，不清空向量。重启中断的索引标为失败，需要重新上传；缺片的旧索引保持隔离，需人工核验。
- `KB_MAX_FILE_BYTES` 后端默认 50 MiB，前端也有 50 MiB 限制；单改后端配置不会提高前端上限。
- `KB_CHUNK_SIZE=512`、`KB_CHUNK_OVERLAP=0` 控制文档 token 分块；不应拿旧网页快照分块字段配置它。
- 精排需设置 `KB_RERANK_ENABLED=true`，并填写 `KB_RERANK_API_URL`、`KB_RERANK_API_KEY`、`KB_RERANK_MODEL`。URL 必须是完整调用路径（例如服务实际提供的 `/rerank`），代码不会自动补路径。
- 当前精排适配器要求非空 Key；服务无鉴权时，也需要使用该服务接受的非空占位值。接口需兼容 `model/query/documents/top_n` 请求及 `results[].index/relevance_score` 返回格式。
- 精排缺少配置或调用失败会退回原检索排序；界面开关开启不证明精排实际成功，应结合日志验证。精排分数阈值应针对所用模型校准。
- `SEARXNG_API_URL` 填 SearXNG 搜索接口的完整路径（通常为 `/search`），需支持 JSON 结果。`SEARCH_ENABLED=1` 开启聊天自动搜索工具；关闭它不等于禁用前端手动搜索路径。

## 常用操作

- **聊天 / 自动化切换**：普通问答关闭自动化；操作网页时启用自动化，并确认当前目标标签页。
- **知识库**：在知识库面板建库、上传并等待索引成功，再在聊天输入区域选择对应知识库。后端新增的库可用刷新按钮同步。
- **历史会话**：使用本项目后端时，从历史列表恢复；不要手工拼接旧消息冒充服务端当前轮请求。
- **图片 / 网页内容**：可上传图片、框选截图或读取网页文本辅助聊天；这不等于旧版网页快照自动入库。
- **停止任务**：停止后续输入和模型结果发布，尝试释放已按下的键 / 鼠标；已发出的浏览器命令不能保证被中断，也不会撤销已经发生的网页操作。界面会区分正在停止和执行状态未确认。

## 后端接口概览

以运行中的 OpenAPI 和 [backend/api](backend/api) 为准。以下路径均相对于后端地址：

| 方法与路径 | 用途 |
| --- | --- |
| `POST /v1/chat/completions` | 聊天入口，支持服务端会话协议及兼容客户端模式 |
| `POST /v1/agent/execute` | 创建自动化任务并取得首步决策 |
| `POST /v1/agent/step`、`POST /v1/agent/cancel` | 回传观察 / 动作结果推进任务，或取消任务 |
| `POST /v1/agent/status` | 按 `session_id`、`request_id` 查询原决策；超时不另起一步 |
| `GET /v1/sessions/capabilities`、`GET /v1/sessions/list` | 能力探测与历史会话列表 |
| `GET /v1/sessions/{chat_id}/messages` | 消息分页，支持 `before_seq`、`limit` |
| `GET /v1/sessions/{chat_id}/requests/{request_id}` | 查询请求状态，供恢复与重试使用 |
| `PATCH /v1/sessions/{chat_id}`、`DELETE /v1/sessions/{chat_id}` | 会话管理 |
| `POST /v1/kb`、`GET /v1/kb` | 创建、列出知识库 |
| `POST /v1/kb/{kb_id}/docs`、`GET /v1/kb/{kb_id}/docs`、`GET /v1/kb/{kb_id}/docs/{doc_id}/status` | 文档上传、列表、索引状态 |
| `GET /v1/kb/trash`、`POST /v1/kb/{kb_id}/restore` | 知识库回收站与恢复；删除接口详见 OpenAPI |
| `/v1/memory/*` | 长期记忆查询、管理及 rethink |
| `GET /v1/logs/query`、`GET /v1/logs/sessions`、`GET /v1/logs/files` | 日志查询 |

服务端聊天需设置 `context_mode: "server"`，携带会话 / 请求标识和预期序号，`messages` 只包含本轮一条用户消息。幂等重试应复用原请求 ID，不要每次生成新 ID；完整行为见 [服务端上下文回归说明](test/audit_review/SERVER_CONTEXT_RESULTS.md)。

当前应用没有旧的 `/api/pages/refresh_snapshot`、独立 `/search` 路由，也没有通用 `/v1/models` 代理。兼容聊天入口不等于实现了全部 OpenAI API。

## 项目结构

```text
browser-agent/
├─ backend/
│  ├─ app.py                  应用入口及路由挂载
│  ├─ requirements.txt        后端依赖
│  ├─ api/                    聊天、自动化、会话、知识库、记忆、日志接口
│  ├─ agent/                  自动化决策、上下文、长期记忆与向量存储
│  ├─ rag/                    文档解析、索引、检索与精排
│  ├─ storage/                聊天历史与知识库 SQLite 存储
│  ├─ search/                 SearXNG 接入及聊天检索工具
│  ├─ tools/                  自动化动作白名单等工具代码
│  ├─ config/                 .env 与模板
│  ├─ data/                   聊天 SQLite 等运行数据
│  └─ logs/                   分频道 JSONL 日志及可选调试截图
├─ extension/
│  ├─ manifest.json           扩展声明与权限
│  ├─ background.js           CDP 连接、页面观察、浏览器动作执行
│  ├─ agent_observation.js    自动化观察辅助及截图标注
│  ├─ agent_editing.js        编辑目标识别、焦点确认与编辑器文档回读
│  ├─ agent_execution.js      按标签页管理动作占用、去重与执行记录
│  ├─ agent_runner.js         决策查询、观察恢复与停止协调
│  ├─ sidepanel.html          侧边栏入口
│  └─ sidepanel.js            聊天、自动化循环、知识库与历史交互
├─ test/                      本地测试、用例与验收报告（Git 忽略）
│  ├─ audit_review/           审计、隔离回归与真实服务验证材料
│  ├─ eval/                   长期记忆评测
│  └─ chat_eval/              聊天链路回归脚本
└─ docs/                      本地文档目录（Git 忽略）
   └─ TODO.md                 待办、已知问题与暂缓设计
```

## 开发检查与回归

以下命令从**仓库根目录**执行。`test/` 是本地保留并被 Git 忽略的目录，新克隆的仓库不包含这些测试及下方验收报告；需已有本地测试副本才能运行。测试依赖与前端检查所需的 Node.js 需另行安装。

```powershell
.\backend\.venv\Scripts\python.exe -m pip install pytest httpx lxml
.\backend\.venv\Scripts\python.exe -X utf8 -B -m pytest test/audit_review -q -p no:cacheprovider

node test/audit_review/server_frontend.test.cjs
node test/audit_review/kb_refresh.test.cjs
node test/audit_review/kb_lifecycle.test.cjs
node test/audit_review/svg_controls.test.cjs
node test/audit_review/empty_controls.test.cjs

node --check extension/background.js
node --check extension/sidepanel.js
node --check extension/agent_observation.js
```

上述 Python 审计回归使用隔离配置、临时 SQLite / 内存 Qdrant，并阻断外部网络，不应读写真实知识库。已知缺陷可能标记为 `xfail`，不表示已经修复。当前本地核对的 qdrant-client 版本为 1.18.0。

不要直接把整个 `test` 目录当成安全的离线套件：其中有旧架构脚本和真实服务测试，部分会调用模型、写入或清理集合。`live_*.py` 也需要明确的测试数据范围及服务准备，不应仅为检查文档随意运行。

验收记录：

- [长会话与恢复](test/audit_review/SERVER_CONTEXT_RESULTS.md)
- [SVG 控件](test/audit_review/SVG_CONTROLS_RESULTS.md)
- [无文本控件](test/audit_review/EMPTY_CONTROLS_RESULTS.md)
- [编辑器输入与按键](test/audit_review/EDITOR_INPUT_RESULTS.md)
- [真实 reranker 测试](test/audit_review/LIVE_RERANK_RESULTS.md)
- [知识库一致性修复与真实流程验收](test/audit_review/KB_LIFECYCLE_RESULTS.md)
- [详细审计结果](test/audit_review/DEEP_AUDIT_RESULTS.md)

历史报告描述的是各自测试时的环境和结果，不代替当前版本的重新验收。

## 故障排查

| 现象 | 优先检查 |
| --- | --- |
| 会话 / 知识库接口 404 | 前端是否连接本项目后端、路径是否带正确的 `/v1`、模块是否挂载失败 |
| 模型 400 / 格式错误 | 模型名称、图片 / 工具能力、上游聊天模板、上下文窗口与请求结构；结合实际错误定位 |
| `Connection error` | 从后端机器检查目标服务、端口与代理；不要把浏览器能访问当成后端可达 |
| 改配置仍未生效 | 后端进程是否真正重启、IDE / 终端环境变量是否覆盖文件 |
| 向量维度不匹配 | embedding 实际输出、`MEMORY_VECTOR_SIZE` 与已有 Qdrant 集合是否一致 |
| 文档一直 pending / failed | 解析、embedding、Qdrant 连接与索引日志；上传成功只是接收文件成功 |
| 开启精排但结果没变 | 完整 URL、非空 Key、模型、响应协议，以及是否触发回退 |
| 改了扩展仍是旧行为 | 扩展管理页重新加载，关闭重开侧边栏 |
| 点击返回成功但任务没完成 | 下一步页面观察是否满足业务条件；事件已发送不等于网页业务成功 |

日志位于 `backend/logs/{channel}_YYYY-MM-DD.jsonl`。查询时尽量按频道、会话或事件缩小范围，例如 `/v1/logs/query?channel=agent&event=step_result&limit=20`；旧日志查询的完整性问题仍在待办中。启用 `AGENT_DEBUG_SCREENSHOT=1` 并重启后，可保存自动化调试截图到 `backend/logs/screenshots/`，排查结束后建议关闭。

## 数据、权限与当前限制

- 聊天历史持久化到 `backend/data/chat_history.sqlite3`；记忆审计与知识库元数据使用 `backend/agent/data/agent_memory.sqlite3`。Qdrant 还保存向量及内容 payload，不能只备份一个 SQLite 就认为数据完整。
- 历史图片内容不做完整持久化恢复；关闭侧边栏后，某些图片失败请求重试需要重新附图。
- 扩展的 API 地址、模型等设置保存在 `chrome.storage.local`，API Key 使用 `chrome.storage.session`。这不表示后端聊天、知识库和记忆不落盘。
- 当前 manifest 包含 `debugger` 和 `<all_urls>` 等较广权限。启动自动化后会逐步采集页面内容和截图，并发送到配置的后端 / 模型服务；不只是用户手动截图时才发送。
- 知识库文本会发送给 embedding 服务，启用精排后候选文本会发送给 reranker。记忆提取和后台 rethink 也可能调用配置的模型服务。
- 日志与调试截图可能含页面信息、输入内容或其他敏感数据。`.gitignore` 忽略主要配置、数据库和后端日志，但不等于加密、彻底脱敏，也不覆盖所有手工导出目录。
- 后端尚未完成生产级鉴权和多租户隔离；自动化前端也没有为所有敏感动作启用逐次确认。不要在未审查的任务中授权支付、删除、转账等高风险操作。
- 自动化决策幂等和取消保护限于单后端进程及会话保留期；重启后不续接旧任务，也未支持多 worker。扩展后台重启发现未完成的执行记录时会阻止自动重放，不能据此宣称任意网页操作具有跨重启的 exactly-once 保证。
- 旧隐私 / 发布材料（包括 `extension/PRIVACY.md`）尚需按当前权限与数据流另行校准，不能据此认定已满足发布合规要求。

更多已知问题、优先级和暂缓项统一维护在本地 `docs/TODO.md`（不随仓库分发）。本项目当前适合受控的本地开发和测试，不应把已有回归通过等同于生产安全认证。
