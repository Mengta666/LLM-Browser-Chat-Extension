const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../../extension/background.js'), 'utf8');
function keys(send) {
  const calls = [];
  const c = vm.createContext({ sleep: async () => {}, cdpSend: async (target, method, params) => {
    calls.push({ method, ...params });
    if (send) await send(target, method, params);
  } });
  vm.runInContext(source.slice(source.indexOf('const KEY_MODIFIERS ='), source.indexOf('// 命中目标本身')), c);
  return { c, calls };
}
test('printable press_key emits a valid physical code and character event', async () => {
  const { c, calls } = keys();
  await c.dispatchSpecialKey({}, 'a', []);
  assert.equal(calls[0].code, 'KeyA');
  assert.equal(calls[0].windowsVirtualKeyCode, 65);
  assert.equal(calls.find(c => c.type === 'char')?.text, 'a');
});
test('Ctrl+A uses KeyA and never inserts an a', async () => {
  const { c, calls } = keys();
  await c.dispatchSpecialKey({}, 'a', ['Control']);
  const down = calls.find(c => c.type === 'keyDown' && c.key.toLowerCase() === 'a');
  assert.equal(down.code, 'KeyA');
  assert.equal(down.windowsVirtualKeyCode, 65);
  assert.equal(down.modifiers, 2);
  assert.equal(calls.some(c => c.type === 'char'), false);
});
test('text newlines and unicode are inserted as text, never Enter', async () => {
  const { c, calls } = keys();
  await c.typeChars({}, '中🙂\n');
  assert.equal(calls.some(c => c.key === 'Enter'), false);
  assert.equal(calls.filter(c => c.method === 'Input.insertText').map(c => c.text).join(''), '中🙂\n');
});
test('unsupported key and modifier fail before any input is sent', async () => {
  const { c, calls } = keys();
  await assert.rejects(c.dispatchSpecialKey({}, 'Control+A', []));
  await assert.rejects(c.dispatchSpecialKey({}, 'a', ['oops']));
  assert.equal(calls.length, 0);
});

test('focus loss after keyDown suppresses char but still releases the key', async () => {
  const { c, calls } = keys();
  let checks = 0;
  await assert.rejects(c.typeChars({}, 'AB', async () => { if (++checks === 2) throw new Error('focus lost'); }));
  assert.equal(calls.some(c => c.type === 'char'), false);
  assert.deepEqual(calls.map(c => c.type), ['keyDown', 'keyUp']);
});

test('shift punctuation gets Digit1 and an exclamation mark', async () => {
  const { c, calls } = keys();
  await c.dispatchSpecialKey({}, '1', ['Shift']);
  assert.equal(calls[0].code, 'Digit1');
  assert.equal(calls[0].key, '!');
  assert.equal(calls.find(c => c.type === 'char').text, '!');
});
