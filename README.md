# Browser Agent

Browser Agent 是一个运行在 Chrome 侧边栏中的 AI 助手。你可以用它与模型对话、操作当前网页、查询自己的文档，并结合联网搜索获取网页资料。

项目由 **Chrome 扩展、必需的 Python 后端和可选增强服务**组成。扩展连接本项目后端，后端调用自建或第三方 OpenAI-compatible 模型接口；扩展无需 npm 构建即可加载。

## 功能概览

- **浏览器自动化**：用自然语言描述任务，执行点击、输入、选择、滚动、导航等操作。
- **聊天与会话管理**：支持图片、截图、Markdown、公式、历史会话和长会话摘要；回答达到输出上限时可手动继续生成。
- **文档知识库**：上传 PDF、Markdown 和文本文件，在对话中检索文档并展示来源。
- **长期记忆**：保存和管理偏好、稳定事实及事件信息，供后续对话参考。
- **联网问答**：通过 SearXNG 搜索网页，结合上下文处理追问。
- **网页正文提取**：可选接入 web-reader，从搜索结果中提取正文，按相关性与上下文额度选取内容。

## 阅读导航

- [部署组成与运行要求](#部署组成与运行要求)
- [安装与启动](#安装与启动)
- [功能使用](#功能使用)
- [部署搜索与正文服务](#部署搜索与正文服务)
- [配置参考](#配置参考)
- [更新与维护](#更新与维护)
- [开发与接口](#开发与接口)
- [故障排查](#故障排查)
- [安全与数据](#安全与数据)

## 部署组成与运行要求

| 组件 | 作用 | 何时需要 |
| --- | --- | --- |
| Chrome 扩展 | 聊天界面、页面观察、浏览器动作执行 | 使用侧边栏或自动化时 |
| Python 后端 | 调用模型、管理会话、执行检索和任务决策 | 必需 |
| 模型服务 | 提供 OpenAI-compatible 对话接口 | 必需，自行准备 |
| Embedding + Qdrant | 文档与长期记忆的向量化、存储和检索 | 使用知识库或长期记忆时 |
| Reranker | 对知识库候选内容重新排序 | 可选 |
| SearXNG | 聚合搜索引擎结果 | 使用联网搜索时 |
| web-reader | 抓取公开网页并提取正文 | 需要搜索正文增强时，可选 |

准备以下环境：

- **Python 3.13**：用于后端和独立正文服务。后端依赖见 [requirements.txt](backend/requirements.txt)。
- **Chrome**：支持 Manifest V3 和侧边栏，允许加载已解压的扩展。
- **Git**：用于获取和更新代码。
- **Docker Compose v2**：仅在通过本项目 Compose 部署增强服务时需要，运行 Linux 容器。
- **Node.js**：仅开发检查需要，运行扩展不需要 Node.js 或 Playwright。

模型选择要求：

- 普通文字聊天需要兼容对话接口。
- 图片理解和浏览器截图分析需要模型具备视觉能力。
- 聊天中的知识库、联网搜索需要模型支持兼容的工具调用；手动联网搜索还使用命名工具强制调用。
- 自动化通过结构化动作指令运行，不依赖聊天工具调用开关。

可以先完成“扩展 + 后端 + 模型”的基本部署，再按需接入其他服务。仅启动后端不会自动部署模型、Qdrant 或其他依赖。

## 安装与启动

### 1. 获取代码

以下以 `agent-slim` 分支为例：

```bash
git clone --branch agent-slim https://github.com/Mengta666/LLM-Browser-Chat-Extension.git browser-agent
cd browser-agent
```

除特别注明外，下文命令均从仓库根目录执行。

### 2. 安装后端并创建配置

**Windows PowerShell：**

```powershell
python -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
if (-not (Test-Path -LiteralPath backend/config/.env)) {
    Copy-Item -LiteralPath backend/config/.env.example -Destination backend/config/.env
}
```

**Linux / macOS：**

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
[ -f backend/config/.env ] || cp backend/config/.env.example backend/config/.env
```

用文本编辑器打开 `backend/config/.env`，首先填写：

| 配置项 | 填写内容 |
| --- | --- |
| `MODEL_BASE_URL` | 上游模型服务地址，通常以 `/v1` 结尾，例如 `http://127.0.0.1:8001/v1` |
| `OPENAI_API_KEY` | 上游服务的密钥；无鉴权服务也需填写 SDK 接受的非空占位值 |
| `MEMORY_MODEL` | 服务中可用的模型 ID，用于会话标题、摘要和记忆等辅助任务 |
| `CHAT_CONTEXT_LENGTH` | 实际部署模型允许的上下文窗口，不能超过服务端配置 |
| `CHAT_MAX_OUTPUT_TOKENS` | 单次模型回复的输出上限，默认 `8192` |

聊天和自动化所用的模型在扩展设置中选择，`MEMORY_MODEL` 不替代该设置。暂不使用搜索和正文服务时，保留 `SEARCH_ENABLED=0`、`WEB_READER_ENABLED=0`。暂不需要定期记忆整理时，可添加 `MEMORY_RETHINK_DAEMON_ENABLED=0`。

模板中的地址、模型和向量维度是示例，需按实际服务调整。已有 `.env` 时只补齐缺项，不要用模板覆盖真实值。

### 3. 启动后端

**Windows PowerShell：**

```powershell
.\backend\.venv\Scripts\python.exe -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
```

**Linux / macOS：**

```bash
backend/.venv/bin/python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
```

保持终端运行，打开：

- [接口文档](http://127.0.0.1:8000/docs)
- [会话能力接口](http://127.0.0.1:8000/v1/sessions/capabilities)

能力接口应返回 `server_context: true` 和 `protocol_version: 1`。这些检查确认后端接口可用，不代表上游模型或检索服务已经连通。

部署时使用单个 worker：部分会话锁和自动化状态保存在进程内。后端缺少完整的请求鉴权与多租户隔离，默认仅监听本机；如需跨机器使用，应限制在受控网络中，不要直接暴露到公网。

### 4. 加载并配置扩展

1. 在 Chrome 打开 `chrome://extensions`。
2. 开启“开发者模式”，选择“加载已解压的扩展程序”。
3. 选择仓库中的 `extension` 目录。
4. 打开扩展侧边栏，进入“设置”。

| 扩展设置 | 本机部署示例 |
| --- | --- |
| Browser Agent 后端地址 | `http://127.0.0.1:8000/v1`（默认） |
| API Key | 本项目后端未配置入口鉴权时可留空 |
| 模型 | 上游实际提供的模型 ID |

注意区分两个地址：**扩展连接本项目后端，后端再连接模型服务**。扩展中的 API Key 不会替换后端 `.env` 的 `OPENAI_API_KEY`。

扩展不再支持直接连接第三方模型接口。发送前会检查后端会话协议；后端连接失败或协议不兼容时明确报错并保留待发送内容，不静默切换聊天模式。普通聊天没有固定 8000 字限制，仍受请求体大小和后端完整请求 Token 预算约束；自动化任务指令目前仍保留 8000 字限制。

### 5. 完成第一次使用检查

1. 在输入框旁关闭“自动化”和“搜索”，发送一个简单问题，确认模型能回答。
2. 点击“会话”新建另一会话，再打开原会话，确认历史可恢复。
3. 打开一个允许测试的普通网页，启用“自动化”，尝试“滚动到页面底部”等低风险操作。
4. 接入知识库、搜索等服务后，再分别按下文验证对应功能。

Chrome 内部页面、扩展管理页面等受限页面不能作为普通网页自动化目标。

## 功能使用

### 聊天、图片与历史会话

在主面板中关闭“自动化”即可聊天。“图片”和“框选截图”可添加视觉材料；输入框支持 Enter 发送、Shift+Enter 换行。

- 使用“会话”创建、切换和恢复对话。
- 持续追问时保持在同一会话，后端会恢复历史并对较早内容生成摘要。
- 出现“回答尚未完成”且提供“继续生成”按钮时，可继续最后一条可续写回答。续写保存为新回复，不覆盖原文。
- 图片内容不会完整持久化恢复；涉及历史图片的重试可能需要重新附图。

会话摘要用于控制上下文长度，并非原文的无损副本。重要约束可在新问题中再次明确。

服务端聊天达到压缩阈值后，会先整理历史，再回答当前问题。侧边栏显示已完成批数；每批检查点保存在本地数据库。处理中“发送”按钮变为“■ 停止”，停止整轮提问，不会跳过压缩继续回答。压缩失败不会带着旧的超额上下文继续回答：临时网络错误有限重试，其他错误显示原因，可选择“继续处理原问题”或“结束本次提问”。恢复不会重复保存用户问题，已经完成的兼容批次无需重算。

后端重启或侧边栏断开后，可从原会话检查状态并恢复。取消保留历史及检查点，但不再继续当前问题；已发给上游的调用可能直到返回或超时才结束，迟到结果不能覆盖正式摘要。只有全部批次完成且完整请求预算检查通过，才一次性更新正式摘要和覆盖进度。修改摘要模型、提示词或分批预算后，不兼容的检查点会重新计算。

服务端会话存在已压缩历史时，模型可调用 `search_session_history` 查找本会话的旧消息，再通过 `read_session_history` 按序号读取原文。侧边栏显示“回查会话历史”，与联网、知识库检索区分。回查仅覆盖本次请求开始前保存的有效文字消息，不读取其他会话、已删除会话或失败请求；未完成回答保留状态标记。

历史搜索目前按原词匹配，不是语义检索；长消息按偏移量分段读取，未读完会明确标记。旧回答和旧引用不代表本轮已查证，也不构成新的操作授权。此能力不恢复历史图片，也不保证模型每次都会正确选择回查工具。

### 浏览器自动化

1. 打开任务目标网页，先完成必要的登录，并确认账号及页面正确。
2. 在输入框旁启用“自动化”。
3. 描述目标、操作范围和结束条件，例如：“在当前测试表单中填写姓名为 Demo，检查必填项，但不要提交。”
4. 查看执行步骤和页面变化，确认结果符合要求；需要终止时点击停止。

任务绑定启动时的标签页。扩展采集页面元素及截图，后端决定动作，再由扩展通过 Chrome DevTools Protocol 执行。切换活动标签不会自动切换任务目标。

可用动作包括 `click`、`type`、`clear`、`press_key`、`select`、`focus`、`hover`、`scroll`、`scroll_to_element`、`navigate`、`wait`。输入支持原生输入框、普通 contenteditable 和已适配的 CodeMirror 5；复杂富文本、跨域 iframe 等页面仍可能受限。

观察失败时会重新获取页面状态；动作超时会先确认原动作状态，避免直接重复执行。如果提示执行状态无法确认，先检查网页实际结果，不要立即重试有副作用的任务。停止不会撤销已经发生的操作。

不支持通用拖拽、桌面鼠标控制或自动通过验证码。验证码需人工处理；支付、转账、删除等操作应在明确授权和人工监督下进行。

### 文档知识库

**准备服务。** 配置 embedding 和 Qdrant，重启后端：

| 配置项 | 说明 |
| --- | --- |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` | 向量化服务的兼容接口地址和密钥 |
| `EMBEDDING_MODEL` | 实际部署的 embedding 模型 ID |
| `MEMORY_VECTOR_SIZE` | embedding 实际输出维度，必须与集合一致 |
| `QDRANT_URL` / `QDRANT_API_KEY` | Qdrant 地址和可选密钥 |
| `QDRANT_MEMORY_COLLECTION` | 使用的集合名，未设置时为 `agent_memories` |

知识库和长期记忆使用命名向量 `dense` / `text`，由不同的内容类型和标识区分。更换 embedding 模型或维度前应备份数据并规划重建索引；即使两个模型维度相同，也不能直接混用其向量。

**上传并提问。**

1. 进入“知识库”，点击“新建知识库”，填写名称。
2. 打开该知识库，上传文档。
3. 等待文档状态由 `pending` 变为 `indexed`；`failed` 表示未完成索引，需查看错误。
4. 回到聊天，通过输入框旁的“知识库”按钮选择对应库。
5. 提出文档中的具体问题，查看检索过程、回答及来源。

支持 PDF、Markdown 和 UTF-8 文本，默认单文件上限为 50 MiB。扫描版 PDF 没有 OCR，需要先转换成可提取文字的文档。上传成功只代表文件已接收，索引成功后才能检索。

可用“刷新”同步列表，删除的库可在“回收站”中查看或恢复。删除后同步未完成、恢复失败等状态需按提示处理；彻底删除不可恢复。

**可选精排。** 若有兼容 rerank 服务，配置：

```dotenv
KB_RERANK_ENABLED=true
KB_RERANK_API_URL=http://127.0.0.1:18200/rerank
KB_RERANK_API_KEY=replace-with-your-reranker-key
KB_RERANK_MODEL=your-reranker-model
KB_RERANK_TOP_K=5
```

地址须包含服务实际提供的完整调用路径。适配器使用 `model/query/documents/top_n` 请求和 `results[].index/relevance_score` 响应；Key 要求非空。调用失败会退回原检索排序，精排分数阈值应结合自己的模型和文档调整。

### 长期记忆

长期记忆与知识库共用 embedding 和 Qdrant 配置，还需要可用的 `MEMORY_MODEL`。

在“记忆”面板可以查看、添加、编辑、删除记忆，或点击“整理”让模型处理冲突、过期和重复信息。对话也会按配置触发记忆提取，但并非每条消息都会成为长期记忆。

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `CHAT_WRITE_EVERY_N_TURNS` | `3` | 聊天记忆提取的轮次间隔 |
| `MEMORY_RETHINK_DAEMON_ENABLED` | `1` | 是否启用定期整理；设为 `0` 关闭 |

长期记忆保存偏好、稳定事实及事件信息；聊天历史保存对话记录；会话摘要压缩较早上下文。三者用途不同，删除或修改一种并不等于处理了全部数据。记忆提取和整理会产生额外模型调用。

### 联网搜索与来源

接入 SearXNG 后，在后端配置：

```dotenv
SEARCH_ENABLED=1
SEARXNG_API_URL=http://127.0.0.1:19080/search
SEARCH_RESULT_COUNT=5
SEARCH_TIMEOUT=20
```

以上地址适用于本项目 Compose 的同机部署；已有搜索服务时填写自己的完整 `/search` 地址。后端模板中的示例端口是 `8888`，需按实际部署修改。

- `SEARCH_ENABLED=1` 允许模型自行选择联网搜索。
- 点击输入框旁“搜索”按钮，表示本轮要求联网；关闭自动搜索不等于禁用该按钮。
- 模型结合会话生成查询，每条用户问题最多进行 3 次搜索，搜索和生成共用时间预算。
- 联网失败和正常“无匹配”是不同状态；失败不能证明网上没有相关内容。

回答中的 `[N]` 对应本轮来源。来源卡片表示检索到了资料，不一定已被正文引用；模型仍可能错引，重要结论应打开来源复核。查询时间、抓取时间也不等于网页发布时间。

未启用正文服务时，模型使用搜索摘要；需要读取更多页面内容时，按下一节接入 web-reader。

## 部署搜索与正文服务

SearXNG 提供候选网页，web-reader 使用 Trafilatura 提取公开 HTML 正文。两者是独立服务，可分别启用。项目的 Compose 不包含 Open WebUI、模型服务、Qdrant 或主后端。

完整参数及维护命令见 [部署文档](deploy/README.md)，正文协议见 [web-reader 文档](addons/web-reader/README.md)。下列命令在存放仓库的部署机器上运行；Linux 可将 `python` 换为 `python3`。

### 1. 选择部署方式

**已有 SearXNG，只新增正文服务：**

```bash
python deploy/init.py
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile reader config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile reader up -d --build
```

**同时部署搜索和正文服务：**

```bash
python deploy/init.py --with-search
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile search --profile reader config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile search --profile reader up -d --build
```

初始化只创建缺失配置并生成密钥，不覆盖已有 `deploy/.env`。仅部署 reader 不会启动另一套 SearXNG。

### 2. 配置后端访问

在 `backend/config/.env` 中设置：

```dotenv
WEB_READER_ENABLED=1
WEB_READER_API_URL=http://127.0.0.1:19081
WEB_READER_API_KEY=copy-the-key-from-deploy-env
WEB_READER_MAX_PAGES=3
WEB_READER_TIMEOUT=20
WEB_READER_CONTEXT_TOKENS=6000
```

将占位密钥替换成 `deploy/.env` 中的同一个 `WEB_READER_API_KEY`，至少 32 个 ASCII 字符。服务地址填写基址，不添加 `/v1/extract`。保存后重启实际运行的后端。

默认端口为 SearXNG `19080`、web-reader `19081`，仅绑定 `127.0.0.1`。跨机器部署时：

1. 在 `deploy/.env` 修改 `READER_BIND_IP`；需要远程搜索时同时修改 `SEARCH_BIND_IP`。
2. 重新执行对应的 `up -d`，使端口映射生效。
3. 后端填写服务器可访问的地址，不要使用后端自身的 `127.0.0.1`。
4. 限制允许访问的客户端，跨不可信网络使用 HTTPS / VPN。

`0.0.0.0` 是监听所有 IPv4 网卡的地址，不是客户端应填写的目标地址。不同 Compose 项目默认网络隔离，不能假定通过服务名即可互通。

### 3. 验证服务和实际使用

以下示例在部署机器上执行，读取本地 `deploy/.env` 的密钥：

```bash
python deploy/check.py --reader-url http://127.0.0.1:19081
python deploy/check.py --reader-url http://127.0.0.1:19081 --url https://www.python.org/downloads/
python deploy/check.py --search-url http://127.0.0.1:19080/search
```

第一条检查进程就绪；第二条实际抓取网页，应有正文和结构块；第三条执行搜索。按已部署服务选择执行，不要频繁重复搜索检查。

随后在侧边栏进行一次联网提问，查看搜索卡片中的“已读取”和“纳入”数量，以及来源的正文 / 节选 / 摘要标签：

- **已读取**：正文服务成功提取了文本。
- **已纳入**：本次工具结果实际放入模型上下文的正文来源。
- 读取失败时保留搜索摘要；预算不足、没有可容纳的完整段落或本轮内容重复时，可能不新增正文。

web-reader 不执行 JavaScript，不使用登录 Cookie，不处理 PDF 或验证码。它默认限制下载 / 解压数据为 2 MiB，返回正文最多 100000 字符、2048 个结构块；后端还会进一步节选，不会把提取上限直接作为模型输入量。

## 配置参考

### 配置文件与生效方式

| 位置 | 管理的内容 | 修改后如何生效 |
| --- | --- | --- |
| 扩展“设置” | 后端地址、聊天 / 自动化模型及前端参数 | 保存设置 |
| `backend/config/.env` | 上游服务、检索及聊天预算 | 重启后端 |
| `deploy/.env` | 可选服务镜像、端口、密钥及资源参数 | 对相应服务重新执行 `docker compose ... up -d` |

两份 `.env` 不自动同步。模板见 [后端配置](backend/config/.env.example) 和 [部署配置](deploy/.env.example)。终端或 IDE 已设置的同名环境变量优先于后端 `.env`，修改文件后仍未生效时应检查启动环境。

模板保留了兼容字段：`AGENT_MODE`、`QDRANT_VECTOR_SIZE`、`QDRANT_COLLECTION`、旧 `KNOWLEDGE_*` 和 `MONGODB_*` 不控制本文所述主流程。文档与记忆集合、维度以 `QDRANT_MEMORY_COLLECTION`、`MEMORY_VECTOR_SIZE` 为准。

### 上下文、输出与正文预算

| 配置项 | 代码默认值 | 用途 |
| --- | --- | --- |
| `CHAT_CONTEXT_LENGTH` | `128000` | 模型上下文窗口，必须按实际服务调整 |
| `CHAT_MAX_OUTPUT_TOKENS` | `8192` | 单次模型回复输出上限 |
| `CHAT_CONTEXT_SAFETY_TOKENS` | `2048` | 给计数误差及请求包装预留余量 |
| `CHAT_TOKENIZER_URL` | 留空 | 可选 vLLM `/tokenize` 完整地址；须与模型使用同一分词器及聊天模板 |
| `CHAT_TOKENIZER_MODEL` | 留空 | 与聊天模型 ID 完全匹配才启用远程计数，避免混用分词器 |
| `CHAT_TOKENIZER_API_KEY` | 留空 | 分词接口独立凭证，不复用模型网关密钥 |
| `CHAT_TOKENIZER_TIMEOUT` | `2` | HTTP各阶段超时，秒；上限5秒，单轮计数时间预算8秒 |
| `CHAT_TOKENIZER_SAFETY_RATIO` | `1.05` | 远程计数额外预留5%，最小为1；与固定安全余量同时生效 |
| `CHAT_COMPACT_TRIGGER_RATIO` | `0.70` | 服务端聊天达到此输入额度比例后，先压缩再回答 |
| `CHAT_COMPACT_HARD_RATIO` | `0.90` | 仅旧客户端兼容入口使用 |
| `CHAT_COMPACT_TARGET_RATIO` | `0.50` | 压缩后的目标输入比例，减少连续触发 |
| `CHAT_COMPACT_KEEP_PAIRS` | `3` | 优先保留的近期对话对数，空间不足时可减少 |
| `CHAT_COMPACT_SUMMARY_MAX_TOKENS` | `800` | 保留摘要的目标上限 |
| `CHAT_COMPACT_MAX_OUTPUT_TOKENS` | `4096` | 摘要模型单次调用的输出上限 |
| `CHAT_COMPACT_MODEL` | 留空 | 摘要专用模型；默认沿用辅助模型 |
| `CHAT_COMPACT_ENABLE_THINKING` | 留空 | 仅摘要任务的推理开关；须确认上游支持 `chat_template_kwargs.enable_thinking`，否则留空 |
| `CHAT_COMPACT_CALL_TIMEOUT` | `90` | 单次摘要调用超时，秒 |
| `CHAT_COMPACT_TIMEOUT` | `360` | 每次恢复或执行压缩的阶段上限，秒 |
| `CHAT_COMPACT_MAX_ATTEMPTS` | `2` | 每批每阶段的临时错误尝试次数，含首次；最多 5 次 |
| `CHAT_TURN_TIMEOUT` | `540` | 整条用户请求上限，秒；服务端最多 570 秒 |
| `WEB_READER_CONTEXT_TOKENS` | `6000` | 一条用户问题内网页正文和摘要共用的估算额度 |

可用输入额度 = 上下文窗口 − 输出上限 − 安全余量。例如窗口为 65536、输出上限为 8192、安全余量为 2048 时，输入额度为 55296。

摘要与正式回答分别计时，压缩完成后再开始计算回答阶段的 `CHAT_LLM_TIMEOUT`，但两者仍受整轮上限约束。摘要输出截断、预算配置错误不会无限自动重试；超过摘要目标长度时先精简，仍不合格则保留检查点等待处理。单条新消息或固定提示本身已超额时，无法靠压缩旧历史解决，需缩短输入或修正窗口配置。

网页资料额度包含在总输入额度内，多次搜索共享；下一条用户问题重新计数。历史消息、工具定义、标题和 URL 也占上下文。不是每次搜索都额外增加 6000，也没有固定的首次 / 后续分配比例。

启用匹配模型的 `CHAT_TOKENIZER_URL` 后，服务端聊天准备、工具循环和联网资料裁剪将完整消息、工具定义交给 vLLM，按聊天模板计数并额外预留5%。接口返回的窗口上限小于 `CHAT_CONTEXT_LENGTH` 时，以较小值计算输入额度。接口应仅对受信任后端开放；vLLM 的模型 API Key 不一定保护 `/tokenize`，不要直接裸露到公网。

同一轮缓存重复请求的计数；接口超时、报错或返回无效计数后，本轮停止远程尝试，回退本地估算并保留25%余量。未配置、模型不匹配或含图片等多模态内容时也使用本地估算。日志通过 `count_mode`、`input_tokens_raw`、`token_safety_margin` 和 `tokenizer_fallback` 区分实际计数与降级；网页分块、摘要分片及历史片段额度仍是本地估算，最终聊天请求再统一核对。遇到超额时会缩减可移除的网页资料或明确报错，不保证任意长度请求都可继续。

历史回查每条用户问题最多调用4次，单次返回上限2000 tokens、累计上限4000 tokens，实际还受当前输入剩余额度限制；这些都包含在总输入额度中，不额外扩展模型窗口。降低正文额度会减少资料覆盖，提高输出上限则会减少可用输入空间。

## 更新与维护

### 更新应用

1. 结束正在运行的自动化任务，保留本地配置，并备份有价值的数据。
2. 检查工作区改动，再更新所使用的分支。没有分叉时可使用：
   ```bash
   git status
   git pull --ff-only
   ```
   如果存在冲突或分叉，先处理，不要强行覆盖本地更改。
3. 按新版本的 `backend/requirements.txt` 更新依赖，对照模板补充配置，保留原有有效值。
4. 停止并重启后端；在 `chrome://extensions` 重新加载扩展，然后关闭并重开侧边栏。仅刷新网页不够。
5. 重新检查基本聊天、会话恢复，以及启用的知识库和搜索功能。

后端与扩展应使用匹配版本。后端重启不会续接旧自动化任务；重新开始前先确认页面上已发生的操作。

### 更新或停用正文服务

更新源码后，在部署机器重新构建对应服务：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile reader up -d --build web-reader
```

检查 `deploy/.env` 的镜像标签是否与新版本一致，保留已有绑定地址、端口和密钥，再运行服务检查。无需重建另一项目的 SearXNG。

暂时停用增强时，将后端 `WEB_READER_ENABLED` 设为 `0` 并重启后端；搜索仍可使用摘要。容器升级、回退及更多维护步骤见 [部署文档](deploy/README.md)。

### 备份与恢复

| 数据 | 保存位置 |
| --- | --- |
| 聊天历史和请求状态 | `backend/data/chat_history.sqlite3` |
| 知识库元数据、记忆变更审计 | `backend/agent/data/agent_memory.sqlite3` |
| 文档 / 记忆向量及内容 | 配置的 Qdrant 集合 |
| 后端与部署配置 | `backend/config/.env`、`deploy/.env`、`deploy/runtime/` |
| 运行日志 | `backend/logs/` |

维护前停止相关写入，按 SQLite / Qdrant 对应方式制作一致性备份。只备份一个 SQLite 文件不能恢复全部知识库和记忆。恢复时应匹配数据库、向量集合及 embedding 配置；不要通过删除数据目录或 Docker 数据卷解决普通连接故障。

## 开发与接口

### 目录结构

```text
browser-agent/
├─ backend/
│  ├─ app.py              FastAPI 应用入口
│  ├─ api/                聊天、会话、自动化、知识库、记忆和日志接口
│  ├─ agent/              任务决策、上下文处理和长期记忆
│  ├─ rag/                文档解析、索引、检索与精排
│  ├─ search/             搜索、正文客户端、节选及请求预算
│  ├─ storage/            聊天和知识库元数据存储
│  └─ config/             后端配置与模板
├─ extension/             Chrome 扩展界面、页面观察和动作执行
├─ addons/web-reader/     独立正文服务、Dockerfile 及离线测试
└─ deploy/                Compose、初始化和服务检查脚本
```

浏览器动作由扩展执行，后端本身不直接控制浏览器。知识库、搜索属于聊天工具链，不是自动化动作注册表。新增独立增强服务时参照 [addons 维护约定](addons/README.md)。

### API 入口

运行后查看 [Swagger](http://127.0.0.1:8000/docs) 或 [OpenAPI](http://127.0.0.1:8000/openapi.json) 中的完整字段。

| 接口 | 用途 |
| --- | --- |
| `POST /v1/chat/completions` | 聊天及检索工具调用 |
| `GET /v1/sessions/capabilities`、`GET /v1/sessions/list` | 会话能力和历史列表 |
| `GET /v1/sessions/{chat_id}/messages` | 消息分页 |
| `GET /v1/sessions/{chat_id}/requests/{request_id}` | 请求状态、恢复及重试信息 |
| `POST /v1/agent/execute`、`POST /v1/agent/step` | 自动化启动和推进 |
| `POST /v1/agent/status`、`POST /v1/agent/cancel` | 自动化状态查询和取消 |
| `/v1/kb` | 建库、文档上传、索引状态、删除与恢复 |
| `/v1/memory/*` | 长期记忆管理 |
| `/v1/logs/*` | 日志查询与文件列表 |

自定义客户端使用服务端会话时，设置 `context_mode: "server"`，携带 `chat_id`、`request_id` 和 `expected_last_seq`，只提交本轮用户消息。重试复用原请求 ID；续写使用新请求 ID 并通过 `continuation_of` 关联原回答。实现见 [server_chat.py](backend/api/server_chat.py)。

主后端不提供通用 `/v1/models` 代理。正文服务单独提供 `GET /health`、`POST /v1/extract`，不要将其路由拼到主后端地址下。

### 开发检查

扩展没有构建步骤。安装 Node.js 后可检查语法：

```bash
node --check extension/background.js
node --check extension/sidepanel.js
node --check extension/agent_editing.js
node --check extension/agent_runner.js
```

web-reader 的测试随仓库发布，使用独立 Python 环境，不要与后端应用混装。Windows 示例：

```powershell
python -m venv addons/web-reader/.venv
.\addons\web-reader\.venv\Scripts\python.exe -m pip install -r addons/web-reader/requirements-test.txt
.\addons\web-reader\.venv\Scripts\python.exe -m pytest addons/web-reader/tests -q
```

Linux / macOS 将解释器路径换为 `addons/web-reader/.venv/bin/python`。此套件禁止外部网络，部分测试需要创建子进程。新增行为应配套离线用例；真实模型、网页或知识库测试应使用明确的独立测试范围，避免写入业务数据。

`test/`、`docs/`、`output/` 是被 Git 忽略的本地目录，新克隆仓库不包含其中的测试、待办和报告。独立服务接口与测试说明见 [web-reader 文档](addons/web-reader/README.md)。

## 故障排查

| 现象 | 检查方法 |
| --- | --- |
| 扩展无法连接 / 接口 404 | 确认后端已启动，扩展地址为后端的 `/v1` 基址；检查启动日志是否有模块加载失败 |
| 模型报 400 | 核对模型 ID、视觉 / 工具能力、消息模板及实际上下文窗口，根据上游错误定位 |
| `Connection error` | 从实际发起请求的机器检查服务地址、端口和代理；浏览器能访问不代表后端可达 |
| 修改配置仍未生效 | 重启后端，检查 IDE / 终端的同名环境变量；部署端口变更需重新 `up -d` |
| 修改扩展后行为不变 | 在扩展管理页重新加载，关闭并重开侧边栏 |
| 自动化显示状态未确认 | 检查原网页执行结果，不要直接重复提交；停止不撤销已发生的操作 |
| 文档一直 `pending` / `failed` | 查看文档错误及后端日志，检查文本解析、embedding、Qdrant 和向量维度 |
| 开启精排但结果未变化 | 核对完整 rerank 地址、非空 Key、响应格式，以及日志中是否回退 |
| 搜索返回 0 项 | 检查 SearXNG 的 `results` 与 `unresponsive_engines`；HTTP 200 或容器健康不等于搜索成功 |
| `too many requests` / `CAPTCHA` / `Suspended` | 上游引擎限制或冷却；减少重复请求，等待恢复。关闭本地 limiter 不能解除上游限制 |
| reader 本机可用、远端不通 | 检查绑定地址、宿主机端口和访问控制；`127.0.0.1` 只供本机使用 |
| reader 健康但没有正文 | 显式用 `deploy/check.py --reader-url ... --url ...` 测试目标网页；核对密钥和读取错误 |
| 已读取但没有纳入 / 回答不完整 | 检查来源的纳入状态、节选标签、上下文预算，以及是否达到输出上限 |
| 来源卡片存在但正文没有引用 | 模型可能未引用；检索到来源与生成正文引用是两个环节，不能仅凭卡片判断答案有依据 |

搜索尚未具备完善的统一限速、跨请求缓存和按引擎冷却状态提前结束补搜策略，遇到引擎不可用时可能连续出现失败提示。此时不要持续发送同类请求来测试是否恢复。

日志按频道保存在 `backend/logs/{channel}_YYYY-MM-DD.jsonl`。例如：

```text
/v1/logs/query?channel=chat&event=web_search_finished&limit=20
/v1/logs/query?channel=chat&event=web_context_selected&limit=20
/v1/logs/query?channel=chat&event=agentic_context_budget&limit=20
```

分别用于查看搜索结果、实际纳入资料和完整请求预算；通过 `session_id`、日志中的 `request_id` 关联问题。`/v1/logs/query` 只读取当前进程内存缓存，查看重启前的事件应读取对应磁盘日志。

## 安全与数据

- 部署用于受控的个人或团队环境。后端没有完整的入口鉴权及多租户隔离，不应直接开放公网。
- 扩展拥有较广的网页访问和调试权限。自动化会发送页面内容和截图到后端 / 模型服务，只在信任的页面、账号和任务范围内使用。
- 文档会发送给 embedding 服务，候选片段可能发送给 reranker；聊天、摘要和记忆处理会调用模型。联网查询发送给搜索服务，选中的网页内容也会进入模型上下文。
- web-reader 不使用登录态、不持久化整页 HTML，但有限的来源节选会随聊天历史保存。它和后端正文客户端不继承系统 HTTP 代理，部署机器需具备所需网络连通性。
- 真实 `.env`、数据库、日志和运行产物由 Git 忽略，不代表这些数据已加密或彻底脱敏。分享排障材料前检查其中的密钥、页面信息和用户输入。
- 模型回答、引用和浏览器操作都需要结合实际结果判断。对敏感操作保留人工确认，不将输出“成功”视为已经完成业务验证。
