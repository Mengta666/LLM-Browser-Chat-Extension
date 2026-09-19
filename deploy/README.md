# 可选搜索增强部署

本入口仅管理本项目可选的 SearXNG 和 web-reader，不部署聊天 UI、后端主服务、模型或浏览器扩展，也不修改其他 Compose 项目。

## 前提

- Linux Docker Engine 或支持 Linux 容器的 Docker Desktop，以及 Docker Compose v2。
- Python 3.10+ 用于初始化/检查脚本（仅标准库）；服务本身在 Python 3.13 镜像运行。
- 构建机能下载官方镜像及 PyPI 依赖；正文服务所在容器能直接访问公开网站。
- 命令从克隆后的**仓库根目录**执行。Windows 可把 `python3` 换为 `python`。

## 1. 已有 SearXNG：仅新增正文服务

```bash
python3 deploy/init.py
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile reader config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile reader up -d --build
```

初始化只创建不存在的 `deploy/.env`，自动生成密钥，不打印密钥、不覆盖已有配置，不生成新的 SearXNG 配置。

`reader` 与 `search` 相互独立，没有 `depends_on`，只启用 reader 不会启动搜索服务。Compose 项目名默认 `browser-agent-addons`，可在 `.env` 修改；没有固定容器名，不占用其他项目的同名容器。

## 2. 没有 SearXNG：一起部署

```bash
python3 deploy/init.py --with-search
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile search --profile reader config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile search --profile reader up -d --build
```

多生成一份 `deploy/runtime/searxng/settings.yml`，采用 `use_default_settings: true`，启用 JSON 输出，生成独立 secret。官方镜像可能调整挂载目录的所有者，属于 SearXNG 自身启动行为；初始化脚本不会重写已有配置。

默认发布地址：SearXNG `127.0.0.1:19080`、web-reader `127.0.0.1:19081`。端口被占用时改 `.env`，不要停止未知容器来释放端口。

## 3. 配置与跨机器访问

`deploy/.env` 只用于部署；**不会自动写入 `backend/config/.env`**。

| 配置 | 默认/说明 |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | `browser-agent-addons`；同主机多套部署需不同名字和端口 |
| `SEARXNG_IMAGE` | 官方固定标签和摘要；仅 search 使用 |
| `SEARCH_BIND_IP` / `SEARCH_PORT` | `127.0.0.1` / `19080` |
| `WEB_READER_IMAGE` | 本地构建标签 `browser-agent-web-reader:0.2.0`，不是远程预构建镜像 |
| `READER_BIND_IP` / `READER_PORT` | `127.0.0.1` / `19081` |
| `WEB_READER_API_KEY` | init 生成；至少 32 个 ASCII 字符，启动时必须有效 |
| `READER_CONCURRENCY` | 3；允许 1～16，内存限制也需匹配 |
| `READER_MAX_BYTES` | 2097152；下载/解压数据上限，允许 64 KiB～8 MiB |

后端位于另一台机器时，把 `READER_BIND_IP` 改为服务器的可信网卡地址，按客户端设置访问控制，再重新 `up -d` 使端口映射生效。不要直接使用全网公开绑定。跨不可信网络必须通过 HTTPS 或 VPN，Bearer 密钥不能裸露传输。

Docker 发布端口可能不受宿主机某些简易防火墙规则限制，必须实际从允许/禁止的客户端验证。SearXNG 的 JSON 接口也不应匿名暴露公网。

## 4. 检查服务

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile reader ps
python3 deploy/check.py --reader-url http://127.0.0.1:19081
python3 deploy/check.py --reader-url http://127.0.0.1:19081 --url https://www.python.org/downloads/
```

只有显式传 `--url` 才访问该公开网页。检查读取本地密钥，只输出状态、长度及错误码，不输出正文或密钥。

验证 SearXNG 时显式给出地址（会执行一次固定的 Python 文档搜索）：

```bash
python3 deploy/check.py --search-url http://127.0.0.1:19080/search
```

`healthy` 仅说明接口就绪；必须进一步检查真实抓取。SearXNG 能联网不代表 web-reader 的出站网络相同。脚本连接服务时不继承系统 HTTP 代理。

## 5. 接到本项目后端

在**已有** `backend/config/.env` 中按需增加/更新以下键，不要用模板覆盖真实文件：

```dotenv
WEB_READER_ENABLED=1
WEB_READER_API_URL=http://127.0.0.1:19081
WEB_READER_API_KEY=填写部署配置中的同一个密钥
WEB_READER_MAX_PAGES=3
WEB_READER_TIMEOUT=20
WEB_READER_CONTEXT_TOKENS=6000
```

`WEB_READER_API_URL` 是基址，不带 `/v1/extract`。同机宿主机后端使用映射端口；远程后端使用服务器地址；同一 Compose 网络里的客户端可使用 `http://web-reader:8000`。不同 Compose 项目默认网络隔离，不能直接假定服务名互通。

SearXNG 仍由后端原有 `SEARXNG_API_URL` 配置。已有搜索服务继续使用原地址；新部署的宿主机示例是 `http://127.0.0.1:19080/search`。

重启实际运行的后端。更新扩展后在扩展管理页重新加载并重开侧边栏。观察搜索卡片是否出现正文读取数量，来源是否标记正文/节选/摘要。

默认关闭正文增强；开启后配置无效、鉴权失败、抓取失败会显式降级为摘要，不阻断普通聊天，不保证“最新”答案必然正确。

`WEB_READER_CONTEXT_TOKENS=6000` 是**一次用户提问至最终回答**内新纳入正文及搜索摘要的共享估算额度，多个搜索、缓存读取都共用，不是每次搜索各 6000；下一条用户问题重新计算。没有固定的 4000/2000 拆分。历史、标题、URL、工具定义另计入完整请求预算，已分配额度不会因移除旧工具文本退还。

当前计数使用 cl100k_base（不可用时启发式）乘 1.25 的保守估算，并保留 `CHAT_MAX_OUTPUT_TOKENS` 和 `CHAT_CONTEXT_SAFETY_TOKENS`；这不是 Qwen 精确 tokenizer，图片也仍是估算。需保证 `CHAT_CONTEXT_LENGTH` 不大于服务端实际模型窗口。服务端明确拒绝上下文长度时，仅允许一次缩减网页资料后的重试；普通参数错误不会重试。

## 6. 运维与升级

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail 100 web-reader
docker compose --env-file deploy/.env -f deploy/compose.yaml stop web-reader
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile reader up -d --build web-reader
```

升级先取得匹配的项目版本，保留本地配置，重新构建选定服务并运行检查；不要覆盖 `.env`，不要自动切到浮动镜像版本。回退可关闭 `WEB_READER_ENABLED` 并重启后端，或部署之前保留的服务镜像。

从 0.1.0 升级到 0.2.0：同步 `addons/web-reader/` 和 `deploy/` 的新代码；只将已有 `deploy/.env` 的 `WEB_READER_IMAGE` 改为 `browser-agent-web-reader:0.2.0`，保留绑定地址、端口和密钥。执行上面的 reader-only `up -d --build web-reader`，运行 `deploy/check.py` 确认健康版本及 `structure_version=1`。再重启后端、重新加载扩展。不要再次初始化或重建已有 SearXNG。

新后端兼容旧服务，但会标记 `legacy_text`，不能恢复旧服务已经打乱的段落。结构修复必须在远端 reader 也更新后才生效。

不要为了停止 reader 执行其他项目的 `down`，也不要使用 `down -v` 删除数据卷。此服务不持久化正文，Compose 中唯一持久卷属于可选 SearXNG 缓存。

密钥仅保存在本地配置/容器环境，具备 Docker 管理权限的人能查看容器环境。排障不要粘贴完整 `docker inspect`、`docker compose config` 或真实 `.env`；配置验证使用 `config --quiet`。

## 7. 发布和验收

当前实现的具体验证结果见 [验收记录](../addons/web-reader/VALIDATION.md)。本地已完成 Python 服务、实际模型链路及 Compose 静态校验；容器构建/启动尚待有 Docker 的环境验证。

- 固定镜像标签/摘要、Python 依赖随版本更新；本地构建不需要发布账号。
- 发布部署文档和 `addons/web-reader/tests`；根目录已有的本地 `test/` 和 `docs/` 继续忽略。
- 从独立 Python 环境运行服务测试；检查配置初始化重复执行不覆盖任何文件。
- 有 Docker 的环境执行两种 profile 的构建/启动验收，确认 reader-only 不会创建 SearXNG。
- 对公开文章、中文文档、表格和长页面执行显式抓取，再对比聊天仅摘要/正文增强的结果。
- 模型请求中必须实际出现正文，引用编号、读取状态和会话恢复都需要验证；截图卡片显示成功不等于整条链路正确。

参考：[Compose Profiles](https://docs.docker.com/compose/how-tos/profiles/)、[Docker 网络](https://docs.docker.com/compose/how-tos/networking/)、[Trafilatura](https://trafilatura.readthedocs.io/)。
