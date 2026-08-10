// Chrome 扩展后台脚本：负责右键菜单、侧边栏唤起、API 白名单校验和后端请求转发。
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

const MAX_PROMPT_LENGTH = 8000;
const MAX_LLM_BODY_BYTES = 25 * 1024 * 1024;
const CUSTOM_API_BASE_URLS_KEY = 'customApiBaseUrls';
const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
const CHAT_HISTORY_ENDPOINT_PREFIX = '/api/chats';
const MEMORY_ENDPOINT_PREFIX = '/api/memories';
const MEMORY_JOB_ENDPOINT_PREFIX = '/api/memory_jobs';
const PLAN_ENDPOINT_PREFIX = '/api/plans';
const DEFAULT_ALLOWED_CHAT_URLS = new Set([
  'https://api.openai.com/v1/chat/completions'
]);

// 生成一次右键菜单动作的唯一 ID，用于侧边栏去重处理。
function createActionId() {
  if (crypto?.randomUUID) return crypto.randomUUID();

  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

// 截断右键选中的文本，避免把过长内容塞进 session storage。
function sanitizeActionText(text) {
  const value = String(text || '').trim();
  return value.length > MAX_PROMPT_LENGTH ? `${value.slice(0, MAX_PROMPT_LENGTH - 1)}…` : value;
}

// 构造传给侧边栏的待处理动作，统一记录来源、时间和用户手势。
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

// 规范化 OpenAI-compatible chat completions URL，去掉尾斜杠方便白名单比较。
function normalizeChatUrl(value) {
  const url = new URL(String(value || ''));
  return `${url.origin}${url.pathname.replace(/\/$/, '')}`;
}

// 从 /v1 API base 推导 Browser Agent 后端根路径，用于 pages/chats/memory/plans API。
function buildBackendRootFromApiBase(apiBaseUrl) {
  const normalizedApiBaseUrl = normalizeApiBaseUrl(apiBaseUrl);
  const url = new URL(normalizedApiBaseUrl);
  let pathname = url.pathname.replace(/\/$/, '');
  if (pathname.endsWith('/v1')) {
    pathname = pathname.slice(0, -3);
  }
  return `${url.origin}${pathname}`;
}

// 判断 IPv4 地址是否属于本机、内网、链路本地或 CGNAT 网段。
function isPrivateIpv4Host(host) {
  return /^127\./.test(host)
    || /^10\./.test(host)
    || /^192\.168\./.test(host)
    || /^0\.0\.0\.0$/.test(host)
    || /^172\.(1[6-9]|2\d|3[01])\./.test(host)
    || /^169\.254\./.test(host)
    || /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./.test(host);
}

// 判断 hostname 是否是本机或内网地址；本地后端允许使用 http 和缺省 Authorization。
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

// 校验并规范化 API base URL；公网只允许 HTTPS，本地/内网允许 HTTP。
function normalizeApiBaseUrl(value) {
  const url = new URL(String(value || ''));
  const allowLocalHttp = url.protocol === 'http:' && isPrivateOrLocalHost(url.hostname);
  if ((url.protocol !== 'https:' && !allowLocalHttp) || url.search || url.hash || url.username || url.password) return '';
  return `${url.origin}${url.pathname.replace(/\/$/, '')}`;
}

// 获取允许转发聊天请求的完整 /chat/completions URL 集合。
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

// 获取允许调用“刷新当前页快照”的后端 URL 集合。
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

// 获取允许访问的后端根地址集合，供历史、记忆和计划 API 共享。
async function getAllowedBackendApiRoots() {
  const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
  return new Set(
    customUrls
      .map((url) => {
        try {
          return buildBackendRootFromApiBase(url);
        } catch {
          return '';
        }
      })
      .filter(Boolean)
  );
}

// 判断一次聊天请求 URL 是否严格命中白名单。
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

// 判断页面快照刷新 URL 是否严格命中白名单。
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

// 判断通用后端 API URL 是否位于允许的 Browser Agent API 前缀下。
async function isAllowedBackendApiUrl(value) {
  try {
    const url = new URL(String(value || ''));
    if (url.hash) return false;

    const normalized = normalizeChatUrl(url.href);
    const pageRefreshUrls = await getAllowedPageRefreshUrls();
    if (pageRefreshUrls.has(normalized)) return true;

    const roots = await getAllowedBackendApiRoots();
    for (const root of roots) {
      const historyRoot = `${root}${CHAT_HISTORY_ENDPOINT_PREFIX}`;
      if (normalized === historyRoot || normalized.startsWith(`${historyRoot}/`)) {
        return true;
      }
      const memoryRoot = `${root}${MEMORY_ENDPOINT_PREFIX}`;
      if (normalized === memoryRoot || normalized.startsWith(`${memoryRoot}/`)) {
        return true;
      }
      const memoryJobRoot = `${root}${MEMORY_JOB_ENDPOINT_PREFIX}`;
      if (normalized === memoryJobRoot || normalized.startsWith(`${memoryJobRoot}/`)) {
        return true;
      }
      const planRoot = `${root}${PLAN_ENDPOINT_PREFIX}`;
      if (normalized === planRoot || normalized.startsWith(`${planRoot}/`)) {
        return true;
      }
    }
    return false;
  } catch {
    return false;
  }
}

// 以下 sendLlm* 函数把后台请求状态统一转发给侧边栏当前消息气泡。
function sendLlmError(msgId, error) {
  chrome.runtime.sendMessage({ type: 'LLM_ERROR', msgId, error });
}

// 通知侧边栏当前流式请求结束。
function sendLlmDone(msgId) {
  chrome.runtime.sendMessage({ type: 'LLM_DONE', msgId });
}

// 发送一段流式文本增量。
function sendLlmChunk(msgId, chunk) {
  if (chunk) {
    chrome.runtime.sendMessage({ type: 'LLM_CHUNK', msgId, chunk });
  }
}

// 发送最终可展示的引用来源列表。
function sendLlmSources(msgId, sources) {
  chrome.runtime.sendMessage({
    type: 'LLM_SOURCES',
    msgId,
    sources: Array.isArray(sources) ? sources : []
  });
}

// 发送服务端清洗后的最终文本，覆盖前端本地拼接结果。
function sendLlmFinalText(msgId, content) {
  chrome.runtime.sendMessage({
    type: 'LLM_FINAL_TEXT',
    msgId,
    content: String(content || '')
  });
}

// 兼容 OpenAI、部分兼容服务和本地后端的多种文本字段形态。
function extractChunkText(dataObj) {
  return dataObj?.choices?.[0]?.delta?.content
    || dataObj?.choices?.[0]?.message?.content
    || dataObj?.choices?.[0]?.text
    || dataObj?.message?.content
    || dataObj?.response
    || dataObj?.content
    || '';
}

// 尽量从错误响应体中提取可读错误，提取失败则回退 HTTP 状态。
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

// 打开侧边栏，并把待处理动作放入 session storage 供侧边栏启动后读取。
function openSidePanelWithAction(windowId, action) {
  if (!windowId) return Promise.resolve();

  const openPromise = chrome.sidePanel.open({ windowId });
  chrome.storage.session.set({ pendingSidePanelAction: action }).catch(console.error);

  return openPromise;
}

// 把右键划词动作转成解释/翻译任务，等待用户在侧边栏确认发送。
function queueAutoTask(windowId, taskType, focusText) {
  const action = createContextMenuAction('AUTO_SEND_PROMPT', {
    taskType,
    focusText: sanitizeActionText(focusText),
    autoSend: false
  });

  return openSidePanelWithAction(windowId, action);
}

// 把右键图片动作转成图片工具输入，打开侧边栏的图片工具页签。
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

// 处理右键菜单点击，按菜单 ID 分发到文本任务或图片工具。
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

// 安装或更新扩展时重建右键菜单，避免重复菜单项。
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

// 转发聊天请求：校验 URL、鉴权和请求体后，兼容 SSE 与非流式 JSON 响应。
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
    body
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
        if (dataObj?.type === 'final_answer') {
          sendLlmFinalText(msgId, dataObj.content);
          continue;
        }
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

// 转发后端 JSON API：只允许 Browser Agent 自己的 pages/chats/memory/plans 路径。
async function handleCallApiJson(request) {
  const { url, options } = request;

  if (!(await isAllowedBackendApiUrl(url))) {
    throw new Error('API 地址不被允许');
  }

  const apiHost = new URL(url).hostname;
  const allowMissingAuth = isPrivateOrLocalHost(apiHost);
  const authHeader = options?.headers?.Authorization || options?.headers?.authorization || '';
  const authToken = String(authHeader).replace(/^Bearer\s+/i, '').trim();
  const hasBearerAuth = String(authHeader).startsWith('Bearer ') && !!authToken;
  const method = String(options?.method || 'GET').toUpperCase();
  if (!['GET', 'POST', 'PATCH', 'DELETE'].includes(method) || (!hasBearerAuth && !allowMissingAuth)) {
    throw new Error('API 请求配置无效');
  }

  const methodAllowsBody = ['POST', 'PATCH'].includes(method);
  const body = methodAllowsBody ? String(options?.body || '') : '';
  if (methodAllowsBody) {
    if (!body || body.length > MAX_LLM_BODY_BYTES) {
      throw new Error('API 请求体为空或过大');
    }

    try {
      JSON.parse(body);
    } catch {
      throw new Error('API 请求体不是有效 JSON');
    }
  }

  const requestHeaders = {
    'Content-Type': 'application/json'
  };
  if (hasBearerAuth) {
    requestHeaders.Authorization = authHeader;
  }

  const response = await fetch(url, {
    method,
    credentials: 'omit',
    redirect: 'error',
    headers: requestHeaders,
    body: methodAllowsBody ? body : undefined
  });

  if (!response.ok) {
    const errorMessage = await getResponseErrorMessage(response);
    throw new Error(`请求失败 (${response.status})：${errorMessage}`);
  }

  return response.json();
}

// 接收侧边栏消息，并用异步 sendResponse 模式返回结果。
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
});
