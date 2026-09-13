# SVG 自动化修复与回归结果

验证日期：2026-09-12（America/Los_Angeles）。本次只修改扩展自动化实现和测试，不修改 chat、后端接口、模型配置或 `.env`。

## 实现

- `buildSnapshotLookup` / `constructEnhancedTree`：矩形与文档滚动按同一尺度转换为 CSS 坐标，包含 iframe 内容区原点；不把客户区尺寸 `clientRects` 当作视口位置。父子关系构建后再分类，对缺少有效快照几何的交互候选做有限并发的真实矩形复核。
- `isInteractive`：使用快照 `isClickable`，保留禁用、inert 和隐藏过滤，不把 `title` 本身当作点击证据。
- `extractText` / `applyBoundingBoxFilter`：保留包装控件标签，兼容没有自身布局框的 `display: contents`；装饰图标去重，独立子控件保留。名称补全不替换点击目标，不跨文档或独立操作分支借用标签。
- `indexMap`：内部保存 frame 路径与独立子控件，外部 ActionResult/PageState 接口不变。点击父控件时避开独立子控件区域。
- `doClick`：检查候选点及各级 iframe 遮挡，移动鼠标后再次检查；最多派发一次真实点击。删除无几何/遮挡时的 JS 穿透点击，以及复选框状态未更新时的重复点击。
- `getActionGeometry`：点击前读取最新 frame 几何。跨进程 iframe 的真实鼠标事件发往其自身 session，上层视口与遮挡检查仍在顶层坐标中完成。
- `runtimeValue` / `dispatchRealClick`：脚本异常不再当作成功；按下异常也尝试释放鼠标，未完整确认时报告结果不确定，不自动重复点击。

## 验证结果

| 测试组 | 通过 | 失败 |
| --- | ---: | ---: |
| SVG/坐标/执行单元测试 | 26 | 0 |
| 真实 Chromium + 实际扩展观察/执行 | 25 | 0 |
| 原有知识库刷新回归 | 8 | 0 |
| 原有服务端会话前端回归 | 6 | 0 |
| 合计 | 65 | 0 |

Node.js v22.15.0。生产脚本与浏览器测试脚本通过语法检查，`git diff --check` 通过。

真实浏览器用例包含：原始行内 `span title + SVG use` 结构、SVG 自身监听、链接、父子独立动作、全遮挡与部分遮挡、异步复选框、100%/125%/150%/200% 缩放与滚动、同源及跨进程 iframe、父页面遮罩、观察后变为禁用/隐藏、开放 Shadow DOM、`display: contents` 标签。

成功点击断言实际事件计数；主要 SVG 用例同时验证 `isTrusted=true`。阻挡用例断言返回失败且没有产生点击事件，不以返回字符串或 URL 变化代替事件验证。

## 复跑

从仓库根目录执行：

```powershell
node backend/test/audit_review/svg_controls.test.cjs
node backend/test/audit_review/kb_refresh.test.cjs
node backend/test/audit_review/server_frontend.test.cjs
```

浏览器测试通过 Playwright CLI 的 `run-code` 运行 `svg_controls.browser.js`，把 `__SVG_FIXTURE_HTML__` 替换为 `fixtures/svg_controls.html` 的 JSON 字符串。测试会拦截 `svg-audit.test` 与 `svg-child.test`，只返回合成页面，不访问真实站点。

在已安装 Playwright CLI 的 Windows 环境中，可按以下方式加载测试脚本；`$svgCli` 是安装环境中的 CLI 入口路径：

```powershell
$svgScript = Get-Content backend/test/audit_review/svg_controls.browser.js -Raw -Encoding UTF8
$svgFixture = Get-Content backend/test/audit_review/fixtures/svg_controls.html -Raw -Encoding UTF8 | ConvertTo-Json -Compress
$svgScript = $svgScript.Replace('__SVG_FIXTURE_HTML__', $svgFixture)
node $svgCli -s=svg-regression run-code $svgScript
```

运行前创建名为 `svg-regression` 的隔离 Chromium 会话，并通过启动参数加载本仓库 `extension` 目录。当前开发环境使用 `output/playwright/context-extension.json`；该文件是本机测试配置，不作为生产依赖。必须使用新的临时 profile，或确保开发扩展已真正重新加载：只关闭页面/重开会话可能仍命中旧 service worker 缓存。测试入口会检查新版函数是否存在。

## 范围与限制

- 未对真实账号执行签到、转账、登出等业务操作；真实网站的模型选路仍需要用户复测。
- 点击成功表示真实事件已完整派发，不等于目标业务已完成；沿用后续页面观察确认业务结果。
- 旋转或翻转的跨进程 iframe 会明确停止，不冒险套用轴对齐坐标换算；本轮验证的是普通、缩放及滚动 iframe。
- 需要在用户浏览器中重新加载扩展并重开侧栏才能使用新实现；本次未重启或修改后端。
- 未提交、未推送。
