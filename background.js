chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);

const MAX_PROMPT_LENGTH = 8000;
const MAX_LLM_BODY_BYTES = 25 * 1024 * 1024;
const CUSTOM_API_BASE_URLS_KEY = 'customApiBaseUrls';
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

function normalizeApiBaseUrl(value) {
  const url = new URL(String(value || ''));
  if (url.protocol !== 'https:' || url.search || url.hash || url.username || url.password) return '';
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

function queueAutoSendPrompt(windowId, text) {
  const action = createContextMenuAction('AUTO_SEND_PROMPT', {
    text: sanitizeActionText(text)
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

    const explainPrompt = `请帮我解释以下名词，要求给出通俗、准确、适合快速理解的说明，并补充必要的背景信息：\n\n${info.selectionText}`;
    queueAutoSendPrompt(tab.windowId, explainPrompt).catch(console.error);
    return;
  }

  if (info.menuItemId === 'translate-to-copilot' && tab?.windowId) {
    const translatedPrompt = `请帮我翻译以下文本为中文（结合当前的语境，给出合理、文艺且正式翻译即可）：\n\n${info.selectionText}`;
    queueAutoSendPrompt(tab.windowId, translatedPrompt).catch(console.error);
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
  const authHeader = options?.headers?.Authorization || options?.headers?.authorization || '';
  const authToken = String(authHeader).replace(/^Bearer\s+/i, '').trim();
  if (options?.method !== 'POST' || !String(authHeader).startsWith('Bearer ') || !authToken) {
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

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'omit',
    redirect: 'error',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': authHeader
    },
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
        sendLlmChunk(msgId, extractChunkText(JSON.parse(dataStr)));
      } catch (error) {
        // 忽略无法解析的流式碎片
      }
    }
  }
}

chrome.runtime.onMessage.addListener((request) => {
  if (request.type === 'CALL_LLM_STREAM') {
    handleCallLlmStream(request).catch((error) => {
      sendLlmError(request.msgId, `请求失败：${error?.message || '未知错误'}`);
    });
    return true;
  }
});
