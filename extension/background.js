importScripts('shared.js');

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

const MAX_LLM_BODY_BYTES = 25 * 1024 * 1024;
const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
const AGENT_ENDPOINT_PATHS = ['/v1/agent/execute', '/v1/agent/step', '/v1/agent/cancel'];

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

  if (request.type === 'DEBUGGER_CLICK') {
    handleDebuggerClick(request.tabId, request.x, request.y)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error?.message || '未知错误' }));
    return true;
  }

  if (request.type === 'DEBUGGER_TYPE') {
    handleDebuggerType(request.tabId, request.text)
      .then(() => sendResponse({ ok: true }))
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
});

// Debugger 会话管理：保持 attach 状态复用，避免每次 attach/detach 的开销
const _debuggerAttached = new Set();

chrome.debugger.onDetach.addListener((source) => {
  _debuggerAttached.delete(source.tabId);
});

async function debuggerEnsureAttached(tabId) {
  if (_debuggerAttached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, '1.3');
  _debuggerAttached.add(tabId);
}

async function debuggerDetach(tabId) {
  if (!_debuggerAttached.has(tabId)) return;
  await chrome.debugger.detach({ tabId }).catch(() => {});
  _debuggerAttached.delete(tabId);
}

async function handleDebuggerClick(tabId, x, y) {
  await debuggerEnsureAttached(tabId);
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
    type: 'mousePressed', x, y, button: 'left', clickCount: 1
  });
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
    type: 'mouseReleased', x, y, button: 'left', clickCount: 1
  });
}

// 真实鼠标移动，触发 hover 浮层（CSS :hover 和 JS mouseenter 都生效）
async function handleDebuggerHover(tabId, x, y) {
  await debuggerEnsureAttached(tabId);
  await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
    type: 'mouseMoved', x, y
  });
}

async function handleDebuggerType(tabId, text) {
  await debuggerEnsureAttached(tabId);
  for (const char of text) {
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
      type: 'keyDown', text: char
    });
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchKeyEvent', {
      type: 'keyUp', text: char
    });
    await new Promise(r => setTimeout(r, 10));
  }
}
