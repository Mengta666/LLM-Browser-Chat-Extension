const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { Run } = require('../../../extension/agent_runner.js');
const { Controller } = require('../../../extension/agent_execution.js');
const source = fs.readFileSync(path.resolve(__dirname, '../../../extension/sidepanel.js'), 'utf8');
const body = source.slice(source.indexOf('  async function runAgentTask('), source.indexOf('  // 拦截发送：'));
const tick = () => new Promise(r => setImmediate(r));

function setup() {
  const nodes = [], messages = [], apiCalls = [], errors = [], results = [], rendered = [];
  const control = new Controller({ read: async () => null, write: async () => {} });
  const stats = { disconnects: 0, observations: 0, inputs: 0, apiCalls, messages, errors, results, rendered };
  const node = tag => {
    const element = { tag, children: [], className: '', textContent: '',
      appendChild(child) { this.children.push(child); return child; },
      querySelectorAll() { return []; }, remove() { this.removed = true; } };
    nodes.push(element); return element;
  };
  const context = vm.createContext({
    console, createMessageId: () => 'test', agentState: {},
    document: { createElement: node }, createMessageNode: () => node('bubble'), scrollToBottom() {},
    AgentRunner: { Run: class extends Run { constructor(...args) { super(...args); this.limits.poll = 1; } } },
    AgentObservation: { annotate: async page => page },
    resolveApiRequestConfig: async () => ({ safeApiUrl: 'http://local.test', modelName: 'offline' }),
    getActiveBrowserTab: async () => ({ id: 7 }), waitForPageSettle: async id => { assert.equal(id, 7); },
    detectInlineGroup: () => null, showAgentConfirmDialog: async () => true,
    renderAgentStepInBubble: (...args) => rendered.push(args),
    renderAgentError: (bubble, text) => errors.push(text),
    renderAgentComplete: (bubble, summary, success) => results.push({ summary, success }),
    callAgentApi: async (url, apiPath, payload) => {
      apiCalls.push({ path: apiPath, payload });
      const basic = { protocol_version: 2, session_id: payload.session_id };
      if (apiPath.endsWith('/execute')) return { ...basic, status: 'action_required', step: 1,
        action: { type: 'click', action_id: 'a', observation_id: payload.page_state.observation_id } };
      return { ...basic, status: apiPath.endsWith('/step') ? 'completed' : 'cancelled', summary: 'synthetic', success: true };
    },
    chrome: { storage: { local: { get: async () => ({}) } }, runtime: {
      connect: () => ({ onDisconnect: { addListener() {} }, disconnect() { stats.disconnects++; } }),
      sendMessage: async m => {
        messages.push(m);
        const { tabId, sessionId, command } = m;
        try {
          if (m.type === 'AGENT_CONTROL') {
            let result;
            if (command === 'start') result = await control.start(tabId, sessionId);
            if (command === 'cancel') result = await control.cancel(tabId, sessionId);
            if (command === 'end') result = await control.end(tabId, sessionId, async () => {});
            if (command === 'status') result = control.status(tabId, sessionId, m.actionId);
            if (command === 'invalidate_observation') result = control.invalidateObservation(tabId, sessionId, m.observationId);
            return { ok: true, result };
          }
          if (m.type === 'AGENT_OBSERVE') {
            stats.observations++;
            control.observeToken(tabId, sessionId, m.observationId);
            return { ok: true, pageState: { tab_id: tabId, observation_id: m.observationId,
              document_epoch: 'doc', interactive_elements: [], url: 'http://local.test/fixture' } };
          }
          if (m.type === 'AGENT_EXECUTE') return { ok: true, result: await control.execute(tabId, sessionId, m.action,
            async ctx => { ctx.check(); stats.inputs++; return { success: true, action_type: 'click' }; }) };
          throw new Error('unexpected message ' + m.type);
        } catch (e) { return { ok: false, code: e.code, error: e.message }; }
      }
    } }
  });
  vm.runInContext(body, context);
  return { context, stats, nodes, control };
}

test('panel complete flow binds one tab, sends fresh observation and action ID', async () => {
  const { context, stats } = setup(); let tabQueries = 0;
  context.getActiveBrowserTab = async () => ({ id: ++tabQueries === 1 ? 7 : 8 });
  await context.runAgentTask('synthetic');
  assert.equal(stats.inputs, 1);
  assert.equal(stats.observations, 2);
  assert.equal(stats.results.length, 1);
  assert.equal(stats.errors.length, 0);
  const [initial, step] = stats.apiCalls;
  assert.equal(step.payload.action_result.action_id, 'a');
  assert.notEqual(initial.payload.page_state.observation_id, step.payload.page_state.observation_id);
  assert.equal(stats.messages.every(m => m.tabId === 7), true);
  assert.equal(stats.disconnects, 1);
  assert.equal(context.agentState.active, false);
});

test('panel observation recovery never repeats the completed action', async () => {
  const { context, stats } = setup();
  const send = context.chrome.runtime.sendMessage;
  let observations = 0;
  context.chrome.runtime.sendMessage = async m => {
    if (m.type === 'AGENT_OBSERVE' && ++observations === 2) return { ok: false, error: 'navigation' };
    return send(m);
  };
  await context.runAgentTask('synthetic');
  assert.equal(observations, 3);
  assert.equal(stats.inputs, 1);
  assert.equal(stats.errors.length, 0);
  assert.equal(stats.results.length, 1);
});

test('stop button already exists during initial observation and suppresses late decision', async () => {
  const { context, stats, nodes } = setup();
  const send = context.chrome.runtime.sendMessage;
  let observing = false;
  context.chrome.runtime.sendMessage = async m => {
    if (m.type === 'AGENT_OBSERVE') { observing = true; return new Promise(() => {}); }
    return send(m);
  };
  const pending = context.runAgentTask('synthetic');
  for (let i = 0; i < 20 && !observing; i++) await tick();
  assert.equal(observing, true);
  const stop = nodes.find(n => n.className === 'agent-btn-cancel');
  assert.ok(stop);
  stop.onclick();
  await pending;
  assert.equal(stats.inputs, 0);
  assert.equal(stats.apiCalls.every(c => c.path.endsWith('/cancel')), true);
  assert.equal(stats.results.length, 0);
  assert.equal(stats.disconnects, 1);
});

test('configuration failure still releases keepalive and foreground busy state', async () => {
  const { context, stats } = setup();
  context.resolveApiRequestConfig = async () => { throw new Error('invalid configuration'); };
  await context.runAgentTask('synthetic');
  assert.deepEqual(stats.errors, ['invalid configuration']);
  assert.equal(stats.disconnects, 1);
  assert.equal(context.agentState.active, false);
});

test('old panel cleanup cannot clear a replacement task state', async () => {
  const { context } = setup();
  context.resolveApiRequestConfig = async () => {
    Object.assign(context.agentState, { active: true, sessionId: 'replacement', status: 'running' });
    throw new Error('old failure');
  };
  await context.runAgentTask('synthetic');
  assert.equal(context.agentState.active, true);
  assert.equal(context.agentState.status, 'running');
});
