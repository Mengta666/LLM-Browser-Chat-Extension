const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.resolve(__dirname, '../../../extension/background.js'), 'utf8');
const start = source.indexOf('const CDP_TIMEOUTS =');
assert.ok(start > 0);

function load() {
  const context = vm.createContext({ console, setTimeout, clearTimeout,
    chrome: { runtime: { onConnect: { addListener() {} } }, alarms: { onAlarm: { addListener() {} } } } });
  vm.runInContext(source.slice(start), context);
  context.sleep = async () => {};
  return context;
}

function node(tag, attrs = {}, options = {}) {
  const box = { x: 20, y: 30, width: 24, height: 24 };
  return { nodeType: 1, nodeName: tag, backendNodeId: 1, attributes: attrs,
    viewportRect: { ...box }, absolutePosition: { ...box }, _viewport: { width: 500, height: 600 },
    snapshot: { bounds: { ...box }, clientRects: { x: 0, y: 0, width: 0, height: 0 },
      computedStyles: { display: 'inline', visibility: 'visible', opacity: '1' }, isClickable: false },
    children: [], parent: null, isVisible: true, isInteractive: false, ...options };
}

function append(parent, child) {
  parent.children.push(child);
  child.parent = parent;
  return child;
}

for (const zoom of [1, 1.25, 1.5, 2]) {
  test(`snapshot document scroll and bounds use the same CSS units at zoom ${zoom}`, () => {
    const c = load();
    const snapshot = { strings: ['frame'], documents: [{ frameId: 0,
      scrollOffsetX: 10 * zoom, scrollOffsetY: 150 * zoom,
      nodes: { backendNodeId: [8], isClickable: { index: [0] } },
      layout: { nodeIndex: [0], bounds: [[50 * zoom, 300 * zoom, 24 * zoom, 21 * zoom]],
        clientRects: [[0, 0, 0, 0]], styles: [[]] } }] };
    const lookup = c.buildSnapshotLookup(snapshot, zoom);
    const built = c.constructEnhancedTree({ nodeId: 1, backendNodeId: 8, nodeType: 1,
      nodeName: 'SPAN', attributes: ['title', '签到'] }, lookup,
    { viewport: { width: 500, height: 600 }, initialOffset: { x: 100, y: 200 } });
    assert.equal(built.root.isVisible, true);
    assert.equal(built.root.isInteractive, true);
    assert.deepEqual(JSON.parse(JSON.stringify(built.root.viewportRect)), { x: 40, y: 150, width: 24, height: 21 });
    assert.equal(built.root.absolutePosition.x, 140);
    assert.equal(built.root.absolutePosition.y, 350);
  });
}

test('zero client area does not hide an inline control; viewport position still matters', () => {
  const c = load(), control = node('SPAN', { title: '签到' });
  assert.equal(c.isVisibleCss(control), true);
  control.viewportRect.x = 550;
  assert.equal(c.isVisibleCss(control), false);
});

test('hidden, collapsed, zero-opacity ancestors and zero geometry remain excluded', () => {
  const c = load();
  for (const style of [{ display: 'none' }, { visibility: 'hidden' }, { visibility: 'collapse' }, { opacity: '0' }]) {
    const control = node('SPAN');
    Object.assign(control.snapshot.computedStyles, style);
    assert.equal(c.isVisibleCss(control), false);
  }
  const parent = node('DIV');
  parent.snapshot.computedStyles.opacity = '0';
  assert.equal(c.isVisibleCss(append(parent, node('SPAN'))), false);
  const zero = node('SPAN'); zero.viewportRect.width = 0;
  assert.equal(c.isVisibleCss(zero), false);
});

test('snapshot clickability works without getEventListeners and title alone is not a click signal', () => {
  const c = load(), control = node('SPAN', { title: '签到' });
  assert.equal(c.isInteractive(control), false);
  control.snapshot.isClickable = true;
  assert.equal(c.isInteractive(control), true);
  control.attributes['aria-disabled'] = 'true';
  assert.equal(c.isInteractive(control), false);
});

test('disabled and inert controls cannot be rescued by nested icon heuristics', () => {
  const c = load();
  for (const parent of [node('BUTTON', { disabled: '' }), node('DIV', { inert: '' }), node('DIV', { 'aria-disabled': 'true' })]) {
    assert.equal(c.isInteractive(append(parent, node('svg', { class: 'icon' }))), false);
  }
  const fieldset = node('FIELDSET', { disabled: '' });
  const legend = append(fieldset, node('LEGEND'));
  assert.equal(c.isDisabled(append(legend, node('INPUT'))), false);
  assert.equal(c.isDisabled(append(fieldset, node('INPUT'))), true);
});

test('tree-dependent classification runs after children and parents exist', () => {
  const c = load();
  const tree = { nodeId: 1, nodeType: 1, nodeName: 'SPAN', backendNodeId: 1,
    children: [{ nodeId: 2, nodeType: 1, nodeName: 'INPUT', backendNodeId: 2 }] };
  const lookup = new Map([1, 2].map(id => [id, { ...node('SPAN').snapshot, scrollX: 0, scrollY: 0 }]));
  const built = c.constructEnhancedTree(tree, lookup, { viewport: { width: 500, height: 600 } });
  assert.equal(built.root.isInteractive, true);
});

test('decorative SVG deduplicates under an inline clickable wrapper despite line-box overflow', () => {
  const c = load(), parent = node('SPAN', { title: '签到' }, { isInteractive: true });
  parent.snapshot.isClickable = true;
  parent.absolutePosition.height = 21;
  const svg = append(parent, node('svg', { class: 'icon' }, { backendNodeId: 2, isInteractive: true }));
  c.applyBoundingBoxFilter([parent, svg]);
  assert.equal(svg.excludedByParent, true);
  const output = c.serializeInteractive([parent, svg], {});
  assert.equal(output.elements.length, 1);
  assert.equal(output.elements[0].text, '签到');
  assert.equal(output.indexMap[1].backendNodeId, parent.backendNodeId);
});

test('SVG-only listener keeps its target while borrowing a single wrapper label', () => {
  const c = load(), parent = node('SPAN', { title: '签到' });
  const svg = append(parent, node('svg', { class: 'icon' }, { backendNodeId: 2, isInteractive: true }));
  svg.snapshot.isClickable = true;
  append(svg, node('use', { href: '#plan' }));
  assert.equal(c.extractText(svg), '签到');
  const output = c.serializeInteractive([parent, svg], {});
  assert.equal(output.indexMap[2].backendNodeId, 2);
  assert.equal(output.elements[0].title, '');
});

test('independent child actions and their own names are preserved', () => {
  const c = load(), parent = node('BUTTON', { title: 'parent' }, { isInteractive: true });
  const child = append(parent, node('svg', { 'aria-label': 'child' }, { isInteractive: true }));
  child.snapshot.isClickable = true;
  c.applyBoundingBoxFilter([parent, child]);
  assert.ok(!child.excludedByParent);
  assert.equal(c.extractText(child), 'child');
});

test('labels do not leak across independent menu branches or document boundaries', () => {
  const c = load(), menu = node('DIV', { title: 'menu' });
  const svg = append(append(menu, node('SPAN')), node('svg', { class: 'icon' }, { isInteractive: true }));
  append(svg, node('use', { href: '#plan' }));
  append(menu, node('BUTTON', { title: 'other' }, { isInteractive: true }));
  assert.equal(c.extractText(svg), 'plan');
  const doc = node('#document', {}, { nodeType: 9 });
  append(menu, doc); append(doc, svg);
  assert.equal(c.extractText(svg), 'plan');
});

test('viewport click points never clamp an offscreen element onto a different element', () => {
  const c = load();
  assert.equal(c.quadsToClickPoint([[600, 20, 620, 20, 620, 40, 600, 40]], 500, 600), null);
  const points = c.clickPointsFromQuads([[-10, 20, 30, 20, 30, 40, -10, 40]], 500, 600);
  assert.equal(points.length, 5);
  assert.ok(points.every(p => p.x >= 0 && p.x < 30 && p.y > 20 && p.y < 40));
});

test('runtime exceptionDetails are not mistaken for a successful protocol response', () => {
  const c = load();
  assert.throws(() => c.runtimeValue({ exceptionDetails: { text: 'synthetic error' }, result: {} }), /Runtime/);
  assert.equal(c.runtimeValue({ result: { value: false } }), false);
});

test('click failure releases the mouse but never retries mousePressed', async () => {
  for (const failedType of ['mousePressed', 'mouseReleased']) {
    const c = load(), calls = [];
    c.cdpSend = async (target, method, params) => {
      calls.push(params.type);
      if (params.type === failedType) throw new Error('synthetic protocol failure');
      return {};
    };
    await assert.rejects(c.dispatchRealClick({ tabId: 1 }, 20, 30), /未能完整确认/);
    assert.deepEqual(calls, ['mouseMoved', 'mousePressed', 'mouseReleased']);
  }
});

for (const scenario of ['blocked', 'no-geometry', 'partial', 'click', 'hover-overlay', 'hit-test-error']) {
  test(`doClick ${scenario}: no JS bypass and at most one physical click`, async () => {
    const c = load(), calls = [], pressed = [];
    let moved = false;
    c.cdpSend = async (target, method, params) => { calls.push(method); if (params?.type === 'mouseMoved') moved = true; return {}; };
    c.getActionGeometry = async () => scenario === 'no-geometry' ? null : ({ frames: [],
      offset: { x: 0, y: 0, sx: 1, sy: 1 }, quads: [[20, 20, 100, 20, 100, 60, 20, 60]] });
    c.checkOcclusion = async (target, id, x) => {
      if (scenario === 'hit-test-error') throw new Error('synthetic hit-test failure');
      return scenario === 'blocked' || (scenario === 'partial' && x > 45) || (scenario === 'hover-overlay' && moved);
    };
    c.dispatchRealClick = async (target, x, y) => pressed.push({ x, y });
    if (scenario === 'hit-test-error') {
      await assert.rejects(c.doClick({ tabId: 1 }, { tabId: 1 }, 2, 2, 500, 600), /hit-test/);
    } else {
      const result = await c.doClick({ tabId: 1 }, { tabId: 1 }, 2, 2, 500, 600);
      const shouldClick = scenario === 'partial' || scenario === 'click';
      assert.equal(result.success, shouldClick);
      assert.equal(pressed.length, shouldClick ? 1 : 0);
    }
    assert.ok(!calls.includes('Runtime.callFunctionOn'));
  });
}

test('failed geometry fallback releases its remote object and exposes script exceptions', async () => {
  const c = load(), calls = [];
  c.cdpSend = async (target, method) => {
    calls.push(method);
    if (method === 'DOM.getContentQuads' || method === 'DOM.getBoxModel') throw new Error('no geometry');
    if (method === 'DOM.resolveNode') return { object: { objectId: 'node-object' } };
    if (method === 'Runtime.callFunctionOn') return { exceptionDetails: { text: 'synthetic exception' } };
    return {};
  };
  await assert.rejects(c.getElementCoordinates({ tabId: 1 }, 7), /Runtime/);
  assert.equal(calls.at(-1), 'Runtime.releaseObject');
});

test('cross-process iframe dispatch uses its own session and local coordinates', async () => {
  const c = load(), checks = [], clicks = [];
  c.cdpSend = async () => ({});
  c.getActionGeometry = async () => ({ frames: [{ target: { tabId: 1 }, backendNodeId: 8,
    offset: { x: 0, y: 0, sx: 1, sy: 1 } }],
  offset: { x: 100, y: 200, sx: 2, sy: 2 }, quads: [[140, 260, 180, 260, 180, 300, 140, 300]] });
  c.checkOcclusion = async (target, id, x, y) => { checks.push({ target, id, x, y }); return false; };
  c.dispatchRealClick = async (target, x, y) => clicks.push({ target, x, y });
  const result = await c.doClick({ tabId: 1, sessionId: 'child' }, { tabId: 1 }, 9, 9, 500, 600);
  assert.equal(result.success, true);
  assert.deepEqual(clicks, [{ target: { tabId: 1, sessionId: 'child' }, x: 30, y: 40 }]);
  assert.ok(checks.some(c => !c.target.sessionId && c.id === 8 && c.x === 160 && c.y === 280));
});

test('script failure in hit testing releases target and independent-child handles', async () => {
  const c = load(), released = [];
  c.cdpSend = async (target, method, params) => {
    if (method === 'DOM.resolveNode') return { object: { objectId: `node-${params.backendNodeId}` } };
    if (method === 'DOM.getBoxModel') return { model: { border: [0, 0, 40, 0, 40, 40, 0, 40] } };
    if (method === 'Runtime.callFunctionOn') {
      assert.equal(params.arguments[3].objectId, 'node-2');
      return { exceptionDetails: { text: 'synthetic failure' } };
    }
    if (method === 'Runtime.releaseObject') released.push(params.objectId);
    return {};
  };
  await assert.rejects(c.checkOcclusion({ tabId: 1 }, 1, 20, 20, [2]), /Runtime/);
  assert.deepEqual(released.sort(), ['node-1', 'node-2']);
});

test('display:contents wrapper can label its icon without becoming a phantom click target', () => {
  const c = load(), wrapper = node('SPAN', { title: '保存' }, { isVisible: false });
  wrapper.snapshot.computedStyles.display = 'contents';
  const svg = append(wrapper, node('svg', { class: 'icon' }, { isInteractive: true }));
  assert.equal(c.extractText(svg), '保存');
  wrapper.snapshot.computedStyles = {};
  assert.equal(c.extractText(svg), '保存');
});
