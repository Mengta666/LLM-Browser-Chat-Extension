# 编辑器输入与按键验收

## 改动范围

保留 `type / clear / focus / press_key` 动作，不新增网站专用动作或固定选择器。`agent_editing.js` 统一解析原生控件、普通 contenteditable、经过实例与包装节点校验的 CodeMirror 5；描述对象只在一次动作内持有，不跨观察复用。

- 观察：增加 `editor_type / editable / read_only / focused`，读取当前值的有限预览，密码框不返回预览；CM5 内部输入代理不单独编号。焦点不依赖 HTML id，区分 frame。
- 输入：默认全文替换；`clear=false` 为当前选区插入，而非自动追加。CM5 使用 `replaceSelection` 写入、`getValue` 回读；普通富文本使用浏览器 `Input.insertText`；原生字符键保留键盘事件，Unicode 使用文本插入。
- 校验：执行前和后续输入前检查目标、只读和焦点；按键按下后再次确认焦点再发字符。目标替换、拒绝写入或回读不符时返回失败 / 部分执行，不自动重放。
- 按键：修正字母、物理键码、修饰键和字符事件。文本换行不生成 Enter；单行控件拒绝多行输入，且在清空前拒绝。
- 控制：新增编辑接口写入仍受原始 CDP 命令占用、取消和迟到回调保护；不改变任务协议。

## 测试环境与边界

采用 Playwright 运行时启动独立 Chromium，加载实际扩展，操作本地合成页与真实 CodeMirror **5.65.16**（textarea / contenteditable 两种输入模式）。不是仿造的编辑器对象。

测试不读取用户浏览器配置，不访问论坛、账号或用户模型，不发送真实回复。完整侧边栏测试启动隔离 Python 后端，实际 HTTP 路由与循环照常执行，只有模型决策使用可控替身。未重启用户正在运行的后端。

## 验收项目

本轮结果：Node **114 passed**；隔离 Python **110 passed / 12 xfailed**（原有已知问题）；真实浏览器编辑器 **15 / 15**、动作控制 **6 / 6**、实际侧边栏与隔离后端 **8 / 8**。修改文件通过 JS 语法检查和 `git diff --check`。

`live_agent_editing.cjs` 覆盖：

1. 真 CM5 / 多编辑器 / 只读的观察识别；隐藏代理与伪装 class 不误当编辑器。
2. 原生 input / textarea、普通和 plaintext-only 富文本的中文、emoji、多行、清空。
3. 空行、连续空行、末尾换行、连续空格、组合字符、Tab 的回读。
4. 可编辑后代解析到编辑根节点；普通选区插入；无法映射的复杂选区在写入前拒绝。
5. 真 CM5 两种模式的全文替换、选区插入、清空、change 事件及不同实例互不串写。
6. 无 id 的输入代理正确报告焦点；字母、Ctrl+A、Shift+1、无 index 按键实际生效。
7. 原生选区、日期、数字输入。
8. 只读 / 假编辑器不写入；可聚焦不等于可输入；单行框多行输入不先清空。
9. input / keydown 期间抢焦点、替换节点，不往其他输入框继续派发字符。
10. 多光标插入与 CM5 nocursor 拒绝执行。
11. beforeChange 拒绝写入时回读失败，只尝试一次。
12. 同源及跨进程 iframe 分别输入；不把未激活 frame 的旧 activeElement 当作当前焦点。
13. 全部文本输入 / 清空流程的 Enter 和提交计数均为零。
14. Unicode 命令迟到时取消 / 结束保持占用，确认后自动收尾并允许新任务。
15. CM5 编辑接口写入的迟到回调执行相同保护，不重写已产生的内容。

实测中发现并修正：CM5 contenteditable 模式逐字符插入会丢失末尾换行，改用公开文档接口；普通富文本逐字符换行也会产生额外空行，改用完整文本插入。富文本清空后的占位 BR 不视为真实内容，不通过删除展示 DOM 修复。

## 复现

从仓库根目录运行：

```powershell
node --test --experimental-test-isolation=none --test-reporter=spec backend/test/audit_review/*.test.cjs
.\backend\.venv\Scripts\python.exe -X utf8 -B -m pytest backend/test/audit_review -q -p no:cacheprovider --tb=short
node backend/test/audit_review/live_agent_editing.cjs <playwright模块路径> <Chromium可执行文件路径> [本地CM5库目录]
node backend/test/audit_review/live_agent_control.cjs <playwright模块路径> <Chromium可执行文件路径>
node backend/test/audit_review/live_agent_panel.cjs <playwright模块路径> <Chromium可执行文件路径>
```

编辑器测试默认从 jsDelivr 读取固定版本 npm 发布包的 `codemirror.js` 和 `codemirror.css`，只用于测试页；可用第四个参数提供对应版本的本地目录离线运行。浏览器配置与截图保留在已忽略的 `output/playwright`，未删除已有资料。

## 未涵盖的能力

- CM6、Monaco、ProseMirror / TipTap 等没有专用文档模型适配，不宣称所有富文本框架均已支持。
- 普通 contenteditable 回读的是纯文本表示，换行按编辑块规范化，浏览器生成的 NBSP 按空格处理；不保证 HTML / 格式 / 不换行空格语义完全保真。
- 富文本选区无法与文本位置可靠对应时，`clear=false` 会在写入前拒绝；不会擅自改成全文替换。
- 回读确认的是当时的内容，不是任意异步业务校验、自动保存或发布成功。用户要求不发送时，不添加提交动作，但无法约束网站自行监听 input 的业务副作用。
- 未操作用户提供的真实论坛页面，也未进行真实模型决策验收。

启用必须重新加载 Chrome 扩展并关闭重开侧边栏，同时重启后端加载更新后的提示规则；仅刷新网页不够。
