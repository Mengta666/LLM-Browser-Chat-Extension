# Web Reader 0.2.0

独立的 HTML 正文提取服务：FastAPI 接口、受限下载器、Trafilatura 提取。部署入口位于 [../../deploy/README.md](../../deploy/README.md)。

## 接口

`GET /health` 无需鉴权，返回 `status/service/version/api_version`，只用于进程就绪检查。

`POST /v1/extract` 使用 `Authorization: Bearer <WEB_READER_API_KEY>`，请求体：

```json
{"url":"https://www.python.org/downloads/","timeout_seconds":15}
```

`url` 及编码后的网址最长 4096 字符；`timeout_seconds` 为 1～30 秒，包括子进程启动、DNS、下载、提取。未知字段拒绝，不允许调用方传 Cookie、请求头或代理。

成功响应字段：

| 字段 | 含义 |
| --- | --- |
| `status` | `ok` |
| `url` / `final_url` | 原始网址 / 实际最终网址 |
| `title` / `content` | 标题（最多 500 字符） / 提取的正文文本（保留基本结构） |
| `content_length` | 截取前提取文本的字符数 |
| `truncated` | 返回文本是否因 100000 字符或 2048 块上限而被节选，按完整块/句子停止 |
| `structure_version` | 结构协议版本，当前为 1；原有成功字段保持兼容 |
| `blocks` | 最多 2048 个有序块，只保存偏移，不重复正文；字段为 `id/type/start/end/section/level` |
| `extracted_date` | 提取出的页面日期，可能为 null，也可能不准确 |
| `fetched_at` | UTC 抓取时间，不是发布时间 |
| `elapsed_ms` | 子进程内下载与提取耗时，不包含调度开销 |

块类型为 `heading/paragraph/list/table/code`。`start/end` 是 `content` 中 Unicode 字符的左闭右开偏移；`section=-1` 表示标题前正文，其余指向所属标题块的 `id`。标题 `level` 为 1～6，其他块为 0。列表、表格（含表头）、代码整体输出；代码保留缩进。正文按树顺序保留行内文字和尾随文本，不把链接地址插入句子，也不根据短行猜标题。无法在上限内返回完整内容时显式节选，超大代码/表格不会从中间截断。

后端按主题选完整段落和相邻窗口，重叠去重后恢复原始顺序，跳过的区间显示 `[中间内容已省略]`。结构信息仅用于当前提问内的选段，不保存完整树或 HTML 到会话。正文读取成功但未纳入上下文会有独立状态，不代表模型已读到。

读取失败返回 HTTP 200、`{"status":"error","error_code":"..."}`。接口层鉴权失败为 401、请求体过大为 413、参数错误为 422、容量已满为 429。这些结果都不包含异常堆栈、密钥或正文。

常用错误：`blocked_address`、`blocked_port`、`dns_error`、`fetch_timeout`、`connection_error`、`http_403`、`page_too_large`、`unsupported_content_type`、`unsupported_encoding`、`empty_content`、`interstitial_page`。

## 安全和限制

- 只允许 HTTP:80 和 HTTPS:443；拒绝 URL 用户名/密码、控制字符、IPv6 zone ID。
- 每次 DNS 解析的全部地址必须为允许的公网地址；每次重定向重新检查，最多 3 次。
- 连接固定到已校验 IP，保留原始 Host 和 TLS SNI/证书验证，防止校验后第二次解析 DNS。
- 默认每页线上数据和解压后数据都限制为 2 MiB；支持 identity、gzip、deflate。
- 默认同时最多 3 个任务（每个 Uvicorn worker）；部署固定单 worker。超时或连接断开终止实际子进程。
- 不继承 HTTP_PROXY 等环境代理，不转发访问正文服务的密钥，不使用登录态。需要代理的网络暂不支持，应使用可直连公网的部署主机。
- 不执行 JavaScript，不处理 PDF、验证码或登录后的内容；提取器并不能可靠识别所有挑战页，`ok` 也不代表事实正确。
- 提取上限、正文段落筛选和来源时效仍会影响覆盖范围；`truncated=true` 不能宣称已读取全文。
- 不持久化网页内容；客户端本轮缓存不属于服务端缓存。

不要将接口无保护开放到公网。默认回环绑定；跨主机时使用可信私网/VPN或 HTTPS 反向代理，按客户端限制访问，确保 Docker 端口发布受实际防火墙规则约束。

## 独立离线测试

使用 Python 3.13，另建环境，不要安装进其他项目的环境。依赖全部来自本服务目录：

```bash
python3 -m venv .venv-reader
.venv-reader/bin/python -m pip install -r addons/web-reader/requirements-test.txt
.venv-reader/bin/python -m pytest addons/web-reader/tests -q
```

Windows 将 Python 路径换成 `.venv-reader\Scripts\python.exe`。此套件与后端测试分开运行，避免两个独立应用的 `app` 模块名冲突。用例不会访问真实网页；部分用例会创建并终止测试子进程。

容器测试可使用 Docker 单独构建启动，再按照部署文档执行 `/health` 和真实 URL 检查。没有 Docker 守护进程时，Python 测试不能替代镜像构建和容器验收。

本版本已完成的验证及尚未覆盖的范围见 [验收记录](VALIDATION.md)。
