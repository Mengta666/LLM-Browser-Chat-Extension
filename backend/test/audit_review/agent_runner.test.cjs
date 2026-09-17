const assert = require('node:assert/strict');
const { test } = require('node:test');
const { Run } = require('../../../extension/agent_runner.js');
const tick = () => new Promise(r => setImmediate(r));
function setup(overrides = {}, limits = {}) {
  const messages = [], states = [];
  const io = {
    message: async m => { messages.push(m); return { ok: true, result: { protocol_version: 2 } }; },
    api: async () => ({ protocol_version: 2, session_id: 's', status: 'cancelled' }),
    annotate: async page => page, status: (...args) => states.push(args), ...overrides
  };
  const run = new Run('s', 7, io, { recovery: 100, observe: 10, action: 10, settleAction: 100, poll: 1, ...limits });
  return { run, messages, states };
}

test('observation failure automatically retries with a new ID without replaying actions', async () => {
  const { run, states } = setup(); let attempts = 0; const ids = [];
  run.io.message = async message => {
    assert.equal(message.tabId, 7);
    if (message.type !== 'AGENT_OBSERVE') return { ok: true };
    ids.push(message.observationId);
    if (++attempts === 1) throw new Error('transient navigation');
    return { ok: true, pageState: { tab_id: 7, observation_id: message.observationId, document_epoch: 'd' } };
  };
  assert.equal((await run.observe()).observation_id, ids[1]);
  assert.notEqual(ids[0], ids[1]);
  assert.equal(states[0][0], 'recovering');
});

test('recovery exhaustion never returns old page state', async () => {
  const { run } = setup({ message: async m => m.type === 'AGENT_OBSERVE' ? { ok: false, error: 'failed' } : { ok: true } }, { recovery: 8 });
  await assert.rejects(run.observe(), /未使用旧观察/);
});

test('late observation response cannot replace the recovered observation', async () => {
  const { run } = setup(); let late, first = true;
  run.io.message = async m => {
    if (m.type !== 'AGENT_OBSERVE') return { ok: true };
    const result = { ok: true, pageState: { tab_id: 7, observation_id: m.observationId, document_epoch: 'd' } };
    if (first) { first = false; return new Promise(r => { late = () => r(result); }); }
    return result;
  };
  const observed = await run.observe(); late(); await tick();
  assert.equal(observed.observation_id, 's:observe:2');
});

test('cancel during observation rejects immediately and never starts another input', async () => {
  const { run } = setup({ message: async m => m.type === 'AGENT_OBSERVE' ? new Promise(() => {}) : { ok: true } });
  const pending = run.observe();
  const rejected = assert.rejects(pending, { code: 'cancelled' });
  await tick(); await run.stop(); await rejected;
  let sent = false; run.io.message = async () => { sent = true; };
  await assert.rejects(run.execute({ type: 'click' }), { code: 'cancelled' });
  assert.equal(sent, false);
});

test('unknown decision delivery queries status, not a new decision', async () => {
  const calls = [];
  const { run } = setup({ api: async (path, body) => {
    calls.push({ path, body });
    if (calls.length === 1) throw new Error('connection lost after submit');
    return { protocol_version: 2, session_id: 's', status: 'action_required', action: { action_id: 'a' } };
  } });
  assert.equal((await run.decide('/v1/agent/execute', {})).action.action_id, 'a');
  assert.deepEqual(calls.map(c => c.path), ['/v1/agent/execute', '/v1/agent/status']);
  assert.equal(calls[0].body.request_id, calls[1].body.request_id);
});

test('unreceived decision is resubmitted with exactly the same body and ID', async () => {
  const calls = [];
  const { run } = setup({ api: async (path, body) => {
    calls.push({ path, body });
    if (calls.length === 1) throw new Error('network');
    if (calls.length === 2) throw Object.assign(new Error('not received'), { status: 404 });
    return { protocol_version: 2, session_id: 's', status: 'completed' };
  } });
  await run.decide('/v1/agent/execute', { task: 'test' });
  assert.equal(calls[2].body, calls[0].body);
});

test('incompatible backend response fails without resubmitting', async () => {
  let calls = 0;
  const { run } = setup({ api: async () => { calls++; return { status: 'action_required' }; } });
  await assert.rejects(run.decide('/v1/agent/execute', {}), /协议/);
  assert.equal(calls, 1);
});

test('action transport timeout queries original action and never replays it', async () => {
  let executions = 0, polls = 0;
  const result = { action_id: 'a', execution_state: 'partial', success: false };
  const { run } = setup({ message: async m => {
    if (m.type === 'AGENT_EXECUTE') { executions++; return new Promise(() => {}); }
    if (m.command === 'status') return { ok: true, result: ++polls === 1 ? { state: 'running' } : { state: 'finished', result } };
    assert.equal(m.command, 'abort_action');
    return { ok: true };
  } });
  assert.deepEqual(await run.execute({ action_id: 'a' }), result);
  assert.equal(executions, 1);
});

test('unknown action prevents continuation', async () => {
  const { run } = setup({ message: async m => m.type === 'AGENT_EXECUTE'
    ? { ok: true, result: { execution_state: 'unknown' } }
    : { ok: true, result: { state: 'unknown' } } });
  await assert.rejects(run.execute({ action_id: 'a' }), /阻止后续输入/);
});

test('cancel during model decision discards late completed result', async () => {
  let release;
  const { run } = setup({ api: async path => path.endsWith('/cancel') ? {} : new Promise(r => { release = r; }) });
  const pending = run.decide('/v1/agent/execute', {});
  const rejected = assert.rejects(pending, { code: 'cancelled' });
  await tick(); await run.stop();
  release({ protocol_version: 2, session_id: 's', status: 'completed' });
  await rejected;
});
