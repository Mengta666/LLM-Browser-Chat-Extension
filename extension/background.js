chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

const MAX_PROMPT_LENGTH = 8000;
const MAX_LLM_BODY_BYTES = 25 * 1024 * 1024;
const CUSTOM_API_BASE_URLS_KEY = 'customApiBaseUrls';
const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
const AGENT_ENDPOINT_PATHS = ['/v1/agent/execute', '/v1/agent/step', '/v1/agent/cancel'];
const DEFAULT_ALLOWED_CHAT_URLS = new Set([
  'https://api.openai.com/v1/chat/completions'
]);

function createActionId() {
  if (crypto?.randomUUID) return crypto.randomUUID();

  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function sanitizeActionText(text) {
  const value = String(text || '').trim();
  return value.length > MAX_PROMPT_LENGTH ? `${value.slice(0, MAX_PROMPT_LENGTH - 1)}…` : value;
}

function createContextMenuAction(type, payload) {
  return {
    ...payload,
    type,
    actionId: createActionId(),
    source: 'context_menu',
    userGesture: true,
    createdAt: Date.now()
  };
}

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

function isPrivateIpv4Host(host) {
  return /^127\./.test(host)
    || /^10\./.test(host)
    || /^192\.168\./.test(host)
    || /^0\.0\.0\.0$/.test(host)
    || /^172\.(1[6-9]|2\d|3[01])\./.test(host)
    || /^169\.254\./.test(host)
    || /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(host);
}

function isPrivateOrLocalHost(hostname) {
  const rawHost = String(hostname || '').trim().toLowerCase();
  if (!rawHost) return false;

  const host = rawHost.startsWith('[') && rawHost.endsWith(']')
    ? rawHost.slice(1, -1)
    : rawHost;

  if (!host) return false;
  if (host === 'localhost' || host === '::1' || host === '::') return true;
  if (isPrivateIpv4Host(host)) return true;
  if (/^(?:fc|fd)[0-9a-f:]*$/i.test(host)) return true;
  if (/^fe80:/i.test(host)) return true;

  const ipv4MappedMatch = host.match(/^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i);
  if (ipv4MappedMatch) {
    return isPrivateIpv4Host(ipv4MappedMatch[1]);
  }

  return false;
}

function normalizeApiBaseUrl(value) {
  const url = new URL(String(value || ''));
  const allowLocalHttp = url.protocol === 'http:' && isPrivateOrLocalHost(url.hostname);
  if ((url.protocol !== 'https:' && !allowLocalHttp) || url.search || url.hash || url.username || url.password) return '';
  return `${url.origin}${url.pathname.replace(/\/$/, '')}`;
}

async function getAllowedChatUrls() {
  const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
  return new Set([
    ...DEFAULT_ALLOWED_CHAT_URLS,
    ...customUrls
      .map((url) => {
        try {
          return normalizeApiBaseUrl(url);
        } catch {
          return '';
        }
      })
      .filter(Boolean)
      .map((url) => `${url}/chat/completions`)
  ]);
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

async function isAllowedChatUrl(value) {
  try {
    const url = new URL(String(value || ''));
    const allowedUrls = await getAllowedChatUrls();
    return allowedUrls.has(normalizeChatUrl(url.href))
      && !url.search
      && !url.hash;
  } catch {
    return false;
  }
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

function sendLlmError(msgId, error) {
  chrome.runtime.sendMessage({ type: 'LLM_ERROR', msgId, error });
}

function sendLlmDone(msgId) {
  chrome.runtime.sendMessage({ type: 'LLM_DONE', msgId });
}

function sendLlmChunk(msgId, chunk) {
  if (chunk) {
    chrome.runtime.sendMessage({ type: 'LLM_CHUNK', msgId, chunk });
  }
}

function sendLlmSources(msgId, sources) {
  chrome.runtime.sendMessage({
    type: 'LLM_SOURCES',
    msgId,
    sources: Array.isArray(sources) ? sources : []
  });
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

function openSidePanelWithAction(windowId, action) {
  if (!windowId) return Promise.resolve();

  const openPromise = chrome.sidePanel.open({ windowId });
  chrome.storage.session.set({ pendingSidePanelAction: action }).catch(console.error);

  return openPromise;
}

function queueAutoTask(windowId, taskType, focusText) {
  const action = createContextMenuAction('AUTO_SEND_PROMPT', {
    taskType,
    focusText: sanitizeActionText(focusText),
    autoSend: false
  });

  return openSidePanelWithAction(windowId, action);
}

function openImageToolTab(tab, info) {
  if (!tab?.windowId) return Promise.resolve();

  const action = createContextMenuAction('AUTO_IMAGE_TOOL', {
    image: {
      srcUrl: info.srcUrl || '',
      pageUrl: info.pageUrl || '',
      title: info.title || ''
    }
  });

  return openSidePanelWithAction(tab.windowId, action);
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'send-to-copilot') {
    if (!tab?.windowId) return;

    queueAutoTask(tab.windowId, 'explain', info.selectionText || '').catch(console.error);
    return;
  }

  if (info.menuItemId === 'translate-to-copilot' && tab?.windowId) {
    queueAutoTask(tab.windowId, 'translate', info.selectionText || '').catch(console.error);
    return;
  }

  if (info.menuItemId === 'image-to-base64-tool') {
    openImageToolTab(tab, info).catch(console.error);
  }
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: 'send-to-copilot',
      title: "📘 发送给助手 名词解释: '%s'",
      contexts: ['selection']
    });

    chrome.contextMenus.create({
      id: 'translate-to-copilot',
      title: "🌐 发送给助手 进行翻译: '%s'",
      contexts: ['selection']
    });

    chrome.contextMenus.create({
      id: 'image-to-base64-tool',
      title: '🖼️ 发送到侧边栏图片工具',
      contexts: ['image']
    });
  });
});

async function handleCallLlmStream(request) {
  const { url, options, msgId } = request;

  if (!(await isAllowedChatUrl(url))) {
    sendLlmError(msgId, 'API 地址不被允许');
    return;
  }

  const apiHost = new URL(url).hostname;
  const allowMissingAuth = isPrivateOrLocalHost(apiHost);
  const authHeader = options?.headers?.Authorization || options?.headers?.authorization || '';
  const authToken = String(authHeader).replace(/^Bearer\s+/i, '').trim();
  const hasBearerAuth = String(authHeader).startsWith('Bearer ') && !!authToken;
  if (options?.method !== 'POST' || (!hasBearerAuth && !allowMissingAuth)) {
    sendLlmError(msgId, 'API 请求配置无效');
    return;
  }

  if (apiHost === 'api.openai.com' && !authToken.startsWith('sk-')) {
    sendLlmError(msgId, 'API 请求配置无效');
    return;
  }

  const body = String(options?.body || '');
  if (!body || body.length > MAX_LLM_BODY_BYTES) {
    sendLlmError(msgId, 'API 请求体为空或过大');
    return;
  }

  try {
    JSON.parse(body);
  } catch {
    sendLlmError(msgId, 'API 请求体不是有效 JSON');
    return;
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
    signal: AbortSignal.timeout(60000)
  });

  if (!response.ok) {
    const errorMessage = await getResponseErrorMessage(response);
    sendLlmError(msgId, `请求失败 (${response.status})：${errorMessage}`);
    return;
  }

  const contentType = response.headers.get('content-type') || '';
  if (!/text\/event-stream|text\/plain/i.test(contentType)) {
    const dataObj = await response.json();
    if (Array.isArray(dataObj?.sources)) {
      sendLlmSources(msgId, dataObj.sources);
    }
    const content = extractChunkText(dataObj);
    if (content) {
      sendLlmChunk(msgId, content);
      sendLlmDone(msgId);
    } else {
      const responseKeys = dataObj && typeof dataObj === 'object'
        ? Object.keys(dataObj).slice(0, 8).join(', ')
        : typeof dataObj;
      sendLlmError(msgId, `响应中没有可显示的文本内容。响应字段：${responseKeys || '无'}`);
    }
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      sendLlmDone(msgId);
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith(':')) continue;

      const dataStr = trimmed.startsWith('data:')
        ? trimmed.substring(5).trim()
        : trimmed;

      if (dataStr === '[DONE]') {
        sendLlmDone(msgId);
        return;
      }

      try {
        const dataObj = JSON.parse(dataStr);
        if (dataObj?.type === 'sources') {
          sendLlmSources(msgId, dataObj.sources);
          continue;
        }
        sendLlmChunk(msgId, extractChunkText(dataObj));
      } catch (error) {
        // 忽略无法解析的流式碎片
      }
    }
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
    signal: AbortSignal.timeout(60000)
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
      sendLlmError(request.msgId, `请求失败：${error?.message || '未知错误'}`);
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
