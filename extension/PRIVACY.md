# 隐私政策

本扩展用于在 Chrome 侧边栏中调用用户配置的 OpenAI 或 OpenAI-compatible API，帮助用户处理聊天、网页摘要、图片分析和截图内容。

## 收集和处理的数据

扩展只在用户主动操作时处理以下数据：

- 用户在输入框中输入的文本。
- 用户上传、粘贴或框选截图得到的图片。
- 用户在图片工具中输入的 Data URL 或 HTTP/HTTPS 图片 URL。
- 用户点击“总结当前网页”并确认后读取的当前网页前 5000 字文本。
- 用户在设置中输入的 API Key。
- 用户选择的模型名和 API 地址。

扩展不会在后台持续读取网页内容，不会自动截图，也不会自动发送网页内容。

## 数据用途

上述数据仅用于完成用户主动请求的 LLM 问答、摘要、翻译、图片分析和公式提取等功能。

## 数据共享

用户输入、网页文本和图片内容会发送到用户在设置中选择的 API 服务。默认服务为 OpenAI API：

```text
https://api.openai.com
```

如果用户添加自定义 HTTPS OpenAI-compatible API，上述数据会发送到用户明确添加并授权的自定义 API 域名。

扩展本身不向广告平台、数据经纪商或其他第三方出售、出租或共享用户数据。

## API Key 存储

API Key 保存在 `chrome.storage.session` 中，仅用于当前浏览器会话。扩展不会把 API Key 写入项目文件，也不会把 API Key 写入 `chrome.storage.local`。用户可以在设置页点击“清除 API Key”。

## 远程图片 URL

当用户输入 HTTP/HTTPS 图片 URL 并点击下载转换时，扩展会请求访问该图片所在站点，并下载该图片用于本地 Base64 转换。扩展会限制图片类型、大小和分辨率。对于 HTTP、本机或内网图片 URL，扩展会在加载前显示额外确认。

当用户输入 `data:image/...;base64,...` 时，扩展会在本地解码并转换为 PNG Base64，不会为了读取该 Data URL 请求外部站点权限。

## 框选截图

框选截图只会在用户点击“框选截图”后触发。扩展会先请求所需截图权限，再注入一次性框选层。Chrome 内置页、Chrome Web Store、扩展页面或受保护页面可能无法被注入或截图。

## Limited Use

The use of information received from Google APIs will adhere to the Chrome Web Store User Data Policy, including the Limited Use requirements.
