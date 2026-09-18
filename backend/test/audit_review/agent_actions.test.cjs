const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../../extension/background.js'), 'utf8');
function load(cdpSend) {
  const c = vm.createContext({ cdpSend, AgentEditing: require('../../../extension/agent_editing.js'),
    runtimeValue: r => r?.result?.value, sleep: async () => {}, typeChars: async () => assert.fail('must not type after failed clear') });
  vm.runInContext(source.slice(source.indexOf('async function withEditingTarget('), source.indexOf('// 悬停:')), c);
  return c;
}

test('clear with missing node is stale, not successful', async () => {
  const c = load(async () => ({}));
  const result = await c.doClear({}, 1, 1);
  assert.equal(result.success, false);
  assert.equal(result.stale, true);
});

test('native select propagates a known missing-option result', async () => {
  const c = load(async (target, method, params) => {
    if (method === 'DOM.resolveNode') return { object: { objectId: 'n' } };
    return { result: { value: params.arguments ? { success: false, error: '选项不存在' } : true } };
  });
  const result = await c.doSelect({}, {}, 1, { index: 1, params: { option_text: 'missing' } });
  assert.equal(result.success, false);
  assert.match(result.error, /选项/);
});

test('type(clear=true) stops when clearing is rejected', async () => {
  const c = load(async (target, method, params) => {
    if (method === 'DOM.resolveNode') return { object: { objectId: 'n' } };
    if (!params.arguments) return { result: { objectId: 'descriptor' } };
    const operation = params.arguments[0].value;
    if (operation === 'read') return { result: { value: { success: true, value: 'old' } } };
    if (operation === 'set_native') return { result: { value: { success: false, error: '只读' } } };
    return { result: { value: { success: true, kind: 'native', editable: true, focused: true } } };
  });
  const result = await c.doType({}, {}, 1, { params: { text: 'new', clear: true } });
  assert.equal(result.success, false);
  assert.equal(result.action_type, 'type');
  assert.match(result.error, /只读/);
});
