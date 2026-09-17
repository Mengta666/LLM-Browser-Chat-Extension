const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Controller } = require('../../../extension/agent_execution.js');
const tick = () => new Promise(r => setImmediate(r));
const action = { type: 'click', index: 1, action_id: 'a', observation_id: 'o' };
async function setup(saved = null) {
  const control = new Controller({ read: async () => saved, write: async (id, value) => { saved = value; } });
  if (!saved) { await control.start(1, 's'); control.observeToken(1, 's', 'o'); }
  return control;
}
test('duplicate action only executes once; another action cannot overlap', async () => {
  const c = await setup(); let release, calls = 0;
  const perform = () => { calls++; return new Promise(r => { release = r; }); };
  const first = c.execute(1, 's', action, perform);
  await tick();
  const duplicate = c.execute(1, 's', action, perform);
  await assert.rejects(c.execute(1, 's', { ...action, action_id: 'b' }, perform), /旧动作/);
  release({ success: true });
  assert.deepEqual(await first, await duplicate);
  assert.equal(calls, 1);
});
test('cancellation fences remaining input; old cleanup cannot detach a new task', async () => {
  const c = await setup(); let release, inputs = 0;
  const pending = c.execute(1, 's', action, async ctx => {
    ctx.check(); inputs++;
    await new Promise(r => { release = r; });
    ctx.check(); inputs++;
    return { success: true };
  });
  await tick(); await c.cancel(1, 's');
  await assert.rejects(c.start(1, 'new'), /任务/);
  release(); await pending;
  assert.equal(inputs, 1);
  await c.end(1, 's', async () => {});
  await c.start(1, 'new');
  let detached = false;
  await c.end(1, 's', async () => { detached = true; });
  assert.equal(detached, false);
});
test('timed-out command must settle before releasing input ownership', async () => {
  const c = await setup(); let complete;
  const pending = c.execute(1, 's', action, async ctx => {
    ctx.dispatched = true;
    ctx.track(new Promise(r => { complete = r; }));
    return { success: false, error: 'timeout' };
  });
  await tick();
  assert.equal(c.status(1, 's', 'a').safe, false);
  await assert.rejects(c.execute(1, 's', { ...action, action_id: 'b' }, async () => ({})), /旧动作/);
  complete();
  assert.equal((await pending).execution_state, 'partial');
  assert.equal(c.status(1, 's', 'a').safe, true);
});
test('old observation cannot publish over a newer generation', async () => {
  const c = await setup(); const old = c.observeToken(1, 's', 'old');
  const newer = c.observeToken(1, 's', 'new'); let published;
  await c.publishObservation(newer, async () => { published = 'new'; });
  await assert.rejects(c.publishObservation(old, async () => { published = 'old'; }), /替换/);
  assert.equal(published, 'new');
  assert.equal((await c.execute(1, 's', action, () => assert.fail('旧观察不能执行'))).stale, true);
});
test('worker restart with outstanding journal cannot silently replay input', async () => {
  const c = await setup({ sessionId: 's', state: 'running' });
  await assert.rejects(c.start(1, 'new'), /未知/);
});

test('cancel during start storage read fences the delayed start', async () => {
  let release;
  const c = new Controller({ read: () => new Promise(r => { release = r; }), write: async () => {} });
  const pending = c.start(1, 's');
  await tick(); await c.cancel(1, 's'); release(null);
  await assert.rejects(pending, /停止/);
});

test('aborting an unreceived action fences its delayed delivery', async () => {
  const c = await setup();
  c.abortAction(1, 's', 'a');
  await assert.rejects(c.execute(1, 's', action, () => assert.fail('late input')), /停止/);
});

test('failed input or failed release remains unknown and cannot release tab ownership', async () => {
  const c = await setup();
  const result = await c.execute(1, 's', action, async ctx => {
    ctx.dispatched = true;
    const command = Promise.reject(new Error('release failed'));
    ctx.track(command, true);
    await command.catch(() => {});
    return { success: false };
  });
  assert.equal(result.execution_state, 'unknown');
  assert.equal(c.status(1, 's', 'a').state, 'unknown');
  let detached = false;
  assert.equal((await c.end(1, 's', async () => { detached = true; })).safe, false);
  assert.equal(detached, false);
  await assert.rejects(c.start(1, 'new'), /任务/);
});

test('changed content with same action ID is rejected', async () => {
  const c = await setup();
  await c.execute(1, 's', action, async () => ({ success: true }));
  await assert.rejects(c.execute(1, 's', { ...action, index: 2 }, () => {}), /不同内容/);
});

test('completed action journal write failure never pretends inputs were not dispatched', async () => {
  const c = await setup();
  c.store.write = async (id, value) => { if (value.state === 'finished') throw new Error('storage'); };
  const result = await c.execute(1, 's', action, async ctx => { ctx.dispatched = true; return { success: true }; });
  assert.equal(result.execution_state, 'unknown');
  assert.equal(c.status(1, 's', 'a').safe, false);
});

test('CDP timeout fences subsequent commands even when the action helper catches the error', async () => {
  const c = await setup(); let complete, inputs = 0, timedOut = false;
  const source = fs.readFileSync(path.resolve(__dirname, '../../../extension/background.js'), 'utf8');
  const context = vm.createContext({ setTimeout, clearTimeout, chrome: {
    runtime: {}, debugger: { sendCommand(target, method, params, callback) {
      if (method === 'Runtime.callFunctionOn') complete = callback;
      else { inputs++; callback({}); }
    } }
  } });
  vm.runInContext(source.slice(source.indexOf('function cdpSend('), source.indexOf('// ── SW 生命周期')), context);
  const pending = c.execute(1, 's', action, async ctx => {
    const target = { tabId: 1, _agentAction: ctx, _agentEffect: true };
    await context.cdpSend(target, 'Runtime.callFunctionOn', {}, 5).catch(() => { timedOut = true; });
    await context.cdpSend(target, 'Input.dispatchKeyEvent', { type: 'keyDown' });
    return { success: true };
  });
  for (let i = 0; i < 100 && !timedOut; i++) await new Promise(r => setTimeout(r, 1));
  assert.equal(timedOut, true);
  assert.equal(c.status(1, 's', 'a').safe, false);
  assert.equal(inputs, 0);
  complete({});
  assert.equal((await pending).execution_state, 'partial');
  assert.equal(inputs, 0);
});
