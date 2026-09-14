const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { layoutMarks, annotate } = require('../../../extension/agent_observation.js');
const source = fs.readFileSync(path.resolve(__dirname, '../../../extension/background.js'), 'utf8');
function load() {
  const c = vm.createContext({ console, URL, setTimeout, clearTimeout,
    chrome: { runtime: { onConnect: { addListener() {} } }, alarms: { onAlarm: { addListener() {} } } } });
  vm.runInContext(source.slice(source.indexOf('const CDP_TIMEOUTS =')), c);
  return c;
}
function node(tag, attributes = {}, parent = null) {
  return { nodeType: 1, nodeName: tag, attributes, parent, children: [], backendNodeId: 1,
    viewportRect: { x: 20, y: 20, width: 23, height: 23 },
    snapshot: { bounds: { width: 23, height: 23 }, computedStyles: { cursor: 'auto' } } };
}
for (const attributes of [{ class: 'pics J_numpic' }, { class: 'search-icon' }, { id: 'search' },
  { 'data-action': 'search' }, { 'aria-label': '收藏' }, { title: '签到' }]) {
  test(`description alone is not interactive: ${JSON.stringify(attributes)}`, () => {
    assert.equal(load().isInteractive(node('B', attributes)), false);
  });
}
test('native empty links and actual role/listener/keyboard controls survive', () => {
  const c = load();
  for (const el of [node('A', { href: '/action' }), node('SPAN', { role: 'button' }),
    node('svg', { onclick: '' }), node('DIV', { tabindex: '0' })]) assert.equal(c.isInteractive(el), true);
});
test('pointer fallback does not independently number inherited decoration', () => {
  const c = load(), parent = node('DIV'), child = node('SPAN', { class: 'icon', 'aria-label': '图标' }, parent);
  parent.snapshot.computedStyles.cursor = child.snapshot.computedStyles.cursor = 'pointer';
  assert.equal(c.interactionSource(parent), 'cursor');
  assert.equal(c.isInteractive(child), false);
  child.snapshot.isClickable = true;
  assert.equal(c.isInteractive(child), true);
});
test('URL hints discard credentials, fragments, unknown fields and executable URLs', () => {
  const c = load();
  const hint = c.safeLinkHint('https://account:secret@example.test/plugin.php?id=k_misign:sign&operation=qiandao&formhash=PRIVATE&token=PRIVATE&redirect=PRIVATE#PRIVATE');
  assert.equal(hint, 'plugin.php; operation=qiandao; id=k_misign:sign');
  for (const href of ['javascript:alert(1)', 'data:text/plain,PRIVATE', '/PRIVATE123?password=PRIVATE', '/?action=%3Cscript%3E'])
    assert.equal(c.safeLinkHint(href), '');
});
test('layoutless listener retains its first rendered child, not every decorative descendant', () => {
  const c = load(), parent = node('SPAN', { onclick: '' });
  parent.viewportRect = null;
  const child = node('svg', {}, parent), shape = node('use', {}, child);
  assert.equal(c.interactionSource(child), 'layoutless-parent');
  assert.equal(c.isInteractive(shape), false);
});
test('empty link retains name absence separately from action hint', () => {
  const c = load(), el = node('A', { href: '/plugin.php?operation=qiandao&formhash=PRIVATE' });
  el.isInteractive = el.isVisible = true;
  const result = c.serializeInteractive([el], {}).elements[0];
  assert.equal(result.text, ''); assert.equal(result.label_source, 'none');
  assert.equal(result.target_hint, 'plugin.php; operation=qiandao');
  assert.equal(JSON.stringify(result).includes('PRIVATE'), false);
});
test('mark layout keeps identity, clips viewport and avoids label collisions', () => {
  const elements = Array.from({ length: 12 }, (_, i) => ({ id: 25000 + i, text: '',
    bounding_box: { x: (i % 4) * 55 - 3, y: Math.floor(i / 4) * 45, width: 30, height: 20 } }));
  elements.push({ id: 99, text: '', bounding_box: { x: 600, y: 0, width: 25, height: 25 } });
  const marks = layoutMarks(elements, { width: 260, height: 200 }, s => s.length * 8);
  assert.equal(marks.length, 12);
  assert.ok(!marks.some(m => m.id === 99));
  for (const m of marks) {
    assert.ok(m.label.x >= 0 && m.label.y >= 0 && m.label.x + m.label.width <= 260 && m.label.y + m.label.height <= 200);
    for (const other of marks.filter(o => o !== m)) {
      const a = m.label, b = other.label;
      assert.ok(!(a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y));
    }
  }
});
test('unique named controls need no mark, duplicate names do', () => {
  const box = { x: 30, y: 30, width: 100, height: 30 };
  assert.equal(layoutMarks([{ id: 1, text: '唯一', bounding_box: box }], { width: 500, height: 400 }, () => 10).length, 0);
  assert.equal(layoutMarks([1, 2].map(id => ({ id, text: '同名', bounding_box: { ...box, y: id * 100 } })),
    { width: 500, height: 400 }, () => 10).length, 2);
});
test('render failure retains original screenshot without fake mark metadata', async () => {
  const state = { screenshot: 'invalid', viewport: { width: 500, height: 400 }, screenshot_marked: true };
  await annotate(state);
  assert.equal(state.screenshot, 'invalid'); assert.equal(state.screenshot_marked, false);
  assert.deepEqual(state.screenshot_mark_ids, []);
});
test('observation retries navigation once and does not publish mismatched indices', async () => {
  const c = load(); let probes = 0, captures = 0; const saved = [];
  c._oopif = new Map();
  c.debuggerEnsureAttached = async () => {};
  c.getSessionEpoch = async () => 1;
  c.observationPosition = async () => ['before', 'after', 'stable', 'stable'][probes++];
  c.gatherAndConstructTarget = async () => ({ built: { allNodes: [], pendingCrossOrigin: [] }, dpr: 1, jsClickCount: 0 });
  c.pageExtrasProbe = async () => ({ viewport: { width: 500, height: 400 } });
  c.saveTabState = async (key, id, value) => saved.push(value);
  c.cdpSend = async () => { captures++; return { data: 'image' }; };
  const state = (await c.handleAgentObserve(5, true)).pageState;
  assert.equal(captures, 2); assert.equal(saved[0], null);
  assert.equal(state.screenshot, 'data:image/jpeg;base64,image');
  c.observationPosition = async () => String(probes++);
  await assert.rejects(c.handleAgentObserve(5, true), /重新观察/);
  assert.equal(saved.at(-1), null);
});
