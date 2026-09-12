// Execute the original functions without changing their timing constants or bodies.
// Chrome messaging, fetch, and the clock are test doubles; no network or browser is used.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../../..');
const background = fs.readFileSync(path.join(root, 'extension/background.js'), 'utf8');
const panel = fs.readFileSync(path.join(root, 'extension/sidepanel.js'), 'utf8');

function section(source, start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a);
  if (a < 0 || b < 0) throw Error(`Source boundary missing: ${start}`);
  return source.slice(a, b);
}

async function flush() {
  for (let i = 0; i < 30; i++) await Promise.resolve();
}

function clock() {
  let now = 0;
  const pending = [];
  return {
    get now() { return now; },
    setTimeout(fn, ms) { pending.push({ at: now + ms, fn }); },
    async advance(target) {
      await flush();
      while (true) {
        pending.sort((a, b) => a.at - b.at);
        if (!pending.length || pending[0].at > target) break;
        const event = pending.shift();
        now = event.at;
        event.fn();
        await flush();
      }
      now = target;
      await flush();
    },
  };
}

function element() {
  return {
    innerHTML: '', textContent: '', style: {}, children: [],
    appendChild(child) { this.children.push(child); },
    addEventListener() {},
  };
}

async function backgroundCase(finalAt) {
  const time = clock();
  const messages = [];
  const timeouts = [];
  const context = {
    URL, TextDecoder, MAX_LLM_BODY_BYTES: 1000000,
    isAllowedChatUrl: async () => true,
    isPrivateOrLocalHost: () => true,
    chrome: { runtime: { sendMessage: m => messages.push(m) } },
    AbortSignal: {
      timeout(ms) {
        timeouts.push(ms);
        const controller = new AbortController();
        time.setTimeout(() => controller.abort(new DOMException('Total timeout', 'TimeoutError')), ms);
        return controller.signal;
      },
    },
    async fetch(url, options) {
      const frames = [
        [40000, { enhancement_step: { status: 'running' } }],
        [80000, { enhancement_step: { status: 'done' } }],
        [finalAt, { choices: [{ delta: { content: 'final-answer' } }] }],
        [finalAt, '[DONE]'],
      ];
      let index = 0;
      return {
        ok: true, headers: { get: () => 'text/event-stream' },
        body: { getReader: () => ({ read: () => new Promise((resolve, reject) => {
          if (options.signal.aborted) return reject(options.signal.reason);
          const onAbort = () => reject(options.signal.reason);
          options.signal.addEventListener('abort', onAbort, { once: true });
          const [at, frame] = frames[index++];
          time.setTimeout(() => {
            options.signal.removeEventListener('abort', onAbort);
            resolve({ done: false, value: Buffer.from(`data: ${typeof frame === 'string' ? frame : JSON.stringify(frame)}\n\n`) });
          }, Math.max(0, at - time.now));
        }) }) },
      };
    },
  };
  vm.createContext(context);
  vm.runInContext(section(background, 'function sendLlmMessage(', 'async function getResponseErrorMessage('), context);
  let errorName = '';
  const task = context.handleCallLlmStream({
    url: 'http://127.0.0.1:8000/v1/chat/completions', msgId: 'audit',
    options: { method: 'POST', headers: {}, body: '{"stream":true}' },
  }).catch(error => { errorName = error.name; });
  await time.advance(150000);
  await task;
  return {
    timeouts, errorName,
    progressCount: messages.filter(m => m.type === 'LLM_ENHANCEMENT_STEP').length,
    answers: messages.filter(m => m.type === 'LLM_CHUNK').map(m => m.chunk),
  };
}

async function panelCase(finalAt) {
  const time = clock();
  const listeners = new Set();
  const finalizedAt = [];
  const nodes = [];
  const context = {
    window: {}, chatMessages: [], alert: () => {},
    serverContextBases: new Set(), callBackendApi: async () => ({}),
    buildBackendEndpointUrl: (base, endpoint) => base + endpoint,
    resolveApiRequestConfig: async () => ({ apiKey: '', modelName: 'offline-test', safeApiUrl: 'http://127.0.0.1:8000/v1' }),
    ensurePrivacyNoticeAccepted: async () => true,
    createMessageNode: () => { const node = element(); nodes.push(node); return node; },
    document: { createElement: element },
    showTypingIndicator() {}, scrollToBottom() {}, updateEnhancementCard() {}, renderSearchCitations() {},
    getOrCreateCurrentChatId: async () => 'audit-chat', createMessageId: () => 'audit',
    createMarkdownStreamer: () => ({ update() {}, cancel() {}, finalize() {} }),
    setTimeout: (fn, ms) => time.setTimeout(fn, ms),
    chrome: { runtime: {
      onMessage: {
        addListener: fn => listeners.add(fn),
        removeListener: fn => { finalizedAt.push(time.now); listeners.delete(fn); },
      },
      sendMessage() {
        const emit = message => { for (const fn of [...listeners]) fn({ msgId: 'audit', ...message }); };
        time.setTimeout(() => emit({ type: 'LLM_ENHANCEMENT_STEP', step: { status: 'running' } }), 40000);
        time.setTimeout(() => emit({ type: 'LLM_ENHANCEMENT_STEP', step: { status: 'done' } }), 80000);
        time.setTimeout(() => {
          emit({ type: 'LLM_CHUNK', chunk: 'final-answer' });
          emit({ type: 'LLM_DONE' });
        }, finalAt);
      },
    } },
  };
  vm.createContext(context);
  vm.runInContext(section(panel, '  async function runPlainChat(', '  // ── 会话历史'), context);
  const task = context.runPlainChat('synthetic audit question', '', '');
  await time.advance(150000);
  await task;
  return {
    finalizedAt, history: context.chatMessages,
    displayedText: nodes[1].children.at(-1).textContent,
  };
}

function citationCase(url, html = '<p>Answer [1]</p>') {
  const body = element();
  body.innerHTML = html;
  const bubble = element();
  bubble.querySelector = () => body;
  const context = { document: { createElement: element } };
  vm.createContext(context);
  const start = panel.indexOf('  function renderSearchCitations(');
  const end = panel.indexOf('\n  }', start) + '\n  }'.length;
  if (start < 0 || end < start) throw Error('Citation function not found');
  vm.runInContext(panel.slice(start, end), context);
  context.renderSearchCitations(bubble, [{ title: 'Audit source', url, snippet: '' }]);
  return body.innerHTML;
}

(async () => {
  const output = {
    slowBackground: await backgroundCase(130000),
    fastBackground: await backgroundCase(100000),
    slowPanel: await panelCase(130000),
    fastPanel: await panelCase(100000),
    normalCitation: citationCase('https://example.invalid/source'),
    injectedCitation: citationCase('https://example.invalid/\"><span id="audit-injected">UNTRUSTED LABEL</span><a href="'),
    attributeCitation: citationCase('https://example.invalid/source', '<p><a href="https://example.invalid/?q=[1]">normal link</a></p>'),
  };
  process.stdout.write(JSON.stringify(output));
})().catch(error => { console.error(error); process.exitCode = 1; });
