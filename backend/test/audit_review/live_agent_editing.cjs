// 独立 Chromium + 实际扩展/CDP + 固定版本的真实 CodeMirror；不访问用户页面或模型。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.argv[2] || 'playwright');
require('node:dns').setDefaultResultOrder('ipv4first');
const root = path.resolve(__dirname, '../../..');
const version = '5.65.16';
const fixture = `<!doctype html><meta charset="utf-8"><title>Editing fixture</title>
<link rel="stylesheet" href="/codemirror.css">
<style>.CodeMirror{height:90px;border:1px solid #999} [contenteditable]{min-height:25px;border:1px solid #999} body{margin:8px}</style>
<form id="form"><input id="text" value="old" aria-label="文本"><textarea id="area">old</textarea>
<input id="readonly" value="keep" readonly><input id="date" type="date"><input id="number" type="number">
<div id="ce" contenteditable="true" role="textbox">old</div><div id="plain" contenteditable="plaintext-only">old</div>
<div id="nested-ce" contenteditable="true"><span tabindex="0" id="nested-child">old</span></div>
<div id="cm-main"></div><div id="cm-other"></div><div id="cm-ce"></div><div id="cm-ro"></div>
<div class="CodeMirror" id="fake" tabindex="0"><textarea hidden id="hidden">keep</textarea>Not an editor</div>
<input id="victim"><input id="steal"><input id="replace"><button id="submit">发送</button></form>
<iframe id="same" src="/frame" style="height:70px"></iframe><iframe id="cross" style="height:70px"></iframe>
<script src="/codemirror.js"></script><script>
window.submits=0;window.enters=0;window.changes=[];window.cm={};
document.querySelector('#form').onsubmit=e=>{e.preventDefault();submits++;};
document.addEventListener('keydown',e=>{if(e.key==='Enter')enters++;});
for(const [id,options] of [['cm-main',{}],['cm-other',{}],['cm-ce',{inputStyle:'contenteditable'}],['cm-ro',{readOnly:true}]]){
 const host=document.getElementById(id); const editor=CodeMirror(host,{value:'old',...options});
 host.removeAttribute('id');editor.getWrapperElement().id=id;cm[id]=editor;
 editor.on('change',()=>changes.push({id,value:editor.getValue()}));
}
document.querySelector('#cross').src=location.origin.replace('127.0.0.1','localhost')+'/frame-cross';
</script>`;
const frame = '<!doctype html><meta charset="utf-8"><input aria-label="帧内输入" id="frame-input">';
const pause = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const assets = {};
  for (const file of ['codemirror.js', 'codemirror.css']) {
    if (process.argv[4]) assets['/' + file] = fs.readFileSync(path.join(process.argv[4], file), 'utf8');
    else {
      const res = await fetch(`https://cdn.jsdelivr.net/npm/codemirror@${version}/lib/${file}`, { signal: AbortSignal.timeout(30000) });
      assert.equal(res.status, 200, `CodeMirror fixture download: ${file}`);
      assets['/' + file] = await res.text();
    }
  }
  const server = http.createServer((req, res) => {
    res.writeHead(200, { 'Content-Type': req.url.endsWith('.js') ? 'text/javascript' : req.url.endsWith('.css') ? 'text/css' : 'text/html; charset=utf-8' });
    res.end(assets[req.url] || (req.url.startsWith('/frame') ? frame : fixture));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.mkdirSync(path.join(root, 'output/playwright'), { recursive: true });
  const profile = fs.mkdtempSync(path.join(root, 'output/playwright/agent-editing-'));
  const results = [];
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: 'chromium', executablePath: process.argv[3], headless: true, viewport: { width: 1100, height: 1100 },
      args: [`--disable-extensions-except=${path.join(root, 'extension')}`, `--load-extension=${path.join(root, 'extension')}`],
    });
    await context.route('**/*', route => /^(http:\/\/(127\.0\.0\.1|localhost):|chrome-extension:)/.test(route.request().url()) ? route.continue() : route.abort());
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/fixture`);
    assert.equal(await page.evaluate(() => CodeMirror.version), version);
    await page.frameLocator('#cross').locator('#frame-input').waitFor();
    const tabId = await worker.evaluate(async () => (await chrome.tabs.query({})).find(t => t.url?.endsWith('/fixture')).id);
    let sid = 'editing', seq = 0;
    const start = () => worker.evaluate(({ tabId, sid }) => agentExecution.start(tabId, sid), { tabId, sid });
    const end = () => worker.evaluate(({ tabId, sid }) => agentExecution.end(tabId, sid, () => debuggerDetach(tabId)), { tabId, sid });
    const observe = () => worker.evaluate(async args => (await managedAgentObserve({ ...args, includeScreenshot: false })).pageState,
      { tabId, sessionId: sid, observationId: `obs-${++seq}` });
    const execute = action => worker.evaluate(({ tabId, sid, action }) => agentExecution.execute(tabId, sid, action,
      ctx => handleAgentExecute(tabId, action, ctx)), { tabId, sid, action });
    const act = async (type, htmlId, params = {}, pick = e => e) => {
      const state = await observe();
      const element = htmlId == null ? null : state.interactive_elements.find(e => e.html_id === htmlId && pick(e));
      if (htmlId != null) assert.ok(element, `observable ${htmlId}: ${JSON.stringify(state.interactive_elements.map(e => [e.html_id,e.editor_type]))}`);
      return execute({ type, params, ...(element ? { index: element.id } : {}), observation_id: state.observation_id, action_id: `action-${seq}` });
    };
    const ok = result => assert.equal(result.success, true, JSON.stringify(result));
    const cmValue = id => page.evaluate(id => cm[id].getValue(), id);
    await start();

    let state = await observe();
    for (const id of ['cm-main', 'cm-other', 'cm-ce', 'cm-ro']) {
      const el = state.interactive_elements.find(e => e.html_id === id);
      assert.equal(el?.editor_type, 'codemirror5', id);
      assert.equal(el.editable, id !== 'cm-ro');
    }
    assert.equal(state.interactive_elements.some(e => e.html_id === 'hidden'), false);
    assert.equal(state.interactive_elements.find(e => e.html_id === 'fake')?.editable, false);
    results.push('观察识别真实 CM5/只读/多编辑器，排除隐藏代理和伪装外壳');

    for (const [id, text] of [['text', '中文🙂A'], ['area', '中文🙂\n第二行\n'], ['ce', '中文🙂\n第二行'], ['plain', '中文🙂\n第二行']]) {
      ok(await act('type', id, { text }));
      const actual = await page.locator('#' + id).evaluate(e => ['INPUT','TEXTAREA'].includes(e.tagName) ? e.value : e.innerText);
      assert.equal(actual, text, id);
      ok(await act('clear', id));
      const empty = await page.locator('#' + id).evaluate(e => ['INPUT','TEXTAREA'].includes(e.tagName) ? e.value : e.textContent);
      assert.equal(empty, '', id);
    }
    results.push('原生 input/textarea、普通及 plaintext-only 富文本：中文/emoji/多行/清空');

    for (const id of ['ce', 'plain']) {
      for (const text of ['\n', 'line\n', '\n\n', 'one\n\ntwo', '  a  b  ', 'e\u0301🙂\tend']) {
        const r = await act('type', id, { text });
        assert.equal(r.success, true, JSON.stringify({ id, text, r,
          dom: await page.locator('#' + id).evaluate(e => ({ html: e.innerHTML, text: e.innerText,
            nodes: Array.from(e.childNodes).map(n => [n.nodeName, n.nodeValue]) })) }));
      }
      ok(await act('clear', id));
    }
    results.push('普通富文本的空行/末尾换行/连续空格/组合字符/Tab 完整回读，不派发 Enter');
    ok(await act('type', 'nested-child', { text: 'inside' }));
    assert.equal(await page.locator('#nested-ce').innerText(), 'inside');
    await page.locator('#nested-ce').evaluate(e => {
      const range = document.createRange(); range.setStart(e.firstChild, 2); range.setEnd(e.firstChild, 4);
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    });
    ok(await act('type', 'nested-ce', { text: 'ZZ', clear: false }));
    assert.equal(await page.locator('#nested-ce').innerText(), 'inZZde');
    await page.locator('#nested-ce').evaluate(e => e.innerHTML = '<div>one</div><div>two</div>');
    assert.equal((await act('type', 'nested-ce', { text: 'wrong', clear: false })).success, false);
    assert.equal(await page.locator('#nested-ce').innerText(), 'one\ntwo');
    results.push('从可编辑子节点定位到整体区域；普通选区插入生效，无法映射的复杂选区在写入前拒绝');

    for (const id of ['cm-main', 'cm-ce']) {
      const text = '中文🙂\n第二行\n';
      const typed = await act('type', id, { text });
      assert.equal(typed.success, true, JSON.stringify({ id, typed, value: await cmValue(id) }));
      assert.equal(await cmValue(id), text);
      await page.evaluate(id => cm[id].setSelection({ line: 1, ch: 0 }, { line: 1, ch: 3 }), id);
      ok(await act('type', id, { text: 'new', clear: false }));
      assert.equal(await cmValue(id), '中文🙂\nnew\n');
      ok(await act('clear', id));
      assert.equal(await cmValue(id), '');
    }
    assert.equal(await cmValue('cm-other'), 'old');
    assert.equal(await page.evaluate(() => changes.some(e => e.value.includes('中文'))), true);
    results.push('真实 CM5 textarea/contenteditable 两种模式：全文替换、选区插入、清空、change 事件、互不串写');

    ok(await act('focus', 'cm-main'));
    state = await observe();
    const focused = state.interactive_elements.find(e => e.html_id === 'cm-main');
    assert.equal(focused.focused, true);
    assert.ok(state.focused_element.includes(`[${focused.id}]`));
    assert.equal(await page.evaluate(() => cm['cm-main'].getInputField().id), '');
    ok(await act('press_key', 'cm-main', { key: 'a' }));
    assert.equal(await cmValue('cm-main'), 'a');
    ok(await act('press_key', 'cm-main', { key: 'a', modifiers: ['Control'] }));
    ok(await act('type', 'cm-main', { text: 'XYZ', clear: false }));
    assert.equal(await cmValue('cm-main'), 'XYZ');
    ok(await act('press_key', null, { key: '1', modifiers: ['Shift'] }));
    assert.equal(await cmValue('cm-main'), 'XYZ!');
    results.push('无 HTML id 的 CM5 代理焦点可见；字母、Ctrl+A、Shift+1 和无 index 按键真实生效');

    ok(await act('type', 'text', { text: 'abc' }));
    ok(await act('press_key', 'text', { key: 'a', modifiers: ['Control'] }));
    assert.deepEqual(await page.locator('#text').evaluate(e => [e.selectionStart,e.selectionEnd]), [0,3]);
    ok(await act('type', 'text', { text: 'Z', clear: false }));
    assert.equal(await page.locator('#text').inputValue(), 'Z');
    ok(await act('type', 'date', { text: '2026-09-17' }));
    ok(await act('type', 'number', { text: '42' }));
    results.push('原生选区插入、Ctrl+A、日期和数字输入回归');

    for (const id of ['readonly', 'cm-ro', 'fake']) {
      const result = await act('type', id, { text: 'wrong' });
      assert.equal(result.success, false, id);
    }
    assert.equal(await page.locator('#readonly').inputValue(), 'keep');
    assert.equal(await page.locator('#hidden').inputValue(), 'keep');
    assert.equal(await cmValue('cm-ro'), 'old');
    assert.equal((await act('focus', 'fake')).success, true); // 可聚焦不等于可输入。
    assert.equal((await act('press_key', 'fake', { key: 'a' })).success, false);
    const singleLine = await act('type', 'text', { text: 'bad\nnewline' });
    assert.equal(singleLine.success, false);
    assert.equal(await page.locator('#text').inputValue(), 'Z');
    results.push('只读/伪装编辑器不写入，普通可聚焦 div 不等于编辑器，单行多行冲突不先清空');

    await page.locator('#victim').evaluate(e => e.addEventListener('input', () => document.querySelector('#steal').focus(), { once: true }));
    let result = await act('type', 'victim', { text: 'ABCD' });
    assert.equal(result.success, false);
    assert.equal(result.execution_state, 'partial');
    assert.equal(await page.locator('#victim').inputValue(), 'A');
    assert.equal(await page.locator('#steal').inputValue(), '');
    for (const type of ['type', 'press_key']) {
      await page.locator('#victim').evaluate(e => {
        e.value = '';
        e.addEventListener('keydown', () => document.querySelector('#steal').focus(), { once: true });
      });
      result = await act(type, 'victim', type === 'type' ? { text: 'AB' } : { key: 'a' });
      assert.equal(result.success, false);
      assert.equal(await page.locator('#victim').inputValue(), '');
      assert.equal(await page.locator('#steal').inputValue(), '');
    }
    await page.locator('#replace').evaluate(e => e.addEventListener('input', () => e.replaceWith(e.cloneNode()), { once: true }));
    result = await act('type', 'replace', { text: 'ABCD' });
    assert.equal(result.success, false);
    assert.equal(result.stale, true);
    assert.equal(await page.locator('#replace').inputValue(), 'A');
    results.push('输入中途抢焦点/替换节点：只保留已写字符，不往其他框继续写，不重放');

    await page.evaluate(() => cm['cm-main'].setSelections([{anchor:{line:0,ch:0},head:{line:0,ch:0}}, {anchor:{line:0,ch:2},head:{line:0,ch:2}}]));
    const before = await cmValue('cm-main');
    assert.equal((await act('type', 'cm-main', { text: 'no', clear: false })).success, false);
    assert.equal(await cmValue('cm-main'), before);
    await page.evaluate(() => cm['cm-main'].setOption('readOnly', 'nocursor'));
    assert.equal((await act('focus', 'cm-main')).success, false);
    await page.evaluate(() => cm['cm-main'].setOption('readOnly', false));
    results.push('多光标插入与 nocursor 明确拒绝，不污染编辑器');

    await page.evaluate(() => {
      cm['cm-other'].setValue('');
      window.rejectCount = 0;
      cm['cm-other'].on('beforeChange', (cm, change) => { rejectCount++; change.cancel(); });
    });
    result = await act('type', 'cm-other', { text: 'rejected' });
    assert.equal(result.success, false);
    assert.match(result.error, /回读/);
    assert.equal(await page.evaluate(() => rejectCount), 1);
    assert.equal(await cmValue('cm-other'), '');
    results.push('编辑器 beforeChange 拒绝写入时回读失败，仅尝试一次，不补写');

    state = await observe();
    const frameInputs = state.interactive_elements.filter(e => e.html_id === 'frame-input');
    assert.equal(frameInputs.length, 2, 'same-origin and OOPIF both observable');
    for (let i = 0; i < frameInputs.length; i++) {
      ok(await act('type', 'frame-input', { text: `frame-${i}` }, e => e.frame_id === frameInputs[i].frame_id));
    }
    const values = await Promise.all(['#same','#cross'].map(id => page.frameLocator(id).locator('#frame-input').inputValue()));
    assert.deepEqual(values.sort(), ['frame-0','frame-1']);
    state = await observe();
    assert.equal(state.interactive_elements.filter(e => e.focused).length, 1, 'inactive frame must not claim focus');
    assert.equal((await act('press_key', null, { key: 'a' })).success, false);
    results.push('同源 iframe/跨进程 iframe 各写入自身节点；无 index 不猜测跨帧焦点');

    assert.equal(await page.evaluate(() => submits), 0);
    assert.equal(await page.evaluate(() => enters), 0);
    results.push('所有文本输入/清空过程无 Enter、无表单提交');
    await end();

    for (const mode of ['unicode', 'editor-api']) {
      sid = `late-${mode}`; await start();
      await worker.evaluate(mode => {
        globalThis.originalSendCommand = chrome.debugger.sendCommand;
        globalThis.originalCdpSend = cdpSend;
        globalThis.heldInsert = false;
        chrome.debugger.sendCommand = (target, method, params, cb) => originalSendCommand(target, method, params, result => {
          const input = mode === 'unicode' ? method === 'Input.insertText' :
            method === 'Runtime.callFunctionOn' && params.arguments?.[0]?.value === 'insert_cm';
          if (input) { globalThis.heldInsert = true; globalThis.releaseInsert = () => cb(result); }
          else cb(result);
        });
        cdpSend = (target, method, params, timeout) => originalCdpSend(target, method, params,
          (mode === 'unicode' ? method === 'Input.insertText' :
            method === 'Runtime.callFunctionOn' && params.arguments?.[0]?.value === 'insert_cm') ? 10 : timeout);
      }, mode);
      const pending = act('type', mode === 'unicode' ? 'area' : 'cm-main', { text: '中🙂后续' });
      for (let i = 0; i < 300 && !await worker.evaluate(() => heldInsert); i++) await pause(10);
      assert.equal(await worker.evaluate(() => heldInsert), true);
      await worker.evaluate(({ tabId, sid }) => agentExecution.cancel(tabId, sid), { tabId, sid });
      await end();
      assert.equal(await worker.evaluate(tabId => agentExecution.tabs.get(tabId).ended, tabId), false);
      await worker.evaluate(() => { releaseInsert(); cdpSend = originalCdpSend; chrome.debugger.sendCommand = originalSendCommand; });
      result = await pending;
      assert.equal(result.success, false);
      if (mode === 'unicode') assert.equal(await page.locator('#area').inputValue(), '中');
      else assert.equal(await cmValue('cm-main'), '中🙂后续');
      for (let i = 0; i < 300 && !await worker.evaluate(tabId => agentExecution.tabs.get(tabId).ended, tabId); i++) await pause(10);
      assert.equal(await worker.evaluate(tabId => agentExecution.tabs.get(tabId).ended, tabId), true);
      sid = `after-late-${mode}`; await start(); await end();
      results.push(`${mode} 命令迟到时取消/结束不提前解锁；确认后自动收尾且可启动新任务`);
    }
    console.log(JSON.stringify({ version, passed: results.length, results, profile }, null, 2));
  } finally {
    if (context) await context.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
