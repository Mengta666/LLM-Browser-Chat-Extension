# Browser Agent

Browser Agent 是一个浏览器侧边栏 AI 助手项目，目前包含 Chrome 扩展前端和 FastAPI 后端。前端负责聊天、页面读取、截图/图片输入和交互状态；后端负责 OpenAI-compatible 聊天接口、当前网页 RAG、页面快照索引、Qdrant 向量检索和 SQLite 元数据管理。

## 项目结构

```text
browser-agent/
  backend/      FastAPI 后端、RAG、向量库、SQLite 存储
  extension/    Chrome 扩展前端
  README.md     当前项目总说明
```

`docs/` 目前作为本地开发文档目录，不进入 Git 仓库。

## 当前能力

- Chrome 侧边栏聊天，支持流式回复、Markdown 和 KaTeX 渲染。
- 支持当前网页内容作为 RAG 上下文。
- 支持“刷新快照”，将同一 URL 的最新页面内容重新索引到 Qdrant。
- 同一页面的旧 chat 再次提问时，会使用该页面最新 snapshot。
- 使用 SQLite 保存 page、snapshot、chat 与页面绑定关系。
- 使用 Qdrant 保存网页 chunk embedding。
- 提供 OpenAI-compatible `/v1/chat/completions` 接口，方便前端按普通模型 API 调用。

## 后端启动

进入后端目录：

```powershell
cd backend
```

创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\pip install fastapi uvicorn openai python-dotenv qdrant-client
```

在 `backend/config/.env` 中配置环境变量。


```env
MODEL_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your_chat_model_api_key

EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=your_embedding_api_key
EMBEDDING_MODEL=your_embedding_model

QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=browser_agent_chunks
QDRANT_VECTOR_SIZE=1024
QDRANT_DISTANCE=Cosine

PAGE_ID_VERSION=v1
CONTENT_HASH_VERSION=v1
SNAPSHOT_ID_VERSION=v1
POINT_ID_VERSION=v1
POINT_NAMESPACE=browser-agent

CHUNKER_VERSION=v1
PAGE_CHUNK_SIZE=800
PAGE_CHUNK_OVERLAP=100
```

注意：`QDRANT_VECTOR_SIZE` 必须和 `EMBEDDING_MODEL` 实际输出维度一致，否则写入或检索会失败。

启动服务：

```powershell
.\.venv\Scripts\python.exe app.py
```

默认监听：

```text
http://localhost:8000
```

## 后端接口

当前主要接口：

- `POST /v1/chat/completions`：OpenAI-compatible 聊天入口。
- `POST /api/pages/refresh_snapshot`：立即刷新当前页面快照并写入/复用 Qdrant 索引。
- `POST /search`：联网搜索入口，需要配置 `SEARXNG_API_URL`。

## Chrome 扩展使用

1. 打开 `chrome://extensions`。
2. 开启“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择 `browser-agent/extension` 目录。
5. 打开扩展侧边栏，在设置中配置 API。

如果使用本项目后端，前端 API Base URL 填：

```text
http://localhost:8000/v1
```

前端会把聊天请求发送到 `/v1/chat/completions`，并在点击“刷新快照”时调用同一后端下的 `/api/pages/refresh_snapshot`。

## RAG 快照规则

当前页面身份由 `backend/common/page_identity.py` 生成：

- `page_id`：基于规范化 URL，表示同一个逻辑页面。
- `content_hash`：基于清洗后的正文内容，表示内容版本。
- `snapshot_id`：基于 `page_id + content_hash`，表示某个页面的某次内容快照。
- `chunk_id` / `point_id`：用于 Qdrant chunk 写入和检索去重。

普通发送时：

- 优先复用 `pages.latest_snapshot_id`。
- 如果 latest snapshot 已存在且 Qdrant 中有向量，不重复写库。
- 如果没有可用 snapshot，才按当前页面内容创建新 snapshot。

点击“刷新快照”时：

- 当前 URL 的 latest snapshot 会切到最新页面内容。
- 所有绑定该 `page_id` 的历史 chat 会同步切到新 snapshot。
- 旧 snapshot 的 DB 记录保留为 inactive。
- 旧 snapshot 对应的 Qdrant points 会被清理。

## 本地验证

后端自测：

```powershell
cd backend
.\.venv\Scripts\python.exe -B test\test_page_identity.py
.\.venv\Scripts\python.exe -B test\test_rag_refresh_flow.py
```

前端语法检查：

```powershell
node --check extension\background.js
node --check extension\sidepanel.js
```

## 隐私与本地数据

- `backend/config/.env` 不进入 Git。
- SQLite 数据库、Qdrant 数据和 Python 缓存都属于本地运行产物，不进入 Git。
- 插件只会在用户主动发送、启用当前网页或点击刷新快照时，把对应内容发送到配置的后端或模型 API。

## 开发状态

当前项目仍处于 MVP 阶段。RAG 主链路已经接入，后续重点是聊天历史、memory 存储、更多端到端验收和更完整的部署说明。
