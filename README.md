# 私人助手

Chrome 侧边栏 LLM 助手，支持普通聊天、网页摘要、图片上传、框选截图、公式提取提示和 Markdown 渲染。

## 功能

- 侧边栏聊天，支持流式输出。
- Markdown 渲染，代码块带复制按钮。
- 本地图片上传和 Base64 图片预览。
- 图片工具支持本地文件、`data:image/...;base64,...`、HTTP/HTTPS 图片 URL。
- 用户主动框选当前标签页区域截图，并作为图片附件发送。
- 用户确认后读取当前网页前 5000 字，填入输入框，不会自动发送。

## 本地安装

1. 打开 `chrome://extensions`。
2. 开启“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择本仓库目录。
5. 打开侧边栏设置，填写 API Base URL、API Key 和模型名。

## API 配置

默认内置 API 地址：

```text
https://api.openai.com/v1
```

也可以添加可信的 HTTPS OpenAI-compatible API：

1. 在设置页把 API Base URL 改成目标地址，例如 `https://example.com/v1`。
2. 输入该服务对应的 API Key。
3. 点击“保存配置”。
4. 浏览器会请求访问该 API 域名的权限，确认后该地址会加入本机允许列表。

切换 API 地址时需要重新输入该服务对应的 API Key，避免把旧 Key 发到错误服务。API Key 保存在 `chrome.storage.session`，不会写入 `chrome.storage.local`。浏览器重启后可能需要重新输入。

## 权限说明

- `sidePanel`：显示 Chrome 侧边栏。
- `storage`：保存模型名、API 地址和首次隐私确认状态。
- `activeTab`：用户主动打开扩展后，允许读取或截图当前标签页。
- `scripting`：用户点击“总结当前网页”或“框选截图”时注入一次性脚本。
- `contextMenus`：右键发送选中文本或图片到侧边栏。
- `permissions`：在用户操作时请求自定义 API、图片站点、网页读取或截图兜底所需的运行时权限。
- `host_permissions: https://api.openai.com/*`：调用默认 OpenAI API。
- `optional_host_permissions`：仅在用户添加自定义 API、输入图片 URL、读取网页或截图授权不足时按需请求。

## 图片和截图

- 本地图片和 Data URL 会直接转换为 PNG Base64。
- HTTP/HTTPS 图片 URL 会在下载前请求该站点访问权限。
- HTTP、本机或内网图片 URL 会额外弹出风险确认。
- 框选截图会在点击“框选截图”后申请所需权限，再进入框选流程。
- Chrome 内置页、Chrome Web Store、扩展页面或受保护页面可能仍无法注入框选层或截图。
- 修改 `manifest.json` 后，需要在 `chrome://extensions` 里重新加载扩展。

## 隐私

请阅读 [PRIVACY.md](./PRIVACY.md)。用户输入、主动选择的网页文本、上传图片和框选截图会发送到配置的模型 API。
