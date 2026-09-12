const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const extension = path.resolve(__dirname, '../../../extension');
const source = fs.readFileSync(path.join(extension, 'sidepanel.js'), 'utf8');
const start = source.indexOf('(function initKB() {');
assert.ok(start >= 0, 'Knowledge-base module must exist');
const moduleSource = source.slice(start);
const kb = { kb_id: 'kb_refresh_test', name: '刷新测试库', description: '' };

async function mount() {
  const elements = new Map();
  const calls = [];
  const errors = [];
  let respond = async () => ({ ok: true, json: async () => [] });
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      style: {}, innerHTML: '', textContent: '', disabled: false, handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
      click() {
        if (!this.disabled) return this.handlers.click({ currentTarget: this });
      },
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    console: { error: (...args) => errors.push(args) },
    sessionStorage: { getItem: () => '' }, window: {},
    document: {
      getElementById: element,
      createElement: () => ({
        set textContent(value) {
          this.innerHTML = String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        },
      }),
    },
    fetch: async (url, options) => {
      assert.equal(options, undefined, 'Refresh must only issue GET requests');
      calls.push(url);
      return respond(url);
    },
  });
  vm.runInContext(moduleSource, context);
  await new Promise(resolve => setImmediate(resolve));
  return { element, calls, errors, respond(handler) { respond = handler; } };
}

test('refresh button is present in the knowledge-base toolbar', () => {
  const html = fs.readFileSync(path.join(extension, 'sidepanel.html'), 'utf8');
  assert.match(html, /<button id="kbRefreshBtn"[^>]*type="button"[^>]*>↻ 刷新<\/button>/);
});

test('refresh shows a knowledge base created after panel initialization', async () => {
  const ui = await mount();
  assert.equal(ui.element('kbList').innerHTML, '');
  ui.respond(async () => ({ ok: true, json: async () => [kb] }));
  await ui.element('kbRefreshBtn').click();
  assert.match(ui.element('kbList').innerHTML, /kb_refresh_test/);
  assert.equal(ui.calls.length, 2);
  assert.equal(ui.calls.at(-1), 'http://localhost:8000/v1/kb');
  assert.equal(ui.element('kbRefreshBtn').textContent, '↻ 刷新');
});

test('refresh uses the trash endpoint when viewing the recycle bin', async () => {
  const ui = await mount();
  ui.element('kbTrashBtn').click();
  await new Promise(resolve => setImmediate(resolve));
  ui.respond(async () => ({ ok: true, json: async () => [{ ...kb, doc_count: 4, deleted_at: '2026-09-12' }] }));
  await ui.element('kbRefreshBtn').click();
  assert.equal(ui.calls.at(-1), 'http://localhost:8000/v1/kb/trash');
  assert.match(ui.element('kbTrashList').innerHTML, /kb_refresh_test/);
});

test('refresh is disabled while loading and ignores a second click', async () => {
  const ui = await mount();
  let release;
  ui.respond(() => new Promise(resolve => { release = resolve; }));
  const btn = ui.element('kbRefreshBtn');
  const pending = btn.click();
  assert.equal(btn.disabled, true);
  assert.equal(btn.textContent, '刷新中…');
  btn.click();
  assert.equal(ui.calls.length, 2);
  release({ ok: true, json: async () => [kb] });
  await pending;
  assert.equal(btn.disabled, false);
});

for (const view of ['active', 'trash']) {
  for (const failure of ['network', 'http']) {
    test(`${view}: ${failure} failure preserves the list and permits retry`, async () => {
      const ui = await mount();
      const row = { ...kb, doc_count: 4, deleted_at: '2026-09-12' };
      ui.respond(async () => ({ ok: true, json: async () => [row] }));
      if (view === 'trash') {
        ui.element('kbTrashBtn').click();
        await new Promise(resolve => setImmediate(resolve));
      }
      const btn = ui.element('kbRefreshBtn');
      await btn.click();
      const list = ui.element(view === 'trash' ? 'kbTrashList' : 'kbList');
      const before = list.innerHTML;
      ui.respond(async () => {
        if (failure === 'network') throw new Error('Synthetic connection failure');
        return { ok: false, status: 503, json: async () => [] };
      });
      await btn.click();
      assert.equal(list.innerHTML, before);
      assert.equal(btn.disabled, false);
      assert.equal(btn.textContent, '刷新失败，重试');
      assert.equal(ui.errors.length, 1);
      ui.respond(async () => ({ ok: true, json: async () => [] }));
      await btn.click();
      assert.equal(list.innerHTML, '');
      assert.equal(btn.textContent, '↻ 刷新');
    });
  }
}
