// 独立扩展配置、实际侧边栏消息、真实 Python agent 路由；只有模型决策被替换。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { chromium } = require(process.argv[2] || 'playwright');
const root = path.resolve(__dirname, '../../..');
const fixture = `<!doctype html><meta charset="utf-8"><title>Panel lifecycle fixture</title>
<button id="submit">合成提交</button><span id="count">0</span>
<input id="text" aria-label="普通输入" value="old">
<input id="readonly" aria-label="只读输入" value="keep" readonly>
<input id="revert" aria-label="拒绝修改" value="keep">
<select id="choice" aria-label="普通选择"><option value="a">Alpha</option><option value="b">Beta</option><option disabled>Disabled</option></select>
<select id="revert-select" aria-label="拒绝选择"><option value="a">Alpha</option><option value="b">Beta</option></select>
<textarea id="area" aria-label="多行输入">old</textarea><div id="editable" contenteditable="true" role="textbox" aria-label="富文本">old</div>
<script>window.events=[];document.querySelector('#submit').onclick=()=>{const c=document.querySelector('#count');c.textContent=Number(c.textContent)+1;};
document.querySelector('#revert').oninput=e=>{events.push('revert-input');e.target.value='keep';};
document.querySelector('#revert-select').onchange=e=>e.target.value='a';
document.querySelector('#text').oninput=e=>events.push(e.target.value);</script>`;
const pause = ms => new Promise(r => setTimeout(r, ms));
async function until(check, description, ms = 15000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) { if (await check()) return; await pause(30); }
  throw new Error(`Timed out: ${description}`);
}

(async () => {
  const backend = spawn(process.argv[4] || path.join(root, 'backend/.venv/Scripts/python.exe'),
    ['-X', 'utf8', '-B', path.join(__dirname, 'live_agent_backend.py')], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  let backendOutput = '', backendErrors = '', port, context, panel;
  backend.stdout.on('data', chunk => { backendOutput += chunk; port = backendOutput.match(/PORT=(\d+)/)?.[1]; });
  backend.stderr.on('data', chunk => { backendErrors += chunk; });
  backend.on('error', error => { backendErrors += error.message; });
  const server = http.createServer((req, res) => { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(fixture); });
  const results = [];
  fs.mkdirSync(path.join(root, 'output/playwright'), { recursive: true });
  const profile = fs.mkdtempSync(path.join(root, 'output/playwright/agent-panel-'));
  try {
    await until(() => { if (backend.exitCode != null) throw new Error(backendErrors); return !!port; }, 'isolated backend startup');
    const api = `http://127.0.0.1:${port}`;
    await until(async () => { try { return (await fetch(`${api}/audit/state`)).ok; } catch { return false; } }, 'backend HTTP ready');
    const state = async () => (await fetch(`${api}/audit/state`)).json();
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    context = await chromium.launchPersistentContext(profile, {
      channel: 'chromium', executablePath: process.argv[3], headless: true,
      args: [`--disable-extensions-except=${path.join(root, 'extension')}`, `--load-extension=${path.join(root, 'extension')}`],
    });
    await context.route('**/*', route => {
      const url = route.request().url();
      return url.startsWith(api + '/') || url.startsWith(origin + '/') || url.startsWith('chrome-extension:')
        ? route.continue() : route.abort();
    });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    await worker.evaluate(async api => {
      await chrome.storage.local.set({ apiUrl: api, customApiBaseUrls: [api], modelName: 'offline-test', privacyNoticeAccepted: true });
    }, api);
    const page = await context.newPage();
    await page.goto(`${origin}/fixture`);
    const tabId = await worker.evaluate(async () => (await chrome.tabs.query({})).find(t => t.url?.endsWith('/fixture')).id);
    panel = await context.newPage();
    await panel.goto(`chrome-extension://${new URL(worker.url()).hostname}/sidepanel.html`);
    await panel.locator('#chatInput').waitFor();
    await panel.waitForFunction(() => document.querySelector('#modelName')?.value === 'offline-test');
    const startUI = async task => {
      await panel.locator('#chatInput').fill(`/browser-operation ${task}`);
      await page.bringToFront();
      await panel.locator('#sendBtn').click();
    };
    const finishUI = async () => {
      await until(async () => await panel.locator('.agent-header').count() > 0 &&
        await panel.locator('.agent-btn-cancel').count() === 0 && await panel.locator('#sendBtn').isEnabled(), 'panel cleanup');
      return panel.locator('.agent-header').last().innerText();
    };
    const message = data => panel.evaluate(data => chrome.runtime.sendMessage(data), { tabId, ...data });
    const ended = () => worker.evaluate(tabId => agentExecution.tabs.get(tabId)?.ended === true, tabId);

    // 使用真实 DOM 的发送按钮，不直接调用 runAgentTask 或 Controller。
    await startUI('normal');
    assert.equal(await finishUI(), '任务完成');
    assert.equal(await page.locator('#count').innerText(), '1');
    assert.equal(await page.locator('#text').inputValue(), '');
    assert.equal(await page.locator('#choice').inputValue(), 'b');
    assert.ok((await page.evaluate(() => events)).includes('ABCD'));
    assert.equal(await ended(), true);
    let audit = await state();
    const normal = Object.keys(audit.sessions)[0];
    const normalTrace = audit.trace.filter(t => t.session_id === normal && t.observation_id);
    assert.equal(normalTrace.length, 5);
    assert.equal(new Set(normalTrace.map(t => t.observation_id)).size, 5);
    assert.equal(normalTrace.slice(1).every(t => t.action_result.success), true);
    results.push('实际侧边栏→HTTP agent 循环→真实点击/输入/选择/清空→收尾');

    await startUI('single');
    assert.equal(await finishUI(), '任务完成');
    assert.equal(await page.locator('#count').innerText(), '2');
    results.push('正常收尾后，同标签直接开始新任务');

    await startUI('failures');
    assert.equal(await finishUI(), '任务未完成');
    audit = await state();
    const failedId = Object.keys(audit.sessions).find(id => audit.sessions[id].task === 'failures');
    const failedSteps = audit.trace.filter(t => t.session_id === failedId && t.action_result);
    assert.equal(failedSteps.length, 4);
    assert.equal(failedSteps.every(t => t.action_result.success === false), true);
    assert.equal(await page.locator('#readonly').inputValue(), 'keep');
    assert.equal(await page.locator('#revert').inputValue(), 'keep');
    assert.equal(await page.evaluate(() => events.filter(e => e === 'revert-input').length), 1);
    assert.equal(await page.locator('#revert-select').inputValue(), 'a');
    results.push('不存在选项、只读清空、清空被撤销、选择被撤销均真实返回失败且继续新观察');

    await panel.evaluate(() => {
      window.originalMessage = chrome.runtime.sendMessage.bind(chrome.runtime);
      chrome.runtime.sendMessage = m => m.type === 'AGENT_CONTROL' && m.command === 'probe' ? Promise.resolve(undefined) : originalMessage(m);
    });
    const beforeLegacy = (await state()).trace.length;
    await startUI('single');
    assert.equal(await finishUI(), '任务未完成');
    assert.match(await panel.locator('#chatHistory').innerText(), /重新加载扩展/);
    assert.doesNotMatch(await panel.locator('.agent-header').last().innerText(), /旧动作|锁定/);
    assert.equal((await state()).trace.length, beforeLegacy);
    await panel.evaluate(() => { chrome.runtime.sendMessage = originalMessage; });
    results.push('模拟旧后台无回复：无动作、无后端请求、正确提示重新加载扩展');

    await worker.evaluate(() => {
      globalThis.originalObserve = managedAgentObserve;
      globalThis.observeAttempts = 0;
      managedAgentObserve = async request => {
        if (++observeAttempts === 2) throw new Error('synthetic transient observation failure');
        return originalObserve(request);
      };
    });
    const countBeforeRecovery = Number(await page.locator('#count').innerText());
    await startUI('single');
    assert.equal(await finishUI(), '任务完成');
    assert.equal(await worker.evaluate(() => globalThis.observeAttempts), 3);
    assert.equal(Number(await page.locator('#count').innerText()), countBeforeRecovery + 1);
    await worker.evaluate(() => { managedAgentObserve = originalObserve; });
    results.push('动作后观察暂时失败，真实侧边栏自动重观察继续，点击不重放');

    await worker.evaluate(() => {
      globalThis.originalStoreRead = agentExecution.store.read;
      globalThis.startHeld = false;
      globalThis.cancelCountBeforeStart = agentExecution.cancelledSessions.size;
      agentExecution.store.read = async tabId => {
        globalThis.startHeld = true;
        await new Promise(resolve => { globalThis.releaseStart = resolve; });
        return originalStoreRead(tabId);
      };
    });
    const traceBeforeCancel = (await state()).trace.length;
    await startUI('single');
    await until(() => worker.evaluate(() => globalThis.startHeld), 'start storage read');
    await panel.locator('.agent-btn-cancel').click();
    await until(() => worker.evaluate(() => agentExecution.cancelledSessions.size > cancelCountBeforeStart), 'cancel received');
    await worker.evaluate(() => { agentExecution.store.read = originalStoreRead; releaseStart(); });
    assert.match(await finishUI(), /已停止/);
    assert.equal((await state()).trace.length, traceBeforeCancel);
    assert.equal(Number(await page.locator('#count').innerText()), countBeforeRecovery + 1);
    results.push('启动读取尚未结束时点击停止，迟到启动不复活，也不创建后端会话');

    // 暂留真实 mousePressed 回调直到 UI 停止等待，再释放，检验迟到收尾。
    await worker.evaluate(() => {
      globalThis.originalSendCommand = chrome.debugger.sendCommand;
      globalThis.pressHeld = false;
      chrome.debugger.sendCommand = (target, method, params, callback) => originalSendCommand(target, method, params, result => {
        if (method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed') {
          globalThis.pressHeld = true; globalThis.releasePress = () => callback(result);
        } else callback(result);
      });
    });
    await startUI('single');
    await until(() => worker.evaluate(() => globalThis.pressHeld), 'mouse press in flight');
    await panel.locator('.agent-btn-cancel').click();
    assert.match(await finishUI(), /旧动作尚未确认/);
    const busy = await message({ type: 'AGENT_CONTROL', command: 'start', sessionId: 'premature' });
    assert.equal(busy.code, 'tab_busy');
    await worker.evaluate(() => { chrome.debugger.sendCommand = originalSendCommand; releasePress(); });
    await until(ended, 'late command auto cleanup');
    const countAfterStop = await page.locator('#count').innerText();
    await startUI('single');
    assert.equal(await finishUI(), '任务完成');
    assert.equal(Number(await page.locator('#count').innerText()), Number(countAfterStop) + 1);
    results.push('停止时未结束的真实指令保持占用，迟到确认后自动收尾，下一任务可启动且不重放');

    // 直接消息仅用于验证控件边界，仍走实际 onMessage 与 CDP。
    const sid = 'controls';
    assert.equal((await message({ type: 'AGENT_CONTROL', command: 'start', sessionId: sid })).ok, true);
    let sequence = 0;
    const act = async (type, htmlId, params = {}, before = async () => {}) => {
      const observation = await message({ type: 'AGENT_OBSERVE', sessionId: sid, observationId: `controls:${++sequence}`, includeScreenshot: false });
      assert.equal(observation.ok, true);
      const pageState = observation.pageState;
      const index = pageState.interactive_elements.find(e => e.html_id === htmlId)?.id;
      assert.ok(index != null, `fixture element ${htmlId} must be observed`);
      await before();
      return (await message({ type: 'AGENT_EXECUTE', sessionId: sid, action: {
        type, index, params, action_id: `control-action:${sequence}`, observation_id: pageState.observation_id,
      } })).result;
    };
    for (const id of ['text', 'area', 'editable']) {
      assert.equal((await act('clear', id)).success, true);
      assert.equal((await act('clear', id)).success, true);
    }
    assert.equal(await page.locator('#area').inputValue(), '');
    // 原生删除可以留下占位 <br>；内容为空不要求破坏性删除编辑器 DOM。
    assert.equal(await page.locator('#editable').textContent(), '');
    assert.equal((await act('select', 'choice', { option_text: 'Disabled' })).success, false);
    const removed = await act('clear', 'text', {}, () => page.locator('#text').evaluate(e => e.remove()));
    assert.equal(removed.success, false);
    assert.equal(removed.stale, true, JSON.stringify(removed));
    assert.equal((await message({ type: 'AGENT_CONTROL', command: 'end', sessionId: sid })).result.safe, true);
    results.push('真实输入框/textarea/contenteditable 清空与空值无操作、禁用选项、目标移除边界');

    await panel.screenshot({ path: path.join(profile, 'panel.png'), fullPage: true });
    console.log(JSON.stringify({ passed: results.length, results, profile }, null, 2));
  } catch (error) {
    if (panel) {
      console.error('Last panel header:', await panel.locator('.agent-header').last().innerText().catch(() => 'unavailable'));
      console.error('Last panel errors:', await panel.locator('.agent-error').allTextContents().catch(() => []));
      await panel.screenshot({ path: path.join(profile, 'failure.png'), fullPage: true }).catch(() => {});
    }
    throw error;
  } finally {
    if (context) await context.close();
    if (backend.exitCode == null) backend.kill();
    if (server.listening) await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
