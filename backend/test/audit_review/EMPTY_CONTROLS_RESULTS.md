# 无名称控件识别与视觉编号验收

日期：2026-09-12（America/Los_Angeles）。基线：agent-slim / 20d4568。

## 实现范围

- 移除仅凭尺寸、class、搜索关键词认定可点击的规则。保留原生控件、事件证据、可访问角色、键盘控件；指针样式为弱候选，不给继承指针的装饰后代重复编号。
- 保留无布局事件包装层（display:contents）的首层可见子控件，避免收紧筛选后漏掉原有 SVG 控件。
- 名称与链接目标提示分离。仅提供短路由名和受限动作参数；不传原始 href、未知查询参数、账号、fragment 或可执行 URL。合成 formhash/token/signature 检查通过。
- 后端元素列表保留 role、无名称状态、目标提示和 bbox。带坐标的元素不合并成丢失位置的分组。
- 指定 CDP 标签页完成元素与截图采集，不再依赖窗口当前活动标签截图。发现导航、滚动或视口变化时最多重采一次；仍变化则清除编号映射、要求重新观察。
- 在扩展内部对截图副本绘制紫色编号框/引线，不修改目标网页 DOM。主要标注无名称、同名和图标名称控件；标签放不下时不虚报编号，仍提供文字坐标。
- PageState 显式传递 screenshot_marked 和实际绘制的 screenshot_mark_ids。无图、无标注及标注失败的提示均与实际状态一致。
- 日志补充被选中目标的候选来源、名称来源、坐标和安全目标提示。真实点击执行器保持原有几何、遮挡和单次派发机制。

## 验证结果

| 测试组 | 结果 |
| --- | --- |
| 原 SVG 单元测试 | 26 通过 |
| 新增候选筛选、链接提示、标注排版、观察重采测试 | 15 通过 |
| 服务端会话前端回归 | 6 通过 |
| 知识库刷新前端回归 | 8 通过 |
| 原 SVG 真实 Chromium 回归 | 25 通过 |
| 新增空控件真实 Chromium 回归 | 10 通过 |
| qwen3.8-27b 自主选点并执行 | 6/6 通过 |
| 完整 audit_review Python 回归 | 57 通过，19 项既有 xfail |

19 项 xfail 是先前已知的旧协议、知识库及其他问题，不是测试通过；未宣称全仓所有测试全绿。

真实浏览器覆盖背景图空链接、装饰数字过滤、标题与 CSS 生成文字对照、100%/125%/150%/200% 缩放、滚动、同源与跨进程 iframe、指定标签截图、错误图片回退、打乱顺序且链接相同的控件。截图已人工式视觉检查（实际查看生成图），框与编号对应正确。

真实模型使用配置中的 qwen3.8-27b，输入由当前生产 build_messages 与 PageState 生成，并包含实际扩展采集及渲染后的截图。没有向模型提供预期元素 ID。

第一组分别选择“签到、兑换、导出”；第二组打乱按钮位置并将所有 URL 改为相同值，再问同样三个操作。六次均由模型给出正确 index，然后通过原扩展真实点击执行器执行；每次检查事件恰好一次、isTrusted=true，且合成页面结果正确。仅模型名称读取配置，没有修改配置。

浏览器回归曾发现 display:contents 漏候选，修复后原 25 项全部通过。iframe 几何断言另发现当前 Playwright 的 OOPIF boundingBox 漏计边框，改为独立读取父 iframe 和子节点 DOM 矩形及边框计算；生产几何没有因此做特例调整。

## 复跑

在仓库根目录：

```powershell
node --test backend/test/audit_review/svg_controls.test.cjs backend/test/audit_review/empty_controls.test.cjs backend/test/audit_review/server_frontend.test.cjs backend/test/audit_review/kb_refresh.test.cjs
.\backend\.venv\Scripts\python.exe -X utf8 -B -m pytest backend/test/audit_review -q -p no:cacheprovider
```

浏览器脚本为 empty_controls.browser.js，将 __EMPTY_FIXTURE_HTML__ 替换为 fixtures/empty_controls.html 的 JSON 字符串后，由 Playwright CLI run-code 执行。必须使用加载本仓库扩展的新隔离 profile。脚本拦截 empty-audit.test 和 empty-child.test，仅提供合成页面。

脚本将最终合成观察放在测试页 window.auditObservation。将 `{ "state": <该观察> }` 通过 stdin 交给 live_empty_controls.py，即可使用配置模型重新选点。该脚本仅输出模型选择；还需将选择交给原扩展执行，检查事件和合成结果，不能仅以脚本返回 passed 代替点击验收。

## 生效与边界

- 未操作真实网站或真实账号，未写真实聊天、记忆或知识库数据；未修改 .env，未提交、未推送。
- 真实模型测试直接加载修改后的上下文构建代码，不是声称当前运行中的 HTTP 后端已重启。
- 使用时需重新加载扩展、重开侧栏，并重启后端加载新的截图说明和字段。
- 标注为视口轴对齐矩形；沿用原执行器对特殊旋转/翻转跨进程 iframe 的限制。导航/滚动检查不等于任意动态网页的原子快照。
- 不保证任意真实网站任务必然成功；事件已派发仍不等于业务完成，需后续页面证据确认。
