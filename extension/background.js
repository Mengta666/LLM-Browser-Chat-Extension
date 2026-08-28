importScripts('shared.js');

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

const MAX_LLM_BODY_BYTES = 25 * 1024 * 1024;
const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
const AGENT_ENDPOINT_PATHS = ['/v1/agent/execute', '/v1/agent/step', '/v1/agent/cancel'];

// 会话历史 + 记忆管理 CRUD 的路径前缀(支持 GET/POST/PATCH/DELETE，仅本地/自定义后端）
const BACKEND_API_PREFIXES = ['/v1/sessions', '/v1/memory'];

function normalizeChatUrl(value) {
  const url = new URL(String(value || ''));
  return `${url.origin}${url.pathname.replace(/\/$/, '')}`;
}

function buildBackendRootFromApiBase(apiBaseUrl) {
  const normalizedApiBaseUrl = normalizeApiBaseUrl(apiBaseUrl);
  const url = new URL(normalizedApiBaseUrl);
  let pathname = url.pathname.replace(/\/$/, '');
  if (pathname.endsWith('/v1')) {
    pathname = pathname.slice(0, -3);
  }
  return `${url.origin}${pathname}`;
}

async function getAllowedPageRefreshUrls() {
  const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
  return new Set(
    customUrls
      .map((url) => {
        try {
          return `${buildBackendRootFromApiBase(url)}${PAGE_REFRESH_ENDPOINT_PATH}`;
        } catch {
          return '';
        }
      })
      .filter(Boolean)
  );
}

async function isAllowedPageRefreshUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const allowedUrls = await getAllowedPageRefreshUrls();
    return allowedUrls.has(normalizeChatUrl(url.href))
      && !url.search
      && !url.hash;
  } catch {
    return false;
  }
}

async function isAllowedAgentUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
    const normalized = normalizeChatUrl(url.href);
    for (const baseUrl of customUrls) {
      try {
        const root = buildBackendRootFromApiBase(baseUrl);
        for (const path of AGENT_ENDPOINT_PATHS) {
          if (normalized === `${root}${path}`) return true;
        }
      } catch { /* skip invalid */ }
    }
    return false;
  } catch {
    return false;
  }
}

// 会话/记忆 CRUD：路径以白名单前缀开头即放行（覆盖 /v1/sessions/{id}、/v1/memory/{id} 等动态段）
async function isAllowedBackendApiUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
    const normalized = normalizeChatUrl(url.href);
    for (const baseUrl of customUrls) {
      try {
        const root = buildBackendRootFromApiBase(baseUrl);
        for (const prefix of BACKEND_API_PREFIXES) {
          if (normalized === `${root}${prefix}` || normalized.startsWith(`${root}${prefix}/`)) return true;
        }
      } catch { /* skip invalid */ }
    }
    return false;
  } catch {
    return false;
  }
}

const DEFAULT_ALLOWED_CHAT_URLS = new Set([
  'https://api.openai.com/v1/chat/completions'
]);

async function getAllowedChatUrls() {
  const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
  return new Set([
    ...DEFAULT_ALLOWED_CHAT_URLS,
    ...customUrls
      .map((url) => {
        try {
          return `${normalizeApiBaseUrl(url)}/chat/completions`;
        } catch {
          return '';
        }
      })
      .filter(Boolean)
  ]);
}

async function isAllowedChatUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const allowedUrls = await getAllowedChatUrls();
    return allowedUrls.has(normalizeChatUrl(url.href)) && !url.search && !url.hash;
  } catch {
    return false;
  }
}

function sendLlmMessage(msgId, type, extra = {}) {
  chrome.runtime.sendMessage({ type, msgId, ...extra });
}

function extractChunkText(dataObj) {
  return dataObj?.choices?.[0]?.delta?.content
    || dataObj?.choices?.[0]?.message?.content
    || dataObj?.choices?.[0]?.text
    || dataObj?.message?.content
    || dataObj?.response
    || dataObj?.content
    || '';
}

async function handleCallLlmStream(request) {
  const { url, options, msgId } = request;

  if (!(await isAllowedChatUrl(url))) {
    sendLlmMessage(msgId, 'LLM_ERROR', { error: 'API 地址不被允许' });
    return;
  }

  const apiHost = new URL(url).hostname;
  const allowMissingAuth = isPrivateOrLocalHost(apiHost);
  const authHeader = options?.headers?.Authorization || options?.headers?.authorization || '';
  const authToken = String(authHeader).replace(/^Bearer\s+/i, '').trim();
  const hasBearerAuth = String(authHeader).startsWith('Bearer ') && !!authToken;
  if (options?.method !== 'POST' || (!hasBearerAuth && !allowMissingAuth)) {
    sendLlmMessage(msgId, 'LLM_ERROR', { error: 'API 请求配置无效' });
    return;
  }

  const body = String(options?.body || '');
  if (!body || body.length > MAX_LLM_BODY_BYTES) {
    sendLlmMessage(msgId, 'LLM_ERROR', { error: 'API 请求体为空或过大' });
    return;
  }
  try {
    JSON.parse(body);
  } catch {
    sendLlmMessage(msgId, 'LLM_ERROR', { error: 'API 请求体不是有效 JSON' });
    return;
  }

  const requestHeaders = { 'Content-Type': 'application/json' };
  if (hasBearerAuth) requestHeaders.Authorization = authHeader;

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'omit',
    redirect: 'error',
    headers: requestHeaders,
    body,
    signal: AbortSignal.timeout(120000)
  });

  if (!response.ok) {
    const errorMessage = await getResponseErrorMessage(response);
    sendLlmMessage(msgId, 'LLM_ERROR', { error: `请求失败 (${response.status})：${errorMessage}` });
    return;
  }

  const contentType = response.headers.get('content-type') || '';
  if (!/text\/event-stream|text\/plain/i.test(contentType)) {
    const dataObj = await response.json();
    const content = extractChunkText(dataObj);
    if (content) {
      sendLlmMessage(msgId, 'LLM_CHUNK', { chunk: content });
      sendLlmMessage(msgId, 'LLM_DONE');
    } else {
      sendLlmMessage(msgId, 'LLM_ERROR', { error: '响应中没有可显示的文本内容' });
    }
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      sendLlmMessage(msgId, 'LLM_DONE');
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith(':')) continue;
      const dataStr = trimmed.startsWith('data:') ? trimmed.substring(5).trim() : trimmed;
      if (dataStr === '[DONE]') {
        sendLlmMessage(msgId, 'LLM_DONE');
        return;
      }
      try {
        const chunk = extractChunkText(JSON.parse(dataStr));
        if (chunk) sendLlmMessage(msgId, 'LLM_CHUNK', { chunk });
      } catch {
        // 忽略无法解析的流式碎片
      }
    }
  }
}

async function getResponseErrorMessage(response) {
  const fallbackMessage = `${response.status} ${response.statusText}`.trim() || '未知错误';

  let responseText = '';
  try {
    responseText = await response.text();
  } catch {
    return fallbackMessage;
  }

  const trimmedText = responseText.trim();
  if (!trimmedText) {
    return fallbackMessage;
  }

  try {
    const parsed = JSON.parse(trimmedText);
    return parsed?.error?.message
      || parsed?.message
      || parsed?.detail
      || trimmedText
      || fallbackMessage;
  } catch {
    return trimmedText;
  }
}

async function handleCallApiJson(request) {
  const { url, options } = request;

  const isPageRefresh = await isAllowedPageRefreshUrl(url);
  const isAgent = await isAllowedAgentUrl(url);
  if (!isPageRefresh && !isAgent) {
    throw new Error('API 地址不被允许');
  }

  const apiHost = new URL(url).hostname;
  const allowMissingAuth = isPrivateOrLocalHost(apiHost);
  const authHeader = options?.headers?.Authorization || options?.headers?.authorization || '';
  const authToken = String(authHeader).replace(/^Bearer\s+/i, '').trim();
  const hasBearerAuth = String(authHeader).startsWith('Bearer ') && !!authToken;
  if (options?.method !== 'POST' || (!hasBearerAuth && !allowMissingAuth)) {
    throw new Error('API 请求配置无效');
  }

  const body = String(options?.body || '');
  if (!body || body.length > MAX_LLM_BODY_BYTES) {
    throw new Error('API 请求体为空或过大');
  }

  try {
    JSON.parse(body);
  } catch {
    throw new Error('API 请求体不是有效 JSON');
  }

  const requestHeaders = {
    'Content-Type': 'application/json'
  };
  if (hasBearerAuth) {
    requestHeaders.Authorization = authHeader;
  }

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'omit',
    redirect: 'error',
    headers: requestHeaders,
    body,
    signal: AbortSignal.timeout(120000)
  });

  if (!response.ok) {
    const errorMessage = await getResponseErrorMessage(response);
    throw new Error(`请求失败 (${response.status})：${errorMessage}`);
  }

  return response.json();
}

// 会话历史 + 记忆管理 CRUD 代理:支持 GET/POST/PATCH/DELETE，路径前缀白名单，仅本地/自定义后端。
// 与 handleCallApiJson(仅 POST、agent 专用)分开，避免放宽后者的安全约束。
async function handleCallBackendApi(request) {
  const { url, options } = request;

  if (!(await isAllowedBackendApiUrl(url))) {
    throw new Error('API 地址不被允许');
  }

  const method = String(options?.method || 'GET').toUpperCase();
  const ALLOWED_METHODS = new Set(['GET', 'POST', 'PATCH', 'DELETE']);
  if (!ALLOWED_METHODS.has(method)) {
    throw new Error('不支持的请求方法');
  }

  const apiHost = new URL(url).hostname;
  const allowMissingAuth = isPrivateOrLocalHost(apiHost);
  const authHeader = options?.headers?.Authorization || options?.headers?.authorization || '';
  const authToken = String(authHeader).replace(/^Bearer\s+/i, '').trim();
  const hasBearerAuth = String(authHeader).startsWith('Bearer ') && !!authToken;
  if (!hasBearerAuth && !allowMissingAuth) {
    throw new Error('API 请求配置无效');
  }

  const requestHeaders = { 'Content-Type': 'application/json' };
  if (hasBearerAuth) requestHeaders.Authorization = authHeader;

  const fetchOptions = {
    method,
    credentials: 'omit',
    redirect: 'error',
    headers: requestHeaders,
    signal: AbortSignal.timeout(30000)
  };
  // GET/DELETE 通常无 body;POST/PATCH 带 body(校验为合法 JSON 且不过大)
  if (method === 'POST' || method === 'PATCH') {
    const body = String(options?.body || '');
    if (!body || body.length > MAX_LLM_BODY_BYTES) {
      throw new Error('API 请求体为空或过大');
    }
    try {
      JSON.parse(body);
    } catch {
      throw new Error('API 请求体不是有效 JSON');
    }
    fetchOptions.body = body;
  }

  const response = await fetch(url, fetchOptions);
  if (!response.ok) {
    const errorMessage = await getResponseErrorMessage(response);
    throw new Error(`请求失败 (${response.status})：${errorMessage}`);
  }
  return response.json();
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.type === 'CALL_LLM_STREAM') {
    handleCallLlmStream(request).catch((error) => {
      chrome.runtime.sendMessage({ type: 'LLM_ERROR', msgId: request.msgId, error: `请求失败：${error?.message || '未知错误'}` });
    });
    return true;
  }

  if (request.type === 'CALL_API_JSON') {
    handleCallApiJson(request)
      .then((body) => sendResponse({ ok: true, body }))
      .catch((error) => sendResponse({ ok: false, error: error?.message || '未知错误' }));
    return true;
  }

  if (request.type === 'CALL_BACKEND_API') {
    handleCallBackendApi(request)
      .then((body) => sendResponse({ ok: true, body }))
      .catch((error) => sendResponse({ ok: false, error: error?.message || '未知错误' }));
    return true;
  }

  if (request.type === 'DEBUGGER_HOVER') {
    handleDebuggerHover(request.tabId, request.x, request.y)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error?.message || '未知错误' }));
    return true;
  }

  if (request.type === 'DEBUGGER_DETACH') {
    debuggerDetach(request.tabId)
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: true }));
    return true;
  }

  // CDP 观察/执行（Phase 1+，对齐 browser-use）
  if (request.type === 'AGENT_OBSERVE') {
    handleAgentObserve(request.tabId)
      .then((r) => sendResponse({ ok: true, pageState: r.pageState }))
      .catch((error) => sendResponse({ ok: false, error: error?.message || '观察失败' }));
    return true;
  }

  if (request.type === 'AGENT_EXECUTE') {
    handleAgentExecute(request.tabId, request.action)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error?.message || '执行失败' }));
    return true;
  }
});

// Debugger 会话管理：保持 attach 状态复用，避免每次 attach/detach 的开销
const _debuggerAttached = new Set();

chrome.debugger.onDetach.addListener((source) => {
  _debuggerAttached.delete(source.tabId);
  if (source.tabId != null) {
    // 标记未挂载（如用户点了横幅"取消"）；下次 ensureAttached 会重连并 bump epoch → execute 侧转 stale。
    saveTabState(STATE_KEYS.attach, source.tabId, { attached: false, sessionEpoch: _sessionEpoch.get(source.tabId) || 0 });
  }
});

async function debuggerEnsureAttached(tabId) {
  if (_debuggerAttached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, '1.3');
  _debuggerAttached.add(tabId);
  // 新会话：递增 epoch。SW 被杀重启后内存 Set 清空，此处重连即重新 bump，旧观察编号随之失效。
  await bumpSessionEpoch(tabId);
  // OOPIF：开 autoAttach{flatten}，子 target（跨源 iframe）自动挂载，事件走 chrome.debugger.onEvent。
  await armAutoAttach(tabId);
}

// ── OOPIF 会话管理（Phase 4，对齐 session_manager.py）──
// flatten=true 所有子 session 复用同一根 WS，靠 sessionId 区分。4-dict 维护映射，内存态（SW 重启后
// onEvent 会重新累积 + armAutoAttach 重建），仅需内存即可（每轮 observe 重取子树，不依赖跨 SW 存活）。
const _oopif = new Map();   // tabId -> { targets:Map<targetId,info>, sessions:Map<sessionId,{targetId}>,
                            //            targetSessions:Map<targetId,Set<sessionId>>, sessionToTarget:Map<sessionId,targetId> }

function oopifState(tabId) {
  if (!_oopif.has(tabId)) {
    _oopif.set(tabId, { targets: new Map(), sessions: new Map(), targetSessions: new Map(), sessionToTarget: new Map() });
  }
  return _oopif.get(tabId);
}

async function armAutoAttach(tabId) {
  try {
    await cdpSend({ tabId }, 'Target.setDiscoverTargets', { discover: true, filter: [{ type: 'page' }, { type: 'iframe' }] });
  } catch { /* 部分 Chrome 版本 filter 不支持,忽略 */ }
  await cdpSend({ tabId }, 'Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
}

// chrome.debugger.onEvent：flatten 模式下子 target 的事件带 source.sessionId。
chrome.debugger.onEvent.addListener((source, method, params) => {
  const tabId = source.tabId;
  if (tabId == null) return;
  if (method === 'Target.attachedToTarget') {
    handleTargetAttached(tabId, params).catch(() => {});
  } else if (method === 'Target.detachedFromTarget') {
    const st = oopifState(tabId);
    const sid = params && params.sessionId;
    if (sid) {
      const tid = st.sessionToTarget.get(sid);
      st.sessionToTarget.delete(sid);
      st.sessions.delete(sid);
      if (tid) { const set = st.targetSessions.get(tid); if (set) set.delete(sid); }
    }
  }
});

// 收到 attachedToTarget：首先对子 sessionId 再 armAutoAttach（覆盖嵌套 OOPIF）→ 维护 4-dict。
async function handleTargetAttached(tabId, params) {
  const info = params.targetInfo || {};
  const targetId = info.targetId;
  const sessionId = params.sessionId;
  if (!targetId || !sessionId) return;
  // ★ 递归 re-arm：用子 session 再开 autoAttach（session_manager.py:402）。
  try {
    await chrome.debugger.sendCommand({ tabId, sessionId }, 'Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
  } catch { /* -32001 Session not found：短命 target 先 detach 了，正常 */ }
  const st = oopifState(tabId);
  st.targets.set(targetId, { targetId, type: info.type, url: info.url || '' });
  st.sessions.set(sessionId, { targetId });
  if (!st.targetSessions.has(targetId)) st.targetSessions.set(targetId, new Set());
  st.targetSessions.get(targetId).add(sessionId);
  st.sessionToTarget.set(sessionId, targetId);
}

async function debuggerDetach(tabId) {
  if (!_debuggerAttached.has(tabId)) return;
  await chrome.debugger.detach({ tabId }).catch(() => {});
  _debuggerAttached.delete(tabId);
  // 会话结束：清持久化状态，避免 storage.session 无限累积（tab 关闭/新任务不复用旧编号）。
  _sessionEpoch.delete(tabId);
  _oopif.delete(tabId);
  await saveTabState(STATE_KEYS.attach, tabId, null);
  await saveTabState(STATE_KEYS.indexMap, tabId, null);
  await saveTabState(STATE_KEYS.oopif, tabId, null);
}

// 真实鼠标移动，触发 hover 浮层（CSS :hover 和 JS mouseenter 都生效）
// 导航后移鼠标到中性位收残留浮层（sidepanel runAgentTask 用）；元素级 hover 走 AGENT_EXECUTE。
async function handleDebuggerHover(tabId, x, y) {
  await debuggerEnsureAttached(tabId);
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
    type: 'mouseMoved', x, y
  });
}


// ═══════════════════════════════════════════════════════════════════════════
// CDP 观察/执行层基座（Phase 0，对齐 browser-use 0.13.8）
// browser-use 是常驻 Python 单进程直连 CDP；本项目是会被杀的 MV3 service worker，
// 故用「Port keepalive 保命 + storage.session 持久化 + fresh-session 强制 stale」三层兜底。
// ═══════════════════════════════════════════════════════════════════════════

// 超时值表：复现 browser-use 各事件的墙钟超时（它用 bubus event_timeout，本项目用 Promise.race）。
// 单位 ms。来源：browser_use/browser/events.py _get_timeout 默认值。
const CDP_TIMEOUTS = {
  observe: 30000,        // BrowserStateRequestEvent
  click: 15000,          // ClickElementEvent / ClickCoordinateEvent
  type: 60000,           // TypeTextEvent
  navigate: 30000,       // NavigateToUrlEvent
  scroll: 8000,          // ScrollEvent
  switchTab: 10000,      // SwitchTabEvent
  mousePressed: 3000,    // 三连派发内部 wait_for
  mouseReleased: 5000,   // 三连派发内部 wait_for
  refocus: 3000,         // finally 重聚焦
  pendingNetwork: 2000,  // 观察前 pending network 检查
  gatherDeadline: 10000, // 三源并行首轮 deadline
  gatherRetry: 2000,     // 三源重试轮 deadline
};

// captureSnapshot 只取这 10 个 computedStyle（全量会让重站点 Chrome 崩）。
// ★ 顺序即解析键序：layout.styles 的字符串按此下标映射回键名，不可乱。
// 来源：browser_use/dom/enhanced_snapshot.py REQUIRED_COMPUTED_STYLES。
const REQUIRED_COMPUTED_STYLES = [
  'display', 'visibility', 'opacity', 'overflow', 'overflow-x', 'overflow-y',
  'cursor', 'pointer-events', 'position', 'background-color',
];

// ── CDP 命令门面 ─────────────────────────────────────────────────────────────
// target = {tabId} 走根会话；{tabId, sessionId} 路由到子 protocol session（OOPIF flatten）。
// chrome.debugger.sendCommand 官方支持 sessionId（Chromium debugger.json: DebuggerSession.sessionId）。
function cdpSend(target, method, params, timeoutMs) {
  const cmdPromise = new Promise((resolve, reject) => {
    chrome.debugger.sendCommand(target, method, params || {}, (result) => {
      const err = chrome.runtime.lastError;
      if (err) { reject(new Error(`${method}: ${err.message}`)); return; }
      resolve(result);
    });
  });
  if (!timeoutMs) return cmdPromise;
  return withTimeout(cmdPromise, timeoutMs, method);
}

// Promise 墙钟超时：超时 reject（调用方决定降级/吞掉），复现 browser-use asyncio.wait_for 语义。
function withTimeout(promise, ms, label) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label || 'cdp'} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => { if (timer) clearTimeout(timer); });
}

// ── SW 生命周期：跨步骤状态存 storage.session（SW 重启不丢，浏览器重启才清）────────────
// 权威源是 storage.session；_debuggerAttached / _sessionEpoch 只是内存快照缓存。
const STATE_KEYS = { attach: 'agentAttach', indexMap: 'agentIndexMap', oopif: 'agentOopif' };
const _sessionEpoch = new Map();   // tabId -> epoch（内存缓存；权威在 storage.session.agentAttach）

async function loadTabState(key, tabId) {
  try {
    const all = await chrome.storage.session.get([key]);
    return (all[key] || {})[tabId] || null;
  } catch { return null; }
}

async function saveTabState(key, tabId, value) {
  try {
    const all = await chrome.storage.session.get([key]);
    const map = all[key] || {};
    if (value === null) delete map[tabId]; else map[tabId] = value;
    await chrome.storage.session.set({ [key]: map });
  } catch { /* storage 不可用则退化为纯内存，SW 存活期间仍可用 */ }
}

// attach 成功即递增 sessionEpoch：新 CDP session 里旧 backendNodeId 语义可能失效，
// epoch 变化让 execute 侧检测到 → 返回 stale → 复用现有 stale 分支重新观察。
async function bumpSessionEpoch(tabId) {
  const prev = await loadTabState(STATE_KEYS.attach, tabId);
  const epoch = ((prev && prev.sessionEpoch) || 0) + 1;
  _sessionEpoch.set(tabId, epoch);
  await saveTabState(STATE_KEYS.attach, tabId, { attached: true, sessionEpoch: epoch });
  return epoch;
}

async function getSessionEpoch(tabId) {
  if (_sessionEpoch.has(tabId)) return _sessionEpoch.get(tabId);
  const st = await loadTabState(STATE_KEYS.attach, tabId);
  const epoch = (st && st.sessionEpoch) || 0;
  _sessionEpoch.set(tabId, epoch);
  return epoch;
}

// ── Port keepalive（主防线）：活跃 Port 在则 SW 不被 terminate，覆盖单步 settle+LLM 长间隙 ──
const _keepalivePorts = new Set();

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'agent-keepalive') return;
  _keepalivePorts.add(port);
  startKeepaliveAlarm();               // 第二保命：Port 在期间保持 alarm 空转
  port.onDisconnect.addListener(() => {
    _keepalivePorts.delete(port);
    if (chrome.runtime.lastError) { /* 端口异常断开，忽略 */ }
    if (_keepalivePorts.size === 0) { try { chrome.alarms.clear('agent-keepalive-tick'); } catch { /* noop */ } }
  });
});

// alarms 空转（第二保命）：仅在有活跃 keepalive 时注册，25s < SW 30s 空闲阈值。
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'agent-keepalive-tick') { /* 唤醒 SW，无需动作 */ }
});

function startKeepaliveAlarm() {
  try { chrome.alarms.create('agent-keepalive-tick', { periodInMinutes: 25 / 60 }); } catch { /* noop */ }
}

// ═══════════════════════════════════════════════════════════════════════════
// Phase 1 — CDP 三源观察 + 融合（对齐 browser-use 0.13.8 dom/service.py）
// Phase 1 只做 DOM + DOMSnapshot 两源（AX 树留 Phase 3）；同源页面（getDocument pierce
// 已带出同源 iframe + open shadow），跨源 OOPIF 留 Phase 4。
// ═══════════════════════════════════════════════════════════════════════════

// DOM NodeType（views.py NodeType 枚举）
const NODE_TYPE = { ELEMENT: 1, TEXT: 3, DOCUMENT: 9, DOCTYPE: 10, FRAGMENT: 11 };

// CDP rare-boolean 稀疏编码 → Set：captureSnapshot 的 isClickable 等只存 True 的下标数组。
// ★ 陷阱①：必须转 Set 做 O(1) 查，否则 20k 元素 O(n²) 5925ms→2ms（enhanced_snapshot.py）。
function rareBooleanSet(rareData) {
  const s = new Set();
  const idx = (rareData && rareData.index) || [];
  for (const i of idx) s.add(i);
  return s;
}

// 扁平属性数组 [k1,v1,k2,v2,...] → 对象。★ 陷阱③（service.py attributes 解析）。
function parseFlatAttributes(arr) {
  const out = {};
  if (!Array.isArray(arr)) return out;
  for (let i = 0; i + 1 < arr.length; i += 2) out[arr[i]] = arr[i + 1];
  return out;
}

// 三源拉取：captureSnapshot + getDocument（Phase 1 两源）+ getLayoutMetrics(DPR)。
// 并行 + 首轮 deadline 10s、失败重试一轮 2s（对齐 service.py _get_all_trees asyncio.wait）。
async function cdpGatherTrees(target) {
  const mkSnapshot = () => cdpSend(target, 'DOMSnapshot.captureSnapshot', {
    computedStyles: REQUIRED_COMPUTED_STYLES,
    includePaintOrder: true,
    includeDOMRects: true,
    includeBlendedBackgroundColors: false,
    includeTextColorOpacities: false,
  });
  const mkDom = () => cdpSend(target, 'DOM.getDocument', { depth: -1, pierce: true });
  const mkMetrics = () => cdpSend(target, 'Page.getLayoutMetrics', {});

  // 先并行发（首轮），deadline 由 withTimeout 施加在整体上。
  async function runRound(deadlineMs) {
    const [snap, dom, metrics] = await Promise.allSettled([
      withTimeout(mkSnapshot(), deadlineMs, 'captureSnapshot'),
      withTimeout(mkDom(), deadlineMs, 'getDocument'),
      withTimeout(mkMetrics(), deadlineMs, 'getLayoutMetrics'),
    ]);
    return { snap, dom, metrics };
  }

  let { snap, dom, metrics } = await runRound(CDP_TIMEOUTS.gatherDeadline);
  // 重试一轮（只重试失败的）：snapshot/dom 是硬依赖，失败必须重试。
  if (snap.status !== 'fulfilled') snap = await settleOne(withTimeout(mkSnapshot(), CDP_TIMEOUTS.gatherRetry, 'captureSnapshot'));
  if (dom.status !== 'fulfilled') dom = await settleOne(withTimeout(mkDom(), CDP_TIMEOUTS.gatherRetry, 'getDocument'));
  if (metrics.status !== 'fulfilled') metrics = await settleOne(withTimeout(mkMetrics(), CDP_TIMEOUTS.gatherRetry, 'getLayoutMetrics'));

  if (snap.status !== 'fulfilled') throw new Error(`captureSnapshot failed: ${snap.reason?.message || snap.reason}`);
  if (dom.status !== 'fulfilled') throw new Error(`getDocument failed: ${dom.reason?.message || dom.reason}`);

  // DPR = device_width / css_width（service.py _get_viewport_ratio），失败回退 1.0。
  let dpr = 1.0;
  if (metrics.status === 'fulfilled') {
    const m = metrics.value || {};
    const dev = m.visualViewport && (m.visualViewport.clientWidth);
    const css = m.cssVisualViewport && (m.cssVisualViewport.clientWidth);
    if (css && dev && css > 0) dpr = dev / css;
  }
  return { snapshot: snap.value, domTree: dom.value, devicePixelRatio: dpr };
}

function settleOne(p) {
  return p.then((v) => ({ status: 'fulfilled', value: v }),
                (e) => ({ status: 'rejected', reason: e }));
}

// 建 snapshot lookup：{ backendNodeId -> { bounds(÷DPR), clientRects, scrollRects,
//   computedStyles, paintOrder, isClickable } }（enhanced_snapshot.py build_snapshot_lookup）。
function buildSnapshotLookup(snapshot, dpr) {
  const lookup = new Map();
  const documents = (snapshot && snapshot.documents) || [];
  const strings = (snapshot && snapshot.strings) || [];
  for (const doc of documents) {
    const nodes = doc.nodes || {};
    const layout = doc.layout || {};
    const backendIds = nodes.backendNodeId || [];
    // backendNodeId → snapshot node 下标
    const backendToSnapIdx = new Map();
    for (let i = 0; i < backendIds.length; i++) backendToSnapIdx.set(backendIds[i], i);
    // layout.nodeIndex（layout 项 → node 下标）反向：node 下标 → layout 下标（重复只留第一个）。
    const layoutIndexMap = new Map();
    const layoutNodeIndex = layout.nodeIndex || [];
    for (let li = 0; li < layoutNodeIndex.length; li++) {
      const ni = layoutNodeIndex[li];
      if (!layoutIndexMap.has(ni)) layoutIndexMap.set(ni, li);
    }
    // ★ 陷阱①：isClickable 稀疏 → Set。
    const clickableSet = rareBooleanSet(nodes.isClickable);
    const bounds = layout.bounds || [];
    const stylesArr = layout.styles || [];
    const paintOrders = layout.paintOrders || [];
    const clientRects = layout.clientRects || [];
    const scrollRects = layout.scrollRects || [];

    for (const [backendId, snapIdx] of backendToSnapIdx.entries()) {
      const isClickable = clickableSet.has(snapIdx);
      const li = layoutIndexMap.get(snapIdx);
      const entry = { isClickable, bounds: null, clientRects: null, scrollRects: null,
                      computedStyles: {}, paintOrder: null };
      if (li !== undefined) {
        // ★ 陷阱②：bounds ÷ DPR 转 CSS；client/scroll rects 不除。
        const b = bounds[li];
        if (Array.isArray(b) && b.length >= 4) {
          entry.bounds = { x: b[0] / dpr, y: b[1] / dpr, width: b[2] / dpr, height: b[3] / dpr };
        }
        const cr = clientRects[li];
        if (Array.isArray(cr) && cr.length >= 4) entry.clientRects = { x: cr[0], y: cr[1], width: cr[2], height: cr[3] };
        const sr = scrollRects[li];
        if (Array.isArray(sr) && sr.length >= 4) entry.scrollRects = { x: sr[0], y: sr[1], width: sr[2], height: sr[3] };
        // ★ 陷阱④：layout.styles[li] 是字符串索引数组，按 REQUIRED_COMPUTED_STYLES 顺序映射。
        const st = stylesArr[li];
        if (Array.isArray(st)) {
          for (let k = 0; k < REQUIRED_COMPUTED_STYLES.length && k < st.length; k++) {
            const sIdx = st[k];
            entry.computedStyles[REQUIRED_COMPUTED_STYLES[k]] = (sIdx >= 0 && sIdx < strings.length) ? strings[sIdx] : '';
          }
        }
        if (Array.isArray(paintOrders) && li < paintOrders.length) entry.paintOrder = paintOrders[li];
      }
      lookup.set(backendId, entry);
    }
  }
  return lookup;
}

// 递归构建增强树。memoize 键=nodeId、join 键=backendNodeId；absolutePosition=bounds+totalFrameOffset。
// 每个节点带 sessionId/targetId（OOPIF 定位）；跨源 iframe 收集到 pendingCrossOrigin 交外层递归。
function constructEnhancedTree(domRoot, snapshotLookup, opts) {
  opts = opts || {};
  const viewport = opts.viewport || null;
  const jsClickIds = opts.jsClickIds || new Set();
  const axByBackend = opts.axByBackend || null;    // Phase 3 传入
  const ctxSessionId = opts.sessionId || null;     // Phase 4：本树所属 session
  const ctxTargetId = opts.targetId || null;
  const initialOffset = opts.initialOffset || { x: 0, y: 0 };
  const nodeByNodeId = new Map();          // memoize：nodeId → DomNode
  const allNodes = [];                     // 扁平列表，供 serialize 遍历
  const pendingCrossOrigin = [];           // 跨源 iframe 待处理：{ hostNode, frameId, offset }

  function build(cdpNode, totalFrameOffset) {
    if (cdpNode == null) return null;
    if (nodeByNodeId.has(cdpNode.nodeId)) return nodeByNodeId.get(cdpNode.nodeId);

    const attributes = parseFlatAttributes(cdpNode.attributes);
    const snap = snapshotLookup.get(cdpNode.backendNodeId) || null;
    let absolutePosition = null;
    if (snap && snap.bounds) {
      absolutePosition = {
        x: snap.bounds.x + totalFrameOffset.x,
        y: snap.bounds.y + totalFrameOffset.y,
        width: snap.bounds.width, height: snap.bounds.height,
      };
    }
    const node = {
      nodeId: cdpNode.nodeId,
      backendNodeId: cdpNode.backendNodeId,
      nodeType: cdpNode.nodeType,
      nodeName: cdpNode.nodeName || '',
      nodeValue: cdpNode.nodeValue || '',
      attributes,
      frameId: cdpNode.frameId || null,
      snapshot: snap,
      ax: axByBackend ? (axByBackend.get(cdpNode.backendNodeId) || null) : null,
      absolutePosition,
      isVisible: false,
      isInteractive: false,
      hasJsClickListener: jsClickIds.has(cdpNode.backendNodeId),
      ignoredByPaintOrder: false,          // Phase 2 接 paint order
      parent: null,
      children: [],
      selectorIndex: null,
      _viewport: viewport,
      sessionId: ctxSessionId,             // Phase 4：本节点所属 CDP session（OOPIF 定位用）
      targetId: ctxTargetId,
    };
    nodeByNodeId.set(cdpNode.nodeId, node);
    allNodes.push(node);

    // 可见性 + 可交互（依赖 hasJsClickListener / ax / _viewport，均已在上方设好）。
    node.isVisible = isVisibleCss(node);
    node.isInteractive = isInteractive(node);

    // 帧偏移累加（service.py:878）：进入本节点的子树时的偏移。
    // HTML frame（有 frameId）→ 减 scrollRects；IFRAME/FRAME（有 bounds）→ 加 bounds。
    let childOffset = totalFrameOffset;
    const nm = (cdpNode.nodeName || '').toUpperCase();
    if (nm === 'HTML' && cdpNode.frameId && snap && snap.scrollRects) {
      childOffset = { x: totalFrameOffset.x - snap.scrollRects.x, y: totalFrameOffset.y - snap.scrollRects.y };
    } else if ((nm === 'IFRAME' || nm === 'FRAME') && snap && snap.bounds) {
      childOffset = { x: totalFrameOffset.x + snap.bounds.x, y: totalFrameOffset.y + snap.bounds.y };
    }

    // 跨源 iframe：无 contentDocument（同进程拿不到内容）→ 记为待处理，交 handleAgentObserve
    // 用子 target 递归。frameId + 累积偏移随记录。
    if ((nm === 'IFRAME' || nm === 'FRAME') && !cdpNode.contentDocument && cdpNode.frameId
        && snap && snap.bounds && snap.bounds.width >= 10 && snap.bounds.height >= 10) {
      pendingCrossOrigin.push({ hostNode: node, frameId: cdpNode.frameId, offset: childOffset });
    }

    // 递归子节点：children + contentDocument（同源 iframe）+ shadowRoots（open）。
    const kids = [];
    if (Array.isArray(cdpNode.children)) kids.push(...cdpNode.children);
    if (cdpNode.contentDocument) kids.push(cdpNode.contentDocument);
    if (Array.isArray(cdpNode.shadowRoots)) kids.push(...cdpNode.shadowRoots);
    for (const child of kids) {
      const childNode = build(child, childOffset);
      if (childNode) { childNode.parent = node; node.children.push(childNode); }
    }
    return node;
  }

  const root = build(domRoot, initialOffset);
  return { root, allNodes, pendingCrossOrigin };
}

// getEventListeners 检测（service.py:461）：Runtime.evaluate(includeCommandLineAPI) 找挂了
// click/mousedown/mouseup/pointerdown/pointerup 的元素 → getProperties 取 objectId →
// describeNode 分批拿 backendNodeId → releaseObject。返回 Set<backendNodeId>。
async function detectClickListeners(target) {
  const ids = new Set();
  try {
    const expr = `(() => {
      if (typeof getEventListeners !== 'function') return null;
      const all = document.querySelectorAll('*');
      if (all.length > 10000) return null;
      const hit = [];
      for (const el of all) {
        try {
          const l = getEventListeners(el);
          if (l.click || l.mousedown || l.mouseup || l.pointerdown || l.pointerup) {
            hit.push(el);
            if (hit.length > 100) return '__browser_use_too_many_click_listeners__';
          }
        } catch (e) {}
      }
      return hit;
    })()`;
    const res = await cdpSend(target, 'Runtime.evaluate', { expression: expr, includeCommandLineAPI: true, returnByValue: false });
    const obj = res && res.result;
    if (!obj || obj.type === 'null' || obj.subtype === 'null') return ids;
    if (obj.value === '__browser_use_too_many_click_listeners__') return ids;  // 溢出：不解析
    const objectId = obj.objectId;
    if (!objectId) return ids;
    const props = await cdpSend(target, 'Runtime.getProperties', { objectId, ownProperties: true });
    const elObjectIds = [];
    for (const p of (props.result || [])) {
      if (/^\d+$/.test(p.name) && p.value && p.value.objectId) elObjectIds.push(p.value.objectId);
    }
    // 分批 20 并发 describeNode。
    for (let i = 0; i < elObjectIds.length; i += 20) {
      const batch = elObjectIds.slice(i, i + 20);
      const results = await Promise.all(batch.map(oid =>
        cdpSend(target, 'DOM.describeNode', { objectId: oid }).then(r => r && r.node && r.node.backendNodeId).catch(() => null)));
      for (const bid of results) if (bid != null) ids.add(bid);
    }
    await cdpSend(target, 'Runtime.releaseObject', { objectId }).catch(() => {});
  } catch { /* getEventListeners 不可用/异常：降级为空集 */ }
  return ids;
}

// paint order 遮挡过滤（paint_order.py:165）：按 paint_order 降序分组，同一区域下层被上层遮挡则标 ignored。
// 简化实现：对可交互可见节点，按 absolutePosition 中心点检查是否被更高 paintOrder 的元素覆盖。
function applyPaintOrderFilter(allNodes) {
  const candidates = allNodes.filter(n => n.isInteractive && n.isVisible && n.snapshot && n.snapshot.paintOrder != null && n.absolutePosition);
  // 按 paintOrder 降序：高 paintOrder 在上层。
  candidates.sort((a, b) => b.snapshot.paintOrder - a.snapshot.paintOrder);
  const painted = [];  // 已确认在上层的矩形
  for (const n of candidates) {
    const r = n.absolutePosition;
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    // 中心点落在某个更上层矩形内（且那个不是自己的祖先/后代）→ 被遮挡。
    let occluded = false;
    for (const p of painted) {
      const pr = p.absolutePosition;
      if (cx >= pr.x && cx <= pr.x + pr.width && cy >= pr.y && cy <= pr.y + pr.height) {
        if (!isAncestorOrSelf(p, n) && !isAncestorOrSelf(n, p)) { occluded = true; break; }
      }
    }
    if (occluded) n.ignoredByPaintOrder = true;
    else painted.push(n);
  }
}

function isAncestorOrSelf(a, b) {
  let cur = b;
  while (cur) { if (cur === a) return true; cur = cur.parent; }
  return false;
}

// ── AX 树（Phase 3，service.py _get_ax_tree_for_all_frames）──
// Page.getFrameTree 收集所有 frameId，逐 frame getFullAXTree{frameId} 合并；
// 根 frame 失败抛，子 frame 失败跳过；整体失败降级空（不致命）。
async function axTreeForAllFrames(target) {
  try {
    const ft = await cdpSend(target, 'Page.getFrameTree', {});
    const frameIds = [];
    (function collect(node) {
      if (!node) return;
      const fid = node.frame && node.frame.id;
      if (fid) frameIds.push(fid);
      for (const c of (node.childFrames || [])) collect(c);
    })(ft && ft.frameTree);
    const results = await Promise.all(frameIds.map((fid, i) =>
      cdpSend(target, 'Accessibility.getFullAXTree', { frameId: fid })
        .then(r => r && r.nodes ? r.nodes : [])
        .catch((e) => { if (i === 0) throw e; return []; })));
    const nodes = [];
    for (const arr of results) nodes.push(...arr);
    return nodes;
  } catch { return []; }   // 整体失败降级空集
}

// AX lookup：{ backendDOMNodeId -> { role, name, properties{} } }。
function buildAxLookup(axNodes) {
  const map = new Map();
  for (const ax of (axNodes || [])) {
    if (ax.backendDOMNodeId === undefined) continue;
    const props = {};
    for (const p of (ax.properties || [])) {
      const val = p.value && p.value.value;
      props[p.name] = val;
    }
    map.set(ax.backendDOMNodeId, {
      role: (ax.role && ax.role.value) || '',
      name: (ax.name && ax.name.value) || '',
      properties: props,
      ignored: !!ax.ignored,
    });
  }
  return map;
}

// ── 可交互判定常量（clickable_elements.py，逐字抄）──
const INTERACTIVE_TAGS = new Set(['button', 'input', 'select', 'textarea', 'a', 'details', 'summary', 'option', 'optgroup']);
const INTERACTIVE_ATTRS = new Set(['onclick', 'onmousedown', 'onmouseup', 'onkeydown', 'onkeyup', 'tabindex']);
const INTERACTIVE_ROLES = new Set(['button', 'link', 'menuitem', 'option', 'radio', 'checkbox', 'tab', 'textbox',
  'combobox', 'slider', 'spinbutton', 'search', 'searchbox', 'row', 'cell', 'gridcell']);
// AX role 集比 html role 多 listbox。
const INTERACTIVE_AX_ROLES = new Set(['button', 'link', 'menuitem', 'option', 'radio', 'checkbox', 'tab', 'textbox',
  'combobox', 'slider', 'spinbutton', 'listbox', 'search', 'searchbox', 'row', 'cell', 'gridcell']);
const SEARCH_INDICATORS = ['search', 'magnify', 'glass', 'lookup', 'find', 'query', 'search-icon', 'search-btn', 'search-button', 'searchbox'];
const FORM_CONTROL_TAGS = new Set(['input', 'select', 'textarea']);
const VIEWPORT_THRESHOLD = 1000;   // 视口上下各放宽 1000px 缓冲（service.py）。

// 递归 maxDepth 层，子树里有 input/select/textarea → true。
function hasFormControlDescendant(node, maxDepth) {
  if (maxDepth <= 0) return false;
  for (const c of node.children) {
    if (c.nodeType === NODE_TYPE.ELEMENT && FORM_CONTROL_TAGS.has((c.nodeName || '').toLowerCase())) return true;
    if (hasFormControlDescendant(c, maxDepth - 1)) return true;
  }
  return false;
}

// 可见性（is_element_visible_according_to_all_parents 简化：Phase 2 同文档视口求交；
// 跨 frame 偏移累减留 Phase 3）。CSS 检查 + clientRects 视口相交（上下放宽 1000px）。
function isVisibleCss(node) {
  const snap = node.snapshot;
  if (!snap) return false;
  const cs = snap.computedStyles || {};
  if (cs.display === 'none' || cs.visibility === 'hidden') return false;
  if (cs.opacity !== undefined && cs.opacity !== '' && parseFloat(cs.opacity) <= 0) return false;
  if (!snap.bounds) return false;
  if (snap.bounds.width <= 0 || snap.bounds.height <= 0) return false;
  // 视口求交（用 clientRects 视口坐标；无则回退 bounds 不做视口过滤）。
  const r = snap.clientRects;
  if (r && node._viewport) {
    const vw = node._viewport.width, vh = node._viewport.height;
    if (!(r.x < vw && r.x + r.width > 0 && r.y < vh + VIEWPORT_THRESHOLD && r.y + r.height > -VIEWPORT_THRESHOLD)) return false;
  }
  return true;
}

// 可交互判定（完整判定顺序 + 常量集，clickable_elements.py）。命中即返回。
function isInteractive(node) {
  if (node.nodeType !== NODE_TYPE.ELEMENT) return false;
  const tag = (node.nodeName || '').toLowerCase();
  if (tag === 'html' || tag === 'body') return false;
  if (node.hasJsClickListener) return true;
  // IFRAME/FRAME 且 >100×100
  if ((tag === 'iframe' || tag === 'frame') && node.snapshot && node.snapshot.bounds) {
    if (node.snapshot.bounds.width > 100 && node.snapshot.bounds.height > 100) return true;
  }
  const attrs = node.attributes || {};
  // label：有 for → F（避免双触发）；否则含表单控件后代 → T
  if (tag === 'label') {
    if (attrs.for !== undefined) return false;
    if (hasFormControlDescendant(node, 2)) return true;
  }
  // span 含表单控件后代 → T
  if (tag === 'span' && hasFormControlDescendant(node, 2)) return true;
  // search 指示词：class / id / 任意 data-* 值
  const cls = (attrs.class || '').toLowerCase();
  const id = (attrs.id || '').toLowerCase();
  let searchHit = SEARCH_INDICATORS.some(s => cls.includes(s) || id.includes(s));
  if (!searchHit) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith('data-') && SEARCH_INDICATORS.some(s => String(v).toLowerCase().includes(s))) { searchHit = true; break; }
    }
  }
  if (searchHit) return true;
  // AX properties：disabled/hidden → F；focusable/editable/settable/checked/expanded/pressed/selected/required/keyshortcuts → T
  const axp = (node.ax && node.ax.properties) || {};
  if (axp.disabled === true || axp.hidden === true) return false;
  for (const p of ['focusable', 'editable', 'settable', 'checked', 'expanded', 'pressed', 'selected', 'required', 'keyshortcuts']) {
    if (axp[p] === true || (axp[p] !== undefined && axp[p] !== false && p in axp)) return true;
  }
  if (INTERACTIVE_TAGS.has(tag)) return true;
  for (const a of Object.keys(attrs)) { if (INTERACTIVE_ATTRS.has(a)) return true; }
  if (attrs.contenteditable === 'true' || attrs.contenteditable === '') return true;
  if (attrs.role && INTERACTIVE_ROLES.has(attrs.role)) return true;
  if (node.ax && node.ax.role && INTERACTIVE_AX_ROLES.has(node.ax.role)) return true;
  // 图标小元素：10~50px 带 class/role/onclick/data-action/aria-label
  const b = node.snapshot && node.snapshot.bounds;
  if (b && b.width >= 10 && b.width <= 50 && b.height >= 10 && b.height <= 50) {
    if (attrs.class || attrs.role || attrs.onclick || attrs['data-action'] || attrs['aria-label']) return true;
  }
  // cursor:pointer 兜底
  if (node.snapshot && node.snapshot.computedStyles && node.snapshot.computedStyles.cursor === 'pointer') return true;
  return false;
}

// 取元素文本：AX name 优先（Phase 3），否则拼直接 TEXT 子节点。
function extractText(node) {
  if (node.ax && node.ax.name) return node.ax.name.replace(/\s+/g, ' ').trim().slice(0, 100);
  let t = '';
  for (const c of node.children) {
    if (c.nodeType === NODE_TYPE.TEXT && c.nodeValue) t += c.nodeValue;
  }
  return t.replace(/\s+/g, ' ').trim();
}

// 序列化：遍历 isInteractive && isVisible，分配 1..N selectorIndex，产出后端元素 dict + indexMap。
// element dict 字段对齐后端 context_builder._format_element 消费（全部 el.get(k,default) 容错）。
// indexMap: index -> { backendNodeId, sessionId, frameId, targetId } 供 execute 侧 resolveIndex 查表。
function serializeInteractive(allNodes, ctx) {
  const elements = [];
  const indexMap = {};
  let idx = 1;
  for (const node of allNodes) {
    if (!node.isInteractive || !node.isVisible) continue;
    if (node.ignoredByPaintOrder) continue;
    const attrs = node.attributes || {};
    const tag = (node.nodeName || '').toLowerCase();
    const text = extractText(node);
    const box = node.absolutePosition || (node.snapshot && node.snapshot.bounds) || null;
    const el = {
      id: idx,
      tag,
      type: attrs.type || '',
      role: attrs.role || (node.ax && node.ax.role) || '',
      name: attrs.name || '',
      placeholder: attrs.placeholder || '',
      value: tag === 'input' && attrs.type === 'password' ? '' : (attrs.value || ''),  // 剔 password
      text: text.slice(0, 100),
      aria_label: attrs['aria-label'] || '',
      component: attrs['data-component-name'] || '',
      enabled: !(node.ax && node.ax.properties && node.ax.properties.disabled),
      occluded: !!node.ignoredByPaintOrder,
      in_popup: false,                       // Phase 2/3 补弹层归属
      backend_node_id: node.backendNodeId,   // 附加键：后端忽略，前端/execute 用
      bounding_box: box ? { x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height) } : {},
    };
    elements.push(el);
    // OOPIF：用节点自己的 session/target（跨源子树节点带子 session），主 target 节点为 null。
    indexMap[idx] = {
      backendNodeId: node.backendNodeId,
      sessionId: node.sessionId || null,
      frameId: node.frameId || null,
      targetId: node.targetId || null,
    };
    idx++;
  }
  return { elements, indexMap };
}

// 页级只读探针：DOM 树不含的 page-level 字段（url/title/viewport/scroll/正文摘要/forms 等）。
// 纯只读、不打标（不违反"删元素采集注入"——删的是元素采集，页级只读是另一回事）。
// 返回结构对齐后端 PageState 的非 interactive_elements 字段。
const PAGE_EXTRAS_FN = `(() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const NAV_SEL = 'nav, header, aside, footer, [role="navigation"], [role="banner"],'
    + '[class*="sidebar"], [class*="navbar"], [class*="nav-bar"],'
    + '[class*="menu"]:not([class*="content"]), [class*="header"]:not([class*="content"]),'
    + '[class*="breadcrumb"], [class*="topbar"], [class*="footer"]';
  let textSummary = '';
  try {
    let main = document.querySelector('main, [role="main"], article, .main-content, [class*="main-content"]');
    if (main && clean(main.textContent).length >= 80) {
      textSummary = clean(main.textContent).slice(0, 3500);
    } else {
      const clone = document.body ? document.body.cloneNode(true) : null;
      if (clone) {
        clone.querySelectorAll(NAV_SEL + ', script, style, noscript').forEach(n => n.remove());
        const t = clean(clone.textContent);
        textSummary = (t.length >= 80 ? t : clean(document.body.textContent)).slice(0, 3500);
      }
    }
  } catch (e) { textSummary = ''; }
  let forms = [];
  try {
    forms = Array.from(document.forms).slice(0, 10).map(f => ({
      action: f.action, method: f.method,
      fields: Array.from(f.elements).map(e => e.name).filter(Boolean)
    }));
  } catch (e) { forms = []; }
  return {
    url: location.href,
    title: document.title,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    scroll_position: { x: Math.round(window.scrollX), y: Math.round(window.scrollY) },
    document_height: (document.documentElement && document.documentElement.scrollHeight) || 0,
    is_loading: document.readyState !== 'complete',
    focused_element: (document.activeElement && document.activeElement.id) ? ('#' + document.activeElement.id) : null,
    text_content_summary: textSummary,
    forms: forms,
    active_popup: (() => {
      // 检测可见弹出层（对齐旧注入 POPUP_TRUSTED_SELECTORS 的核心集）。
      const SEL = '[role="dialog"],[role="listbox"],[role="menu"],.ant-modal-content,.ant-dropdown,'
        + '.ant-picker-panel,.ant-picker-dropdown,.ant-select-dropdown,.ant-popover-inner,'
        + '.el-dialog,.el-dropdown-menu,.el-picker-panel,.el-select-dropdown,.el-popover,'
        + '.jmtd-dropdown-panel,.jmtd-popup,.jmtd-modal,.jmtd-date-picker-panel,.jmtd-select-dropdown';
      for (const el of document.querySelectorAll(SEL)) {
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const cls = el.className || '';
        let type = 'popup';
        if (/date-picker|picker-panel/.test(cls)) type = 'date_picker';
        else if (/select-dropdown|dropdown-list/.test(cls) || el.getAttribute('role') === 'listbox') type = 'dropdown';
        else if (/modal|dialog/.test(cls) || el.getAttribute('role') === 'dialog') type = 'modal';
        else if (/menu/.test(cls) || el.getAttribute('role') === 'menu') type = 'menu';
        const hdr = el.querySelector('.ant-picker-header,.el-date-picker__header,[class*="header-content"]');
        return { type, header_text: hdr ? hdr.textContent.trim().slice(0, 40) : '' };
      }
      return null;
    })(),
  };
})()`;

async function pageExtrasProbe(target) {
  try {
    const res = await cdpSend(target, 'Runtime.evaluate', {
      expression: PAGE_EXTRAS_FN, returnByValue: true,
    }, CDP_TIMEOUTS.observe);
    return (res && res.result && res.result.value) || {};
  } catch { return {}; }
}

// 对单个 target 拉三源 + getEventListeners + AX → 构建增强树（含跨源 iframe 待处理项）。
async function gatherAndConstructTarget(target, ctx, offset) {
  const trees = await cdpGatherTrees(target);
  const jsClickIds = await detectClickListeners(target);
  const axNodes = await axTreeForAllFrames(target);
  const axByBackend = buildAxLookup(axNodes);
  const metrics = await cdpSend(target, 'Page.getLayoutMetrics', {}).catch(() => null);
  const viewport = metrics && metrics.layoutViewport
    ? { width: metrics.layoutViewport.clientWidth, height: metrics.layoutViewport.clientHeight }
    : null;
  const snapshotLookup = buildSnapshotLookup(trees.snapshot, trees.devicePixelRatio);
  const built = constructEnhancedTree(trees.domTree.root, snapshotLookup, {
    viewport, jsClickIds, axByBackend,
    sessionId: ctx.sessionId, targetId: ctx.targetId, initialOffset: offset,
  });
  return { built, dpr: trees.devicePixelRatio, jsClickCount: jsClickIds.size };
}

// frameId → 子 target/session：遍历 OOPIF 已挂载的子 session，其 frameTree 根 frame id == frameId 即匹配。
async function resolveChildTarget(tabId, frameId) {
  const st = _oopif.get(tabId);
  if (!st) return null;
  for (const [sessionId, meta] of st.sessions.entries()) {
    try {
      const ft = await cdpSend({ tabId, sessionId }, 'Page.getFrameTree', {});
      const rootId = ft && ft.frameTree && ft.frameTree.frame && ft.frameTree.frame.id;
      if (rootId === frameId) return { sessionId, targetId: meta.targetId };
    } catch { /* 子 session 可能已 detach，跳过 */ }
  }
  return null;
}


const MAX_IFRAME_DEPTH = 5, MAX_IFRAMES = 100;
async function handleAgentObserve(tabId) {
  await debuggerEnsureAttached(tabId);
  const target = { tabId };
  const epoch = await getSessionEpoch(tabId);

  // 主 target。
  const { built, dpr, jsClickCount } = await gatherAndConstructTarget(target, { sessionId: null, targetId: null }, { x: 0, y: 0 });
  const allNodes = built.allNodes;
  let pending = built.pendingCrossOrigin;
  let iframeCount = 0;

  // 递归处理跨源 iframe：frameId → 子 target/session → 构子树 → 挂到宿主节点。
  const st = _oopif.get(tabId);
  for (let depth = 0; depth < MAX_IFRAME_DEPTH && pending.length && iframeCount < MAX_IFRAMES; depth++) {
    const nextPending = [];
    for (const pc of pending) {
      if (iframeCount >= MAX_IFRAMES) break;
      const child = await resolveChildTarget(tabId, pc.frameId);
      if (!child) continue;
      iframeCount++;
      try {
        const sub = await gatherAndConstructTarget(
          { tabId, sessionId: child.sessionId }, { sessionId: child.sessionId, targetId: child.targetId }, pc.offset);
        // 挂接：子树根挂到宿主 iframe 节点下。
        if (sub.built.root) { sub.built.root.parent = pc.hostNode; pc.hostNode.children.push(sub.built.root); }
        for (const n of sub.built.allNodes) allNodes.push(n);
        for (const np of sub.built.pendingCrossOrigin) nextPending.push(np);
      } catch { /* 子 frame 失败跳过（对齐 browser-use 子帧失败不致命）*/ }
    }
    pending = nextPending;
  }

  applyPaintOrderFilter(allNodes);
  const { elements, indexMap } = serializeInteractive(allNodes, { sessionId: null, targetId: null });
  const extras = await pageExtrasProbe(target);

  // indexMap 持久化（含 epoch）：SW 重启后 execute 侧比对 epoch，不符即 stale。
  await saveTabState(STATE_KEYS.indexMap, tabId, { epoch, map: indexMap });

  // 路径铁证：在 SW 控制台打印，确认走的是 CDP 观察（区分新旧路径）。
  console.log(`[CDP观察] elems=${elements.length} jsClick=${jsClickCount} iframes=${iframeCount} dpr=${dpr.toFixed(2)} url=${(extras.url || '').slice(0, 60)}`);

  const pageState = {
    url: extras.url || '',
    title: extras.title || '',
    viewport: extras.viewport || {},
    scroll_position: extras.scroll_position || {},
    document_height: extras.document_height || 0,
    is_loading: !!extras.is_loading,
    focused_element: extras.focused_element || null,
    interactive_elements: elements,
    element_count_truncated: false,
    text_content_summary: extras.text_content_summary || '',
    forms: extras.forms || [],
    device_pixel_ratio: dpr,
    active_popup: extras.active_popup || null,
  };
  return { pageState };
}

// ═══════════════════════════════════════════════════════════════════════════
// Phase 1 — 执行层（对齐 browser-use 0.13.8 default_action_watchdog.py）
// ═══════════════════════════════════════════════════════════════════════════

// index → { backendNodeId, sessionId, frameId, targetId }。epoch 不符 → stale（SW 重启/重连后旧编号失效）。
async function resolveIndex(tabId, index) {
  const st = await loadTabState(STATE_KEYS.indexMap, tabId);
  const epoch = await getSessionEpoch(tabId);
  if (!st || st.epoch !== epoch) return { stale: true };
  const entry = (st.map || {})[index];
  if (!entry) return { stale: true };       // 编号不在最新观察里 → 重新观察
  return { entry };
}

// 坐标降级链：getContentQuads → getBoxModel → resolveNode+getBoundingClientRect → JS click 兜底。
// 返回 { rect } 或 { objectId, jsClickOnly:true }（无几何,只能 JS 点）。session.py get_element_coordinates。
async function getElementCoordinates(target, backendNodeId) {
  // Method 1: getContentQuads
  try {
    const r = await cdpSend(target, 'DOM.getContentQuads', { backendNodeId });
    if (r && r.quads && r.quads.length) return { quads: r.quads };
  } catch { /* 下沉 */ }
  // Method 2: getBoxModel（model.content 8 数 = 4 点）
  try {
    const r = await cdpSend(target, 'DOM.getBoxModel', { backendNodeId });
    const c = r && r.model && r.model.content;
    if (Array.isArray(c) && c.length >= 8) return { quads: [c.slice(0, 8)] };
  } catch { /* 下沉 */ }
  // Method 3: resolveNode + getBoundingClientRect
  try {
    const rn = await cdpSend(target, 'DOM.resolveNode', { backendNodeId });
    const objectId = rn && rn.object && rn.object.objectId;
    if (objectId) {
      const js = await cdpSend(target, 'Runtime.callFunctionOn', {
        objectId,
        functionDeclaration: 'function(){const r=this.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};}',
        returnByValue: true,
      });
      const rect = js && js.result && js.result.value;
      if (rect && rect.width > 0 && rect.height > 0) {
        const q = [rect.x, rect.y, rect.x + rect.width, rect.y, rect.x + rect.width, rect.y + rect.height, rect.x, rect.y + rect.height];
        return { quads: [q] };
      }
      return { objectId, jsClickOnly: true };   // 有节点无几何 → 只能 JS 点
    }
  } catch { /* 下沉 */ }
  return null;
}

// 从 quads 选与视口交集面积最大者 → 中心点（4 点均值）→ 夹取视口内。daw.py:830。
function quadsToClickPoint(quads, vw, vh) {
  let best = null, bestArea = 0;
  for (const q of quads) {
    if (!q || q.length < 8) continue;
    const xs = [q[0], q[2], q[4], q[6]], ys = [q[1], q[3], q[5], q[7]];
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    if (maxX < 0 || maxY < 0 || minX > vw || minY > vh) continue;
    const area = (Math.min(vw, maxX) - Math.max(0, minX)) * (Math.min(vh, maxY) - Math.max(0, minY));
    if (area > bestArea) { bestArea = area; best = q; }
  }
  if (!best) best = quads[0];
  let cx = (best[0] + best[2] + best[4] + best[6]) / 4;
  let cy = (best[1] + best[3] + best[5] + best[7]) / 4;
  cx = Math.max(0, Math.min(vw - 1, cx));
  cy = Math.max(0, Math.min(vh - 1, cy));
  return { x: cx, y: cy };
}

async function jsClickBackend(target, backendNodeId) {
  const rn = await cdpSend(target, 'DOM.resolveNode', { backendNodeId });
  const objectId = rn && rn.object && rn.object.objectId;
  if (!objectId) throw new Error('resolveNode 无 objectId');
  await cdpSend(target, 'Runtime.callFunctionOn', {
    objectId, functionDeclaration: 'function(){ this.click(); }',
  });
}

// 真实点击三连：mouseMoved → mousePressed(wait3s) → mouseReleased(wait5s)。daw.py:903。
async function dispatchRealClick(target, x, y) {
  await cdpSend(target, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
  await sleep(50);
  try {
    await cdpSend(target, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 }, CDP_TIMEOUTS.mousePressed);
    await sleep(80);
  } catch { /* 超时不 sleep,继续 release */ }
  try {
    await cdpSend(target, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 }, CDP_TIMEOUTS.mouseReleased);
  } catch { /* 超时忽略 */ }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── VK 映射（actor/utils.py + daw.py _get_char_modifiers_and_vk / _get_key_code_for_char）──
// modifier 位掩码：Alt=1 Control=2 Meta=4 Shift=8。
const KEY_MODIFIERS = { alt: 1, control: 2, ctrl: 2, meta: 4, shift: 8 };
// 需 Shift 的符号 → [基础键, VK码]。
const SHIFT_CHARS = {
  '!': ['1', 49], '@': ['2', 50], '#': ['3', 51], '$': ['4', 52], '%': ['5', 53],
  '^': ['6', 54], '&': ['7', 55], '*': ['8', 56], '(': ['9', 57], ')': ['0', 48],
  '_': ['-', 189], '+': ['=', 187], '{': ['[', 219], '}': [']', 221], '|': ['\\', 220],
  ':': [';', 186], '"': ["'", 222], '<': [',', 188], '>': ['.', 190], '?': ['/', 191], '~': ['`', 192],
};
const NO_SHIFT_CHARS = { ' ': 32, '-': 189, '=': 187, '[': 219, ']': 221, '\\': 220, ';': 186, "'": 222, ',': 188, '.': 190, '/': 191, '`': 192 };
// 专用键 → [code, windowsVirtualKeyCode]（get_key_info 精简表，覆盖常用）。
const SPECIAL_KEYS = {
  Enter: ['Enter', 13], Tab: ['Tab', 9], Escape: ['Escape', 27], Backspace: ['Backspace', 8],
  Delete: ['Delete', 46], ArrowUp: ['ArrowUp', 38], ArrowDown: ['ArrowDown', 40],
  ArrowLeft: ['ArrowLeft', 37], ArrowRight: ['ArrowRight', 39], Home: ['Home', 36], End: ['End', 35],
  PageUp: ['PageUp', 33], PageDown: ['PageDown', 34], Space: ['Space', 32],
};

function charModifiersAndVk(ch) {
  if (SHIFT_CHARS[ch]) { const [base, vk] = SHIFT_CHARS[ch]; return { mod: 8, vk, base }; }
  if (ch >= 'A' && ch <= 'Z') return { mod: 8, vk: ch.charCodeAt(0), base: ch.toLowerCase() };
  if (ch >= 'a' && ch <= 'z') return { mod: 0, vk: ch.toUpperCase().charCodeAt(0), base: ch };
  if (ch >= '0' && ch <= '9') return { mod: 0, vk: ch.charCodeAt(0), base: ch };
  if (NO_SHIFT_CHARS[ch] !== undefined) return { mod: 0, vk: NO_SHIFT_CHARS[ch], base: ch };
  return { mod: 0, vk: ch.length === 1 ? ch.charCodeAt(0) : 0, base: ch };
}

function keyCodeForChar(base) {
  if (base >= 'a' && base <= 'z') return 'Key' + base.toUpperCase();
  if (base >= 'A' && base <= 'Z') return 'Key' + base;
  if (base >= '0' && base <= '9') return 'Digit' + base;
  const map = { ' ': 'Space', '.': 'Period', ',': 'Comma', '-': 'Minus', '=': 'Equal',
    ';': 'Semicolon', "'": 'Quote', '/': 'Slash', '\\': 'Backslash', '[': 'BracketLeft',
    ']': 'BracketRight', '`': 'Backquote' };
  return map[base] || '';
}

// 派发一个专用键（Enter/Escape/Arrow...），含 modifiers 位掩码。
async function dispatchSpecialKey(target, key, modifiers) {
  const [code, vk] = SPECIAL_KEYS[key] || [key, 0];
  let mod = 0;
  for (const m of (modifiers || [])) mod |= (KEY_MODIFIERS[String(m).toLowerCase()] || 0);
  const base = { key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: mod };
  await cdpSend(target, 'Input.dispatchKeyEvent', { type: 'keyDown', ...base });
  if (key === 'Enter') await cdpSend(target, 'Input.dispatchKeyEvent', { type: 'char', text: '\r', key, modifiers: mod });
  await cdpSend(target, 'Input.dispatchKeyEvent', { type: 'keyUp', ...base });
}

// 逐字符输入（三段式 keyDown 无 text → char 有 text → keyUp 无 text，含 VK 映射）。daw.py:1874。
async function typeChars(target, text) {
  for (const ch of text) {
    if (ch === '\n') { await dispatchSpecialKey(target, 'Enter', []); await sleep(1); continue; }
    const { mod, vk, base } = charModifiersAndVk(ch);
    const code = keyCodeForChar(base);
    await cdpSend(target, 'Input.dispatchKeyEvent', { type: 'keyDown', key: base, code, modifiers: mod, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await sleep(5);
    await cdpSend(target, 'Input.dispatchKeyEvent', { type: 'char', text: ch, key: ch });
    await cdpSend(target, 'Input.dispatchKeyEvent', { type: 'keyUp', key: base, code, modifiers: mod, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await sleep(1);
  }
}

// 遮挡检测（daw.py:573）：elementFromPoint 命中目标本身/后代/祖先 + label/input 三关联救援。
// 拿不到→视为遮挡（走 JS click）；异常→视为不遮挡（继续真实点击）。
async function checkOcclusion(target, backendNodeId, x, y) {
  try {
    const rn = await cdpSend(target, 'DOM.resolveNode', { backendNodeId });
    const objectId = rn && rn.object && rn.object.objectId;
    if (!objectId) return true;   // 拿不到 → 视为遮挡
    const fn = `function(){
      const at=document.elementFromPoint(arguments[0],arguments[1]);
      if(!at) return {ok:false,noPoint:true};
      let ok = this===at || this.contains(at) || at.contains(this);
      if(!ok){
        const t=this;
        if(t.tagName==='INPUT'&&t.id){const l=document.querySelector('label[for="'+CSS.escape(t.id)+'"]');if(l&&(l===at||l.contains(at)))ok=true;}
        if(!ok&&t.tagName==='INPUT'){let a=at;for(let i=0;i<3&&a;i++){if(a.tagName==='LABEL'&&a.contains(t)){ok=true;break;}a=a.parentElement;}}
        if(!ok&&t.tagName==='LABEL'){if(t.htmlFor&&at.tagName==='INPUT'&&at.id===t.htmlFor)ok=true;if(!ok&&at.tagName==='INPUT'&&t.contains(at))ok=true;}
      }
      return {ok:ok};
    }`;
    const res = await cdpSend(target, 'Runtime.callFunctionOn', {
      objectId, functionDeclaration: fn,
      arguments: [{ value: x }, { value: y }], returnByValue: true,
    });
    const v = res && res.result && res.result.value;
    if (!v) return true;
    if (v.noPoint) return true;   // 命中不到任何元素 → 视为遮挡
    return !v.ok;                 // ok=可点 → 不遮挡
  } catch { return false; }       // 异常 → 视为不遮挡，继续真实点击
}

// finally 重聚焦顶层（防点击开了新 tab/dialog 卡住）。daw.py:1026。
async function refocusTop(target) {
  try { await cdpSend(target, 'Page.bringToFront', {}, CDP_TIMEOUTS.refocus); } catch { /* noop */ }
}

// AGENT_EXECUTE：按 action.type 分发。Phase 2 完整支持 click/type/scroll/wait/press_key/clear/select/hover/focus/scroll_to_element。
// 返回 ActionResult 形状。__via:'cdp' 是路径铁证标记（区分新旧路径，验证用）。
async function handleAgentExecute(tabId, action) {
  await debuggerEnsureAttached(tabId);
  const rootTarget = { tabId };            // 鼠标/键盘派发用根 target（坐标是顶层视口）
  const type = action.type;

  // 需要元素的动作先解析 index → { backendNodeId, sessionId }（epoch 不符即 stale）。
  const noElActions = ['scroll', 'wait', 'navigate'];
  const optElActions = ['press_key'];   // press_key 可带 index 也可不带（对当前 focus）
  const needsEl = !noElActions.includes(type) && !optElActions.includes(type);
  let backendNodeId = null;
  let domTarget = { tabId };               // DOM 命令（resolveNode/focus/getContentQuads）用；OOPIF 节点走子 session
  if (needsEl || (optElActions.includes(type) && action.index != null)) {
    const r = await resolveIndex(tabId, action.index);
    if (needsEl && r.stale) return { __via: 'cdp', success: false, stale: true, action_type: type, error: `编号 ${action.index} 已失效（需重新观察）` };
    if (!r.stale) {
      backendNodeId = r.entry.backendNodeId;
      // OOPIF：跨源节点的 backendNodeId 只在其子 session 有效（cdp_client_for_node 4级定位）。
      if (r.entry.sessionId) domTarget = { tabId, sessionId: r.entry.sessionId };
    }
  }

  // 视口取根 target（顶层坐标系）。
  const metrics = await cdpSend(rootTarget, 'Page.getLayoutMetrics', {}).catch(() => null);
  const vw = (metrics && metrics.layoutViewport && metrics.layoutViewport.clientWidth) || 1920;
  const vh = (metrics && metrics.layoutViewport && metrics.layoutViewport.clientHeight) || 1080;

  try {
    // DOM 命令走 domTarget（OOPIF 子 session）；鼠标/键盘走 rootTarget（顶层坐标）。
    if (type === 'click') return { __via: 'cdp', ...(await doClick(domTarget, rootTarget, backendNodeId, action.index, vw, vh)) };
    if (type === 'type') return { __via: 'cdp', ...(await doType(domTarget, rootTarget, backendNodeId, action)) };
    if (type === 'clear') return { __via: 'cdp', ...(await doClear(domTarget, backendNodeId, action.index)) };
    if (type === 'select') return { __via: 'cdp', ...(await doSelect(domTarget, rootTarget, backendNodeId, action, vw, vh)) };
    if (type === 'hover') return { __via: 'cdp', ...(await doHover(domTarget, rootTarget, backendNodeId, vw, vh)) };
    if (type === 'focus') { await cdpSend(domTarget, 'DOM.focus', { backendNodeId }).catch(() => {}); return { __via: 'cdp', success: true, action_type: type, details: `聚焦[${action.index}]` }; }
    if (type === 'press_key') {
      const key = (action.params && action.params.key) || 'Enter';
      const mods = (action.params && action.params.modifiers) || [];
      if (backendNodeId != null) await cdpSend(domTarget, 'DOM.focus', { backendNodeId }).catch(() => {});
      await dispatchSpecialKey(rootTarget, key, mods);
      return { __via: 'cdp', success: true, action_type: type, details: `按下 ${key}` };
    }
    if (type === 'scroll_to_element') {
      await cdpSend(domTarget, 'DOM.scrollIntoViewIfNeeded', { backendNodeId }).catch(() => {});
      return { __via: 'cdp', success: true, action_type: type, details: `滚动到[${action.index}]` };
    }
    if (type === 'scroll') {
      const dir = (action.params && action.params.direction) || 'down';
      const amt = (action.params && action.params.amount) || 300;
      const dy = dir === 'up' ? -amt : amt;
      await cdpSend(rootTarget, 'Input.dispatchMouseEvent', { type: 'mouseWheel', x: Math.round(vw / 2), y: Math.round(vh / 2), deltaX: 0, deltaY: dy });
      return { __via: 'cdp', success: true, action_type: type, details: `滚动 ${dir} ${amt}` };
    }
    if (type === 'wait') {
      const ms = Math.min((action.params && action.params.ms) || 1000, 5000);
      await sleep(ms);
      return { __via: 'cdp', success: true, action_type: type, details: `等待 ${ms}ms` };
    }
    return { __via: 'cdp', success: false, action_type: type, error: `不支持的动作: ${type}` };
  } catch (e) {
    // 对齐 browser-use：动作失败转 ActionResult(error) 让 LLM 换招，不抛终止。
    return { __via: 'cdp', success: false, action_type: type, error: (e && e.message) || String(e) };
  } finally {
    await refocusTop(rootTarget);
  }
}

// 点击：滚动→取坐标→遮挡检测→(遮挡)JS click /(不遮挡)三连派发；checkbox 回读兜底。daw.py:702。
// DOM 命令走 domTarget（OOPIF 子 session），真实鼠标派发走 rootTarget（顶层坐标）。
async function doClick(domTarget, rootTarget, backendNodeId, index, vw, vh) {
  const target = domTarget;
  // checkbox/radio 预读 checked
  let checkboxObjId = null, preChecked = null;
  try {
    const rn = await cdpSend(target, 'DOM.resolveNode', { backendNodeId });
    const oid = rn && rn.object && rn.object.objectId;
    if (oid) {
      const info = await cdpSend(target, 'Runtime.callFunctionOn', {
        objectId: oid, functionDeclaration: 'function(){return (this.tagName==="INPUT"&&(this.type==="checkbox"||this.type==="radio"))?this.checked:null;}', returnByValue: true,
      });
      const v = info && info.result && info.result.value;
      if (v !== null && v !== undefined) { checkboxObjId = oid; preChecked = v; }
    }
  } catch { /* 非 toggle，忽略 */ }

  await cdpSend(target, 'DOM.scrollIntoViewIfNeeded', { backendNodeId }).catch(() => {});
  await sleep(50);
  const coords = await getElementCoordinates(target, backendNodeId);
  if (!coords) return { success: false, stale: true, action_type: 'click', error: '无法定位元素坐标' };
  if (coords.jsClickOnly) { await jsClickBackend(target, backendNodeId); return { success: true, action_type: 'click', details: `点击[${index}]（JS兜底）` }; }
  const pt = quadsToClickPoint(coords.quads, vw, vh);
  const occluded = await checkOcclusion(target, backendNodeId, pt.x, pt.y);
  if (occluded) {
    await jsClickBackend(target, backendNodeId);
    return { success: true, action_type: 'click', details: `点击[${index}]（遮挡,JS绕过）` };
  }
  await dispatchRealClick(rootTarget, pt.x, pt.y);   // 真实鼠标走根 target
  // checkbox 回读：状态没变 → JS click 兜底
  if (checkboxObjId && preChecked !== null) {
    await sleep(50);
    try {
      const post = await cdpSend(target, 'Runtime.callFunctionOn', { objectId: checkboxObjId, functionDeclaration: 'function(){return this.checked;}', returnByValue: true });
      if (post && post.result && post.result.value === preChecked) {
        await cdpSend(target, 'Runtime.callFunctionOn', { objectId: checkboxObjId, functionDeclaration: 'function(){this.click();}' });
      }
    } catch { /* noop */ }
  }
  return { success: true, action_type: 'click', details: `点击了[${index}]` };
}

// 输入:focus→(需直接赋值的类型)setter/(否则)clear+逐字符→回读。daw.py:1756。
// DOM 命令 + 键盘走 domTarget（focus 后按键留在该 frame）；rootTarget 备用。
async function doType(domTarget, rootTarget, backendNodeId, action) {
  const target = domTarget;
  const text = (action.params && action.params.text) || '';
  const doClear = action.params && action.params.clear !== false;
  await cdpSend(target, 'DOM.scrollIntoViewIfNeeded', { backendNodeId }).catch(() => {});
  await sleep(10);
  await cdpSend(target, 'DOM.focus', { backendNodeId }).catch(() => {});
  const rn = await cdpSend(target, 'DOM.resolveNode', { backendNodeId }).catch(() => null);
  const objectId = rn && rn.object && rn.object.objectId;

  // 日期/color/range/datepicker 类：直接赋值（原生 setter + 派发事件）。
  if (objectId) {
    const needDirect = await cdpSend(target, 'Runtime.callFunctionOn', {
      objectId, functionDeclaration: `function(){const t=(this.getAttribute('type')||'').toLowerCase();const dp=['date','time','datetime-local','month','week','color','range'];const cls=(this.className||'')+' '+(this.getAttribute('data-provide')||'');return this.tagName==='INPUT'&&(dp.includes(t)||/datepicker|data-date/i.test(cls));}`,
      returnByValue: true,
    }).then(r => r && r.result && r.result.value).catch(() => false);
    if (needDirect) {
      await cdpSend(target, 'Runtime.callFunctionOn', {
        objectId, arguments: [{ value: text }],
        functionDeclaration: `function(v){const p=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');p&&p.set&&p.set.call(this,v);this.dispatchEvent(new Event('focus',{bubbles:true}));this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));this.dispatchEvent(new Event('blur',{bubbles:true}));}`,
      });
      return { success: true, action_type: 'type', details: `直接赋值"${text.slice(0, 20)}"` };
    }
  }
  if (doClear) await clearField(target, backendNodeId, objectId);
  await typeChars(target, text);
  // 回读校验 + 拼接错误重设
  if (objectId) {
    await sleep(50);
    try {
      const rv = await cdpSend(target, 'Runtime.callFunctionOn', { objectId, functionDeclaration: 'function(){return this.value!==undefined?this.value:this.textContent;}', returnByValue: true });
      const actual = (rv && rv.result && rv.result.value) || '';
      if (doClear && actual !== text && actual.length > text.length && (actual.endsWith(text) || actual.startsWith(text))) {
        await cdpSend(target, 'Runtime.callFunctionOn', {
          objectId, arguments: [{ value: text }],
          functionDeclaration: `function(v){const P=this.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;const p=Object.getOwnPropertyDescriptor(P,'value');if(p&&p.set){p.set.call(this,v);}else{this.value=v;}this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));}`,
        });
      }
    } catch { /* noop */ }
  }
  return { success: true, action_type: 'type', details: `输入了"${text.slice(0, 20)}"` };
}

// 清空:JS 赋空(contenteditable removeChild / 普通 select+value="")→ 三击+Delete 兜底。daw.py:1344。
async function clearField(target, backendNodeId, objectId) {
  const oid = objectId || (await cdpSend(target, 'DOM.resolveNode', { backendNodeId }).then(r => r && r.object && r.object.objectId).catch(() => null));
  if (!oid) return;
  await cdpSend(target, 'Runtime.callFunctionOn', {
    objectId: oid,
    functionDeclaration: `function(){const ce=this.getAttribute('contenteditable');if(ce==='true'||ce===''||this.isContentEditable){while(this.firstChild)this.removeChild(this.firstChild);}else{try{this.select();}catch(e){}this.value='';}this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));}`,
  }).catch(() => {});
}

async function doClear(target, backendNodeId, index) {
  await clearField(target, backendNodeId, null);
  return { success: true, action_type: 'clear', details: `清空[${index}]` };
}

// 选择:原生<select>直接设value;自定义下拉→点触发器→在弹层找精确文本选项点击。
async function doSelect(domTarget, rootTarget, backendNodeId, action, vw, vh) {
  const target = domTarget;
  const optText = (action.params && action.params.option_text) || '';
  const rn = await cdpSend(target, 'DOM.resolveNode', { backendNodeId }).catch(() => null);
  const objectId = rn && rn.object && rn.object.objectId;
  if (objectId) {
    const isNative = await cdpSend(target, 'Runtime.callFunctionOn', { objectId, functionDeclaration: 'function(){return this.tagName==="SELECT";}', returnByValue: true }).then(r => r && r.result && r.result.value).catch(() => false);
    if (isNative) {
      await cdpSend(target, 'Runtime.callFunctionOn', {
        objectId, arguments: [{ value: optText }],
        functionDeclaration: `function(t){const o=Array.from(this.options).find(o=>o.textContent.trim().toLowerCase()===t.toLowerCase());if(o){this.value=o.value;this.dispatchEvent(new Event('change',{bubbles:true}));return true;}return false;}`,
        returnByValue: true,
      });
      return { success: true, action_type: 'select', details: `选择"${optText}"` };
    }
  }
  // 自定义下拉:点触发器展开,等,再在弹层里精确文本匹配点击(不用子串,防 wrong-click)
  const coords = await getElementCoordinates(target, backendNodeId);
  if (coords && coords.quads) { const pt = quadsToClickPoint(coords.quads, vw, vh); await dispatchRealClick(rootTarget, pt.x, pt.y); await sleep(500); }
  const found = await cdpSend(target, 'Runtime.evaluate', {
    expression: `(()=>{const t=${JSON.stringify(optText.toLowerCase().trim())};const items=document.querySelectorAll('[role="option"],[role="listbox"] li,.ant-select-item,.el-select-dropdown__item,[class*="option"],[class*="menu-item"],[class*="dropdown"] li');for(const it of items){if((it.textContent||'').toLowerCase().trim()===t){const r=it.getBoundingClientRect();it.click();return {x:r.x+r.width/2,y:r.y+r.height/2};}}return null;})()`,
    returnByValue: true,
  }).then(r => r && r.result && r.result.value).catch(() => null);
  if (found) return { success: true, action_type: 'select', details: `选择"${optText}"` };
  return { success: false, action_type: 'select', error: `下拉项未找到:"${optText}"` };
}

// 悬停:取坐标→真实鼠标移动(触发 CSS :hover / JS mouseenter)。
async function doHover(domTarget, rootTarget, backendNodeId, vw, vh) {
  await cdpSend(domTarget, 'DOM.scrollIntoViewIfNeeded', { backendNodeId }).catch(() => {});
  await sleep(50);
  const coords = await getElementCoordinates(domTarget, backendNodeId);
  if (!coords || !coords.quads) return { success: false, action_type: 'hover', error: '无法定位坐标' };
  const pt = quadsToClickPoint(coords.quads, vw, vh);
  await cdpSend(rootTarget, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x: pt.x, y: pt.y });   // 鼠标走根 target
  return { success: true, action_type: 'hover', details: '悬停' };
}






