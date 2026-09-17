const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.resolve(__dirname, '../../../extension/sidepanel.js'), 'utf8');
const start = source.indexOf('(function initKB() {');
assert.ok(start >= 0);
const tick = () => new Promise(resolve => setImmediate(resolve));
const kb = { kb_id: 'kb_a', name: '测试库', description: '', doc_count: 1, deleted_at: '2026-09-16' };
const doc = { doc_id: 'doc_a', filename: 'synthetic.txt', status: 'indexed', file_bytes: 42 };
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

async function mount() {
  const elements = new Map(), calls = [], timers = [], alerts = [], errors = [];
  let handler = async url => response(url.endsWith('/docs') ? [doc] : [kb]);
  const prompts = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      style: {}, innerHTML: '', textContent: '', handlers: {}, disabled: false,
      addEventListener(type, callback) { this.handlers[type] = callback; },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => ({
      set textContent(value) {
        this.innerHTML = String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      },
    }) },
    sessionStorage: { getItem: () => 'kb_a', setItem() {}, removeItem() {} },
    window: { dispatchEvent() {} }, CustomEvent: class {}, FormData: class { append() {} },
    confirm: () => true, prompt: () => prompts.shift(), alert: message => alerts.push(message),
    console: { error: (...args) => errors.push(args) },
    setTimeout: (fn, ms) => timers.push({ fn, ms }),
    fetch: async (url, options) => { calls.push({ url, options }); return handler(url, options); },
  });
  vm.runInContext(source.slice(start), context);
  await tick();
  return {
    element, calls, timers, alerts, errors, prompts, context,
    respond(fn) { handler = fn; },
    async click(id, selector, dataset = {}) {
      const target = { closest: value => value === selector ? { dataset } : null };
      await element(id).handlers.click({ target, currentTarget: element(id), stopPropagation() {} });
      await tick();
    },
    async upload() {
      await element('fileInput').handlers.change({ target: { files: [{ name: 't.txt', size: 42 }], value: 't.txt' } });
      await tick();
    },
  };
}

test('create actually sends a request and checks the HTTP result', async () => {
  const ui = await mount();
  ui.prompts.push('新库', '合成测试');
  ui.respond(async () => response({ kb_id: 'new' }));
  await ui.click('createKbBtn');
  assert.equal(ui.calls.at(-1).options.method, 'POST');
  assert.equal(ui.element('createKbBtn').textContent, '✓ 已创建');
});

for (const result of [response({ detail: '暂时不可用' }, 503), response({ ok: false }), response({ ok: true, sync_pending: true })]) {
  test(`restore does not display success for HTTP ${result.status} / pending failure`, async () => {
    const ui = await mount();
    await ui.click('kbTrashBtn');
    ui.respond(async () => result);
    await ui.click('kbTrashList', '.kb-card-restore', { kbId: 'kb_a' });
    assert.equal(ui.calls.at(-1).options.method, 'POST');
    assert.match(ui.element('kbTrashList').innerHTML, /还原未完成，重试/);
    assert.doesNotMatch(ui.element('kbTrashList').innerHTML, /✓ 已还原/);
    assert.equal(ui.timers.at(-1).ms, 1500);
  });
}

test('restore displays success only after completed response', async () => {
  const ui = await mount();
  await ui.click('kbTrashBtn');
  ui.respond(async () => response({ ok: true, sync_pending: false }));
  await ui.click('kbTrashList', '.kb-card-restore', { kbId: 'kb_a' });
  assert.match(ui.element('kbTrashList').innerHTML, /✓ 已还原/);
});

for (const pending of [false, true]) {
  test(`delete displays synchronization state: ${pending}`, async () => {
    const ui = await mount();
    ui.respond(async () => response({ ok: true, sync_pending: pending }));
    await ui.click('kbList', '.kb-card-delete', { kbId: 'kb_a' });
    assert.match(ui.element('kbList').innerHTML, pending ? /已删除，待同步/ : /✓ 已删除/);
  });
}

test('failed delete keeps selected KB and does not claim success', async () => {
  const ui = await mount();
  ui.respond(async () => response({ detail: '失败' }, 503));
  await ui.click('kbList', '.kb-card-delete', { kbId: 'kb_a' });
  assert.equal(ui.context.window.getCurrentKbId(), 'kb_a');
  assert.match(ui.element('kbList').innerHTML, /删除失败/);
});

test('failed permanent delete is not presented as successful', async () => {
  const ui = await mount();
  await ui.click('kbTrashBtn');
  ui.prompts.push(kb.name);
  ui.respond(async () => response({ detail: '待重试' }, 503));
  await ui.click('kbTrashList', '.kb-card-hard-delete', { kbId: kb.kb_id, kbName: kb.name });
  assert.equal(ui.calls.at(-1).options.method, 'DELETE');
  assert.match(ui.element('kbTrashList').innerHTML, /删除失败/);
  assert.doesNotMatch(ui.element('kbTrashList').innerHTML, /✓ 已删除/);
});

test('upload conflict is shown, and successful upload starts status polling', async () => {
  const ui = await mount();
  ui.respond(async () => response({ detail: '内容重复' }, 409));
  await ui.upload();
  assert.deepEqual(ui.alerts, ['上传失败: 内容重复']);
  assert.equal(ui.timers.length, 0);
  ui.respond(async (url, options) => response(options?.method === 'POST' ? { doc_id: 'doc_new' } : [doc]));
  await ui.upload();
  assert.match(ui.element('docList').innerHTML, /synthetic.txt/);
  assert.equal(ui.timers.at(-1).ms, 2000);
});

test('pending synchronization and errors are visible and escaped', async () => {
  const ui = await mount();
  ui.respond(async () => response([{ ...doc, sync_pending: true, sync_error: '<img onerror=alert(1)>' }]));
  await ui.click('kbList', '.kb-card', { kbId: 'kb_a' });
  assert.match(ui.element('docList').innerHTML, /待同步/);
  assert.match(ui.element('docList').innerHTML, /&lt;img/);
  assert.doesNotMatch(ui.element('docList').innerHTML, /<img/);
  ui.respond(async () => response([{ ...kb, sync_action: 'restore', sync_error: '<bad>' }]));
  await ui.click('kbTrashBtn');
  assert.match(ui.element('kbTrashList').innerHTML, /待同步：restore/);
  assert.match(ui.element('kbTrashList').innerHTML, /&lt;bad&gt;/);
});

test('upload polling stays with its original KB after selection changes', async () => {
  const ui = await mount();
  let release;
  ui.respond(async (url, options) => {
    if (options?.method === 'POST') return new Promise(resolve => { release = resolve; });
    return response([{ ...doc, filename: url.includes('kb_b') ? 'B.txt' : 'A.txt' }]);
  });
  const upload = ui.upload();
  await tick();
  await ui.click('kbList', '.kb-card', { kbId: 'kb_b' });
  assert.match(ui.element('docList').innerHTML, /B.txt/);
  release(response({ doc_id: 'doc_new' }));
  await upload;
  assert.match(ui.element('docList').innerHTML, /B.txt/);
  ui.respond(async () => response({ status: 'not_found' }));
  ui.timers.find(timer => timer.ms === 2000).fn();
  await tick();
  assert.ok(ui.calls.some(call => call.url.endsWith('/kb_a/docs/doc_new/status')));
  assert.ok(!ui.calls.some(call => call.url.endsWith('/kb_b/docs/doc_new/status')));
});

test('late document-list response cannot overwrite newly selected KB', async () => {
  const ui = await mount();
  let release;
  ui.respond(async url => url.includes('kb_a') ? new Promise(resolve => { release = resolve; })
    : response([{ ...doc, filename: 'B.txt' }]));
  const first = ui.click('kbList', '.kb-card', { kbId: 'kb_a' });
  await tick();
  await ui.click('kbList', '.kb-card', { kbId: 'kb_b' });
  release(response([{ ...doc, filename: 'A.txt' }]));
  await first;
  assert.match(ui.element('docList').innerHTML, /B.txt/);
  assert.doesNotMatch(ui.element('docList').innerHTML, /A.txt/);
});

test('failed indexing with pending cleanup is displayed during polling', async () => {
  const ui = await mount();
  ui.respond(async (url, options) => response(options?.method === 'POST' ? { doc_id: doc.doc_id } : [{ ...doc, status: 'pending', sync_pending: true }]));
  await ui.upload();
  assert.match(ui.element('docList').innerHTML, /索引中/);
  ui.respond(async () => response({ status: 'failed', sync_pending: true, error_msg: '合成索引错误' }));
  ui.timers.find(timer => timer.ms === 2000).fn();
  await tick();
  assert.match(ui.element('docList').innerHTML, /索引失败，待清理/);
  assert.match(ui.element('docList').innerHTML, /合成索引错误/);
  assert.equal(ui.timers.filter(timer => timer.ms === 2000).length, 2);
});
