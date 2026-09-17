// 独立 Chromium 配置和本地合成页；不加载用户配置、不访问业务网站或模型。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.argv[2] || 'playwright');
const root = path.resolve(__dirname, '../../..');
const fixture = `<!doctype html><meta charset="utf-8"><title>Automation control fixture</title>
<button id="submit">合成提交</button><input id="text" aria-label="合成输入"><div id="count">0</div>
<script>window.events=[];document.querySelector('#submit').onclick=()=>{
const c=document.querySelector('#count');c.textContent=Number(c.textContent)+1;};
for(const type of ['keydown','keyup','input'])document.querySelector('#text').addEventListener(type,e=>events.push({type,key:e.key}));</script>`;

(async () => {
  const server = http.createServer((req, res) => { res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(fixture); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.mkdirSync(path.join(root, 'output/playwright'), { recursive: true });
  const profile = fs.mkdtempSync(path.join(root, 'output/playwright/agent-control-'));
  let context;
  const results = [];
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: 'chromium', executablePath: process.argv[3], headless: true,
      args: [`--disable-extensions-except=${path.join(root, 'extension')}`, `--load-extension=${path.join(root, 'extension')}`],
    });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/fixture`);
    const tabId = await worker.evaluate(async () => (await chrome.tabs.query({})).find(t => t.url?.endsWith('/fixture')).id);
    const observe = (sid, oid) => worker.evaluate(async args =>
      (await managedAgentObserve({ tabId: args.tabId, sessionId: args.sid, observationId: args.oid, includeScreenshot: true })).pageState,
    { tabId, sid, oid });
    const start = sid => worker.evaluate(({ tabId, sid }) => agentExecution.start(tabId, sid), { tabId, sid });
    const end = sid => worker.evaluate(({ tabId, sid }) => agentExecution.end(tabId, sid, () => debuggerDetach(tabId)), { tabId, sid });
    const execute = (sid, action) => worker.evaluate(({ tabId, sid, action }) =>
      agentExecution.execute(tabId, sid, action, ctx => handleAgentExecute(tabId, action, ctx)), { tabId, sid, action });
    const makeAction = (state, type, id, htmlId, params = {}) => ({ type, action_id: id,
      observation_id: state.observation_id, index: state.interactive_elements.find(e => e.html_id === htmlId)?.id, params });

    await start('click');
    let state = await observe('click', 'obs-1');
    const click = makeAction(state, 'click', 'a-1', 'submit');
    assert.ok(click.index != null);
    const duplicate = await Promise.all([execute('click', click), execute('click', click)]);
    assert.equal(duplicate[0].execution_state, 'completed');
    assert.deepEqual(duplicate[0], duplicate[1]);
    assert.equal(await page.locator('#count').innerText(), '1');
    results.push('重复投递仅产生一次真实点击');

    state = await observe('click', 'obs-2');
    await page.reload();
    const stale = await execute('click', makeAction(state, 'click', 'a-2', 'submit'));
    assert.equal(stale.stale, true);
    assert.equal(stale.execution_state, 'not_dispatched');
    assert.equal(await page.locator('#count').innerText(), '0');
    results.push('同 URL 重载后拒绝旧文档动作');

    state = await observe('click', 'obs-3');
    const other = await context.newPage();
    await other.goto(`http://127.0.0.1:${server.address().port}/other`);
    await execute('click', makeAction(state, 'click', 'a-3', 'submit'));
    assert.equal(await page.locator('#count').innerText(), '1');
    assert.equal(await other.locator('#count').innerText(), '0');
    results.push('切换活动标签后仍只操作绑定标签');
    await end('click');

    await start('typing');
    state = await observe('typing', 'type-obs');
    await worker.evaluate(({ tabId }) => {
      globalThis.originalCdpSend = cdpSend;
      let chars = 0;
      cdpSend = async (...args) => {
        const result = await originalCdpSend(...args);
        if (args[1] === 'Input.dispatchKeyEvent' && args[2].type === 'char' && ++chars === 2) await agentExecution.cancel(tabId, 'typing');
        return result;
      };
    }, { tabId });
    const typed = await execute('typing', makeAction(state, 'type', 'type-a', 'text', { text: 'ABCD' }));
    await worker.evaluate(() => { cdpSend = originalCdpSend; });
    assert.equal(typed.execution_state, 'partial');
    assert.equal(await page.locator('#text').inputValue(), 'AB');
    const events = await page.evaluate(() => window.events);
    assert.equal(events.filter(e => e.type === 'keyup').length, 2);
    results.push('输入中途取消只保留已发字符，并实际释放按键');
    await end('typing');

    await start('timeout');
    state = await observe('timeout', 'timeout-obs');
    const timeoutAction = makeAction(state, 'click', 'timeout-a', 'submit');
    await worker.evaluate(() => {
      globalThis.originalSendCommand = chrome.debugger.sendCommand;
      chrome.debugger.sendCommand = (target, method, params, callback) => originalSendCommand(target, method, params, result => {
        if (method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed') setTimeout(() => callback(result), 400);
        else callback(result);
      });
      cdpSend = (target, method, params, timeout) => originalCdpSend(target, method, params,
        method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed' ? 5 : timeout);
    });
    const pending = execute('timeout', timeoutAction);
    await page.waitForFunction(() => document.querySelector('#count').textContent === '2');
    const busy = await worker.evaluate(({ tabId }) => {
      try { agentExecution.observeToken(tabId, 'timeout', 'unsafe'); return false; }
      catch (e) { return e.code === 'action_busy'; }
    }, { tabId });
    assert.equal(busy, true);
    const timed = await pending;
    assert.equal(timed.execution_state, 'partial');
    await worker.evaluate(() => { cdpSend = originalCdpSend; chrome.debugger.sendCommand = originalSendCommand; });
    assert.equal(await page.locator('#count').innerText(), '2');
    await observe('timeout', 'timeout-fresh');
    results.push('真实点击回调延迟时不提前放开占用，也不补点');
    await end('timeout');

    await start('observation');
    await worker.evaluate(() => {
      let held = false;
      globalThis.captureHeld = false;
      cdpSend = async (...args) => {
        const result = await originalCdpSend(...args);
        if (!held && args[1] === 'Page.captureScreenshot') {
          held = true; globalThis.captureHeld = true;
          await new Promise(resolve => { globalThis.releaseCapture = resolve; });
        }
        return result;
      };
    });
    const oldObservation = observe('observation', 'older').catch(e => ({ rejected: true }));
    for (let i = 0; i < 100 && !await worker.evaluate(() => globalThis.captureHeld); i++) await new Promise(r => setTimeout(r, 10));
    assert.equal(await worker.evaluate(() => globalThis.captureHeld), true);
    await observe('observation', 'newer');
    await worker.evaluate(() => { releaseCapture(); cdpSend = originalCdpSend; });
    assert.equal((await oldObservation).rejected, true);
    assert.equal(await worker.evaluate(async ({ tabId }) => (await loadTabState(STATE_KEYS.indexMap, tabId)).observationId, { tabId }), 'newer');
    results.push('迟到截图观察不能覆盖新编号表');
    await end('observation');
    console.log(JSON.stringify({ passed: results.length, results, profile }, null, 2));
  } finally {
    if (context) await context.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
