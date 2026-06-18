document.addEventListener('DOMContentLoaded', async () => {
  let conversationHistory = []; // 存储上下文记忆
  const handledAutoSendActionIds = new Set();
  let attachedImage = null;
  let imageToolCurrentDataUrl = '';
  let imageToolCurrentFileName = 'image.png';
  const TASK_TYPE_LABELS = {
    chat: '普通问答',
    explain: '解释',
    translate: '翻译'
  };
  const PAGE_CONTEXT_CANCELLED_MESSAGE = '已取消读取当前网页上下文。';
  const taskState = {
    taskType: 'chat',
    focusText: '',
    source: 'manual'
  };
  const drawerState = {
    taskOpen: false,
    pageContextOpen: false
  };
  let activeSourceContainer = null;
  const pageContextState = {
    enabled: false,
    locked: false,
    forceRefreshPage: false,
    refreshing: false,
    pageContextId: '',
    snapshot: null,
    lastError: '',
    lastRefreshMessage: ''
  };
  const markdownParser = window.marked;
  const MAX_HISTORY_MESSAGES = 12;
  const MAX_PROMPT_LENGTH = 8000;
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
  const MAX_IMAGE_PIXELS = 20_000_000;
  const MAX_ACTION_AGE_MS = 5 * 60 * 1000;
  const MAX_URL_LENGTH = 2048;
  const PRIVACY_NOTICE_KEY = 'privacyNoticeAccepted';
  const CURRENT_CHAT_ID_KEY = 'currentChatId';
  const CUSTOM_API_BASE_URLS_KEY = 'customApiBaseUrls';
  const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
  const ALLOWED_IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  const SOURCE_BLOCK_PATTERN = /\[([^\[\]]+)\]/g;
  const SOURCE_ID_PATTERN = /^S\d+$/i;
  let currentChatId = '';
  const DEFAULT_API_BASE_URLS = new Set([
    'https://api.openai.com/v1'
  ]);
  const DEFAULT_API_URL = 'https://api.openai.com/v1';
  const allowedTags = new Set([
    'a', 'abbr', 'b', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3',
    'h4', 'h5', 'h6', 'hr', 'i', 'li', 'ol', 'p', 'pre', 's',
    'strong', 'sub', 'sup', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul'
  ]);
  const allowedAttributes = {
    a: new Set(['href', 'title', 'target', 'rel']),
    code: new Set(['class']),
    pre: new Set(['class']),
    li: new Set(['class']),
    th: new Set(['align', 'colspan', 'rowspan']),
    td: new Set(['align', 'colspan', 'rowspan'])
  };

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (character) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[character]));
  }

  function normalizeMarkdownText(text) {
    return String(text || '')
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/\r\n/g, '\n');
  }

  function loadImageElement(src) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error('图片解码失败'));
      image.src = src;
    });
  }

  function normalizeImageMimeType(type) {
    const value = String(type || '').trim().toLowerCase();
    if (value === 'image/jpg') return 'image/jpeg';
    return value;
  }

  function isAllowedImageMime(type) {
    return ALLOWED_IMAGE_MIME_TYPES.has(normalizeImageMimeType(type));
  }

  function assertImageDimensionsSafe(width, height) {
    if (!width || !height) {
      throw new Error('无法识别图片尺寸');
    }

    if (width * height > MAX_IMAGE_PIXELS) {
      throw new Error('图片分辨率过大');
    }
  }

  async function normalizeDataUrlToPng(dataUrl) {
    if (!isAllowedDataImageUrl(dataUrl)) {
      throw new Error('仅允许 10MB 以内的 PNG、JPG/JPEG、WebP 或 GIF 图片');
    }

    const image = await loadImageElement(dataUrl);
    const width = image.naturalWidth || image.width;
    const height = image.naturalHeight || image.height;
    assertImageDimensionsSafe(width, height);

    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('无法创建图片画布');

    context.drawImage(image, 0, 0, width, height);
    const normalizedDataUrl = canvas.toDataURL('image/png');
    if (!isAllowedDataImageUrl(normalizedDataUrl)) {
      throw new Error('转换后的图片超过 10MB');
    }
    return normalizedDataUrl;
  }

  function isSafeUrl(url) {
    const value = String(url || '').trim();
    if (!value) return false;

    try {
      const parsed = new URL(value);
      return ['https:', 'mailto:'].includes(parsed.protocol);
    } catch {
      return false;
    }
  }

  function sanitizeRenderedHtml(html) {
    const template = document.createElement('template');
    template.innerHTML = html;

    Array.from(template.content.querySelectorAll('*'))
      .reverse()
      .forEach((element) => {
        const tagName = element.tagName.toLowerCase();

        if (!allowedTags.has(tagName)) {
          if (tagName === 'script' || tagName === 'style' || tagName === 'iframe' || tagName === 'noscript') {
            element.remove();
          } else {
            element.replaceWith(...Array.from(element.childNodes));
          }
          return;
        }

        const tagAttributes = allowedAttributes[tagName] || new Set();
        Array.from(element.attributes).forEach((attribute) => {
          const attributeName = attribute.name.toLowerCase();
          if (!tagAttributes.has(attributeName)) {
            element.removeAttribute(attribute.name);
            return;
          }

          if (tagName === 'a' && attributeName === 'href' && !isSafeUrl(attribute.value)) {
            element.removeAttribute(attribute.name);
          }

        });

        if (tagName === 'a') {
          if (!element.getAttribute('href')) {
            element.removeAttribute('target');
            element.removeAttribute('rel');
          } else {
            element.setAttribute('target', '_blank');
            element.setAttribute('rel', 'noreferrer noopener');
          }
        }

      });

    return template.innerHTML;
  }

  function renderMarkdown(markdown) {
    const source = normalizeMarkdownText(markdown);

    if (!markdownParser?.parse) {
      return escapeHtml(source).replace(/\n/g, '<br>');
    }

    const renderedHtml = markdownParser.parse(source, {
      gfm: true,
      breaks: true
    });

    return sanitizeRenderedHtml(renderedHtml);
  }

  function renderMathInContainer(container) {
    if (typeof renderMathInElement !== 'function') return;

    renderMathInElement(container, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false }
      ],
      throwOnError: false,
      strict: false,
      trust: false,
      output: 'mathml'
    });
  }

  async function copyTextToClipboard(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }

    const fallbackInput = document.createElement('textarea');
    fallbackInput.value = text;
    fallbackInput.setAttribute('readonly', 'readonly');
    fallbackInput.className = 'clipboard-fallback';
    document.body.appendChild(fallbackInput);
    fallbackInput.select();
    document.execCommand('copy');
    fallbackInput.remove();
  }

  function enhanceCodeBlocks(container) {
    container.querySelectorAll('pre').forEach((pre) => {
      if (pre.parentElement?.classList.contains('code-block')) return;

      const wrapper = document.createElement('div');
      wrapper.className = 'code-block';

      const copyButton = document.createElement('button');
      copyButton.type = 'button';
      copyButton.className = 'code-copy-btn';
      copyButton.textContent = '复制';
      copyButton.addEventListener('click', async () => {
        const codeText = pre.querySelector('code')?.innerText || pre.innerText || '';
        try {
          await copyTextToClipboard(codeText);
          copyButton.textContent = '已复制';
          setTimeout(() => {
            copyButton.textContent = '复制';
          }, 1200);
        } catch {
          copyButton.textContent = '失败';
          setTimeout(() => {
            copyButton.textContent = '复制';
          }, 1200);
        }
      });

      pre.replaceWith(wrapper);
      wrapper.appendChild(copyButton);
      wrapper.appendChild(pre);
    });
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function estimateDataUrlBytes(dataUrl) {
    const base64 = String(dataUrl || '').split(',')[1] || '';
    return Math.floor(base64.length * 0.75);
  }

  function createMessageId() {
    if (crypto?.randomUUID) return crypto.randomUUID();

    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  }

  function createChatId() {
    return `chat_${createMessageId()}`;
  }

  async function getOrCreateCurrentChatId() {
    if (currentChatId) return currentChatId;

    const stored = await chrome.storage.session.get([CURRENT_CHAT_ID_KEY]);
    const storedChatId = String(stored?.[CURRENT_CHAT_ID_KEY] || '').trim();
    if (storedChatId) {
      currentChatId = storedChatId;
      updatePageContextUi();
      return currentChatId;
    }

    currentChatId = createChatId();
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: currentChatId });
    updatePageContextUi();
    return currentChatId;
  }

  async function resetCurrentChatId() {
    currentChatId = createChatId();
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: currentChatId });
    updatePageContextUi();
    return currentChatId;
  }

  function normalizeApiBaseUrl(apiUrl) {
    const parsedUrl = new URL(String(apiUrl || DEFAULT_API_URL).trim());
    const allowLocalHttp = parsedUrl.protocol === 'http:' && isPrivateOrLocalHost(parsedUrl.hostname);
    if (parsedUrl.protocol !== 'https:' && !allowLocalHttp) {
      throw new Error('API 地址必须使用 HTTPS');
    }
    if (parsedUrl.username || parsedUrl.password) {
      throw new Error('API 地址不能包含用户名或密码');
    }
    const normalizedPath = parsedUrl.pathname.replace(/\/$/, '');
    const normalizedApiUrl = `${parsedUrl.origin}${normalizedPath}`;
    if (parsedUrl.search || parsedUrl.hash) {
      throw new Error('API 地址不能包含查询参数或片段');
    }

    return normalizedApiUrl;
  }

  function buildBackendEndpointUrl(apiBaseUrl, endpointPath) {
    const normalizedApiBaseUrl = normalizeApiBaseUrl(apiBaseUrl);
    const parsedUrl = new URL(normalizedApiBaseUrl);
    let basePath = parsedUrl.pathname.replace(/\/$/, '');
    if (basePath.endsWith('/v1')) {
      basePath = basePath.slice(0, -3);
    }
    return `${parsedUrl.origin}${basePath}${endpointPath}`;
  }

  async function getAllowedApiBaseUrls() {
    const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
    return new Set([
      ...DEFAULT_API_BASE_URLS,
      ...customUrls
        .filter((url) => typeof url === 'string')
        .map((url) => {
          try {
            return normalizeApiBaseUrl(url);
          } catch {
            return '';
          }
        })
        .filter(Boolean)
    ]);
  }

  function getApiHostPermissionPattern(apiUrl) {
    const parsedUrl = new URL(apiUrl);
    return `${parsedUrl.protocol}//${parsedUrl.hostname}/*`;
  }

  async function ensureApiHostPermission(apiUrl) {
    if (DEFAULT_API_BASE_URLS.has(apiUrl)) return;
    if (!chrome.permissions?.request) {
      throw new Error('当前浏览器不支持运行时 API 站点授权');
    }

    const granted = await chrome.permissions.request({ origins: [getApiHostPermissionPattern(apiUrl)] });
    if (!granted) {
      throw new Error('需要允许访问该 API 站点后才能保存自定义 API 地址');
    }
  }

  async function ensureCustomApiBaseUrlAllowed(apiUrl) {
    if (DEFAULT_API_BASE_URLS.has(apiUrl)) return;

    const allowedUrls = await getAllowedApiBaseUrls();
    if (allowedUrls.has(apiUrl)) return;

    const hostname = new URL(apiUrl).hostname;
    const ok = confirm([
      `确认添加自定义 API 地址：${apiUrl}`,
      '',
      `之后对话内容、图片和你输入的 API Key 会发送到 ${hostname}。`,
      '请确认这是你信任的 OpenAI-compatible API 服务。'
    ].join('\n'));
    if (!ok) {
      throw new Error('已取消添加自定义 API 地址');
    }

    await ensureApiHostPermission(apiUrl);
    const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
    await chrome.storage.local.set({
      [CUSTOM_API_BASE_URLS_KEY]: Array.from(new Set([...customUrls, apiUrl]))
    });
  }

  async function validateOpenAIApiConfig(apiUrl, apiKey) {
    const normalizedApiUrl = normalizeApiBaseUrl(apiUrl);
    const allowedUrls = await getAllowedApiBaseUrls();

    if (!allowedUrls.has(normalizedApiUrl)) {
      throw new Error('API 地址不在白名单中');
    }

    const normalizedApiKey = String(apiKey || '').trim();
    const isLocalApi = isPrivateOrLocalHost(new URL(normalizedApiUrl).hostname);
    if (!normalizedApiKey && !isLocalApi) {
      throw new Error('请先在设置中配置 API Key！');
    }

    if (new URL(normalizedApiUrl).hostname === 'api.openai.com' && !normalizedApiKey.startsWith('sk-')) {
      throw new Error('API Key 格式异常');
    }

    return normalizedApiUrl;
  }

  async function resolveApiRequestConfig({ requireBackendApi = false } = {}) {
    const { apiUrl, modelName } = await chrome.storage.local.get(['apiUrl', 'modelName']);
    const { apiKey, apiKeyApiUrl } = await getStoredApiCredential();
    const safeApiUrl = await validateOpenAIApiConfig(apiUrl || DEFAULT_API_URL, apiKey);
    const shouldEnforceApiKeyBinding = Boolean(String(apiKey || '').trim())
      && !isPrivateOrLocalHost(new URL(safeApiUrl).hostname);

    if (shouldEnforceApiKeyBinding && apiKeyApiUrl && apiKeyApiUrl !== safeApiUrl) {
      throw new Error('当前 API Key 与 API 地址不匹配，请在设置中重新保存配置');
    }

    if (requireBackendApi && DEFAULT_API_BASE_URLS.has(safeApiUrl)) {
      throw new Error('刷新快照需要连接 browser-agent 后端 API 地址，不能使用 OpenAI 官方 API 地址');
    }

    return {
      apiKey,
      modelName,
      safeApiUrl
    };
  }

  function buildMessagesPayload(history) {
    return [
      {
        role: 'system',
        content: '你是一个专业的浏览器助手，请使用 Markdown 格式回答。若用户消息包含图片，请先识别并分析图片内容，再结合文本回答。'
      },
      ...compactConversationHistory(history, { preserveLastMessageImages: true })
    ];
  }

  function createPageContextError(message, userCancelled = false) {
    const error = new Error(message);
    error.userCancelled = userCancelled;
    return error;
  }

  async function getActiveBrowserTab() {
    const queryOptionsList = [
      { active: true, lastFocusedWindow: true },
      { active: true, currentWindow: true }
    ];

    for (const queryOptions of queryOptionsList) {
      try {
        const tabs = await chrome.tabs.query(queryOptions);
        const tab = Array.isArray(tabs) ? tabs.find((candidate) => candidate?.id) : null;
        if (tab?.id) {
          return tab;
        }
      } catch (error) {
        console.warn('Failed to query active browser tab', queryOptions, error);
      }
    }

    return null;
  }

  function normalizeTaskType(value) {
    return TASK_TYPE_LABELS[value] ? value : 'chat';
  }

  function getTaskTypeLabel(taskType) {
    return TASK_TYPE_LABELS[normalizeTaskType(taskType)];
  }

  function getTaskFocusLabel(taskType) {
    return normalizeTaskType(taskType) === 'translate' ? '待翻译文本' : '待解释文本';
  }

  function getTaskElements() {
    return {
      title: document.getElementById('taskStateTitle'),
      summary: document.getElementById('taskStateSummary'),
      focusText: document.getElementById('taskStateFocusText'),
      meta: document.getElementById('taskStateMeta'),
      clearButton: document.getElementById('clearFocusTextBtn'),
      toggleButton: document.getElementById('toggleTaskDrawerBtn'),
      body: document.getElementById('taskDrawerBody'),
      indicator: document.getElementById('taskDrawerIndicator')
    };
  }

  function summarizeInlineText(text, fallback = '未设置', maxLength = 28) {
    const value = String(text || '').trim();
    if (!value) return fallback;
    return value.length > maxLength ? `${value.slice(0, maxLength)}…` : value;
  }

  function setDrawerPresentation(toggleButton, body, indicator, isOpen) {
    if (toggleButton) {
      toggleButton.setAttribute('aria-expanded', String(isOpen));
    }
    if (body) {
      body.hidden = !isOpen;
    }
    if (indicator) {
      indicator.textContent = isOpen ? '收起' : '展开';
    }
  }

  function updateChatInputPlaceholder() {
    const input = document.getElementById('chatInput');
    if (!input) return;

    if (taskState.taskType === 'translate') {
      input.placeholder = taskState.focusText
        ? '输入翻译要求 (可选，例如：更书面化、保留术语)...'
        : '先通过右键划词设置待翻译文本，或直接输入普通问题...';
      return;
    }

    if (taskState.taskType === 'explain') {
      input.placeholder = taskState.focusText
        ? '输入补充要求 (可选，例如：更技术一点、给一个例子)...'
        : '先通过右键划词设置待解释文本，或直接输入普通问题...';
      return;
    }

    input.placeholder = '输入问题 (Enter发送, Shift+Enter换行)...';
  }

  function updateTaskUi() {
    const {
      title,
      summary,
      focusText,
      meta,
      clearButton,
      toggleButton,
      body,
      indicator
    } = getTaskElements();
    if (!title || !summary || !focusText || !meta || !clearButton) return;

    const taskTypeLabel = getTaskTypeLabel(taskState.taskType);
    title.textContent = `当前任务：${taskTypeLabel}`;
    summary.textContent = `当前选中：${summarizeInlineText(taskState.focusText, '未设置')}`;
    focusText.textContent = taskState.focusText
      ? `当前选中：${taskState.focusText}`
      : '当前选中：未设置';

    if (taskState.taskType === 'chat') {
      meta.textContent = taskState.focusText
        ? '当前已保留一段选中文本；右键新文本会覆盖它，也可以清空后回到纯聊天。'
        : '右键划词后可直接进入翻译或解释任务。';
    } else if (taskState.focusText) {
      meta.textContent = `${taskTypeLabel}任务已就绪。输入框现在只用于补充要求，不再承担任务对象。`;
    } else {
      meta.textContent = `${taskTypeLabel}任务需要先提供一段选中文本。`;
    }

    clearButton.disabled = !taskState.focusText;
    document.querySelectorAll('.task-btn').forEach((button) => {
      button.classList.toggle('is-active', button.dataset.taskType === taskState.taskType);
    });
    setDrawerPresentation(toggleButton, body, indicator, drawerState.taskOpen);
    updateChatInputPlaceholder();
  }

  function setTaskState(nextTaskType, focusText = taskState.focusText, source = taskState.source) {
    const normalizedTaskType = normalizeTaskType(nextTaskType);
    taskState.taskType = normalizedTaskType;
    taskState.focusText = String(focusText || '').trim();
    taskState.source = source || 'manual';

    if (!taskState.focusText && taskState.taskType !== 'chat') {
      taskState.taskType = 'chat';
    }

    updateTaskUi();
  }

  function resetTaskState() {
    taskState.taskType = 'chat';
    taskState.focusText = '';
    taskState.source = 'manual';
    updateTaskUi();
  }

  function buildTaskSummaryLines(taskType, focusText, queryText) {
    const lines = [`任务：${getTaskTypeLabel(taskType)}`];
    if (focusText) {
      lines.push(`${taskType === 'translate' ? '当前选中' : '当前选中'}：${focusText}`);
    }
    if (queryText) {
      lines.push(`补充要求：${queryText}`);
    }
    return lines;
  }

  function renderUserTaskSummary(container, taskType, focusText, queryText) {
    const summary = document.createElement('div');
    summary.className = 'task-summary';

    const [titleLine, ...detailLines] = buildTaskSummaryLines(taskType, focusText, queryText);

    const title = document.createElement('div');
    title.className = 'task-summary-title';
    title.textContent = titleLine;
    summary.appendChild(title);

    detailLines.forEach((line) => {
      const row = document.createElement('div');
      row.className = 'task-summary-line';
      row.textContent = line;
      summary.appendChild(row);
    });

    container.appendChild(summary);
  }

  function buildConversationUserContent(taskType, focusText, queryText) {
    if (taskType === 'chat') {
      return queryText;
    }

    const lines = [
      `任务：${getTaskTypeLabel(taskType)}`,
      `${getTaskFocusLabel(taskType)}：${focusText}`
    ];
    if (queryText) {
      lines.push(`补充要求：${queryText}`);
    }
    return lines.join('\n');
  }

  async function readCurrentPageContext() {
    const hasPermission = await ensurePageContextPermission();
    if (!hasPermission) {
      throw createPageContextError('需要先允许扩展访问当前网页，才能读取网页上下文。');
    }

    const tab = await getActiveBrowserTab();
    if (!tab?.id) {
      throw createPageContextError('当前没有可读取的活动网页。');
    }

    if (!tab.url) {
      throw createPageContextError('当前活动标签页的地址不可见，请切回普通网页后重试。');
    }

    if (!isReadablePageUrl(tab.url)) {
      throw createPageContextError('当前页面不是普通的 http/https 网页，无法读取网页上下文。');
    }

    if (looksSensitivePageUrl(tab.url)) {
      const ok = confirm([
        '当前页面可能包含敏感内容。',
        '',
        '如果继续，本次请求会附带当前页面的标题、URL 和部分正文。',
        '请只在确认可以发送这些内容时继续。'
      ].join('\n'));
      if (!ok) {
        throw createPageContextError(PAGE_CONTEXT_CANCELLED_MESSAGE, true);
      }
    }

    const hasPagePermission = await ensurePageHostPermission(tab.url);
    if (!hasPagePermission) {
      throw createPageContextError('未获得当前站点读取权限，请先允许扩展读取该网页。');
    }

    let result = null;
    try {
      const [injectionResult] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const content = String(document.body?.innerText || '').trim().slice(0, 5000);
          return {
            url: location.href,
            title: document.title || '',
            content
          };
        }
      });
      result = injectionResult?.result || null;
    } catch (error) {
      const errorMessage = String(error?.message || '').trim();
      if (/Cannot access contents of (the )?page|The extensions gallery cannot be scripted/i.test(errorMessage)) {
        throw createPageContextError('当前页面禁止扩展读取内容，请切换到普通网页后重试。');
      }
      if (/Frame with ID 0 was removed|No tab with id/i.test(errorMessage)) {
        throw createPageContextError('当前页面已变化或关闭，请刷新页面后重试。');
      }
      throw createPageContextError(
        errorMessage ? `读取当前网页失败：${errorMessage}` : '当前页面不允许脚本读取内容，请切换到普通网页后重试。'
      );
    }

    if (!result) {
      throw createPageContextError('当前页面读取结果为空，可能是页面未完成加载或不允许注入脚本。');
    }

    const content = String(result.content || '').trim();
    if (!content) {
      throw createPageContextError('当前页面没有可读取的正文内容。');
    }

    return {
      url: String(result.url || tab.url),
      title: String(result.title || tab.title || '').trim(),
      content
    };
  }

  function getPageContextElements() {
    return {
      useToggle: document.getElementById('useCurrentPageToggle'),
      lockToggle: document.getElementById('lockCurrentPageToggle'),
      refreshButton: document.getElementById('refreshPageContextBtn'),
      clearButton: document.getElementById('clearPageSnapshotBtn'),
      summary: document.getElementById('pageContextSummary'),
      statusTitle: document.getElementById('pageContextStatusTitle'),
      statusMeta: document.getElementById('pageContextStatusMeta'),
      chatId: document.getElementById('pageContextChatId'),
      inlineChatId: document.getElementById('pageContextChatIdInline'),
      pageTitle: document.getElementById('pageContextPageTitle'),
      pageUrl: document.getElementById('pageContextPageUrl'),
      toggleButton: document.getElementById('togglePageContextDrawerBtn'),
      body: document.getElementById('pageContextDrawerBody'),
      indicator: document.getElementById('pageContextDrawerIndicator')
    };
  }

  function updatePageContextChatIdText() {
    const text = `Chat ID：${currentChatId || '未创建'}`;
    const { chatId, inlineChatId } = getPageContextElements();
    if (chatId) chatId.textContent = text;
    if (inlineChatId) inlineChatId.textContent = text;
  }

  function clonePageContext(currentPage) {
    if (!currentPage) return null;

    return {
      url: String(currentPage.url || '').trim(),
      title: String(currentPage.title || '').trim(),
      content: String(currentPage.content || '').trim()
    };
  }

  function getPageContextValidationError(currentPage, fallbackError = '') {
    if (!currentPage) {
      return fallbackError || '无法读取当前网页上下文，请确认页面可访问并允许读取。';
    }

    if (!String(currentPage.content || '').trim()) {
      return '当前页面没有可用的正文内容。';
    }

    return '';
  }

  function updatePageContextUi() {
    const {
      useToggle,
      lockToggle,
      refreshButton,
      clearButton,
      summary,
      statusTitle,
      statusMeta,
      chatId,
      inlineChatId,
      pageTitle,
      pageUrl,
      toggleButton,
      body,
      indicator
    } = getPageContextElements();
    if (!useToggle || !lockToggle || !refreshButton || !clearButton || !summary) return;

    useToggle.checked = pageContextState.enabled;
    lockToggle.checked = pageContextState.locked;
    lockToggle.disabled = !pageContextState.enabled;
    refreshButton.disabled = !pageContextState.enabled || pageContextState.refreshing;
    clearButton.disabled = !pageContextState.snapshot && !pageContextState.lastError;
    setDrawerPresentation(toggleButton, body, indicator, drawerState.pageContextOpen);

    if (!pageContextState.enabled) {
      summary.textContent = '未启用';
      statusTitle.textContent = '网页上下文未启用';
      statusMeta.textContent = '发送时不会附带当前页面内容。';
      updatePageContextChatIdText();
      pageTitle.textContent = '页面：未采集';
      pageUrl.textContent = 'URL：未采集';
      return;
    }

    if (!pageContextState.snapshot && pageContextState.lastError) {
      summary.textContent = `读取失败 · ${summarizeInlineText(pageContextState.lastError, '未知错误', 20)}`;
      statusTitle.textContent = '网页上下文读取失败';
      statusMeta.textContent = pageContextState.lastError;
      updatePageContextChatIdText();
      pageTitle.textContent = '页面：未采集';
      pageUrl.textContent = 'URL：未采集';
      return;
    }

    if (!pageContextState.snapshot) {
      summary.textContent = '等待采集';
      statusTitle.textContent = '网页上下文已启用（等待采集）';
      statusMeta.textContent = '已启用，但还没有页面快照。可以点“刷新快照”，也可以直接发送时自动采集。';
      updatePageContextChatIdText();
      pageTitle.textContent = '页面：未采集';
      pageUrl.textContent = 'URL：未采集';
      return;
    }

    if (pageContextState.locked) {
      statusTitle.textContent = '网页上下文已启用（已锁定）';
      statusMeta.textContent = '后续多轮会复用同一份页面快照。';
    } else {
      statusTitle.textContent = '网页上下文已启用（未锁定）';
      statusMeta.textContent = '发送时会重新读取当前活动页。';
    }

    summary.textContent = `${pageContextState.locked ? '已锁定' : '未锁定'} · ${summarizeInlineText(pageContextState.snapshot.title || pageContextState.snapshot.url, '(无标题)', 20)}`;
    if (pageContextState.forceRefreshPage) {
      statusMeta.textContent += ' 下次发送会刷新后端网页索引。';
    }
    if (pageContextState.refreshing) {
      statusMeta.textContent += ' 正在刷新后端索引。';
    } else if (pageContextState.lastError) {
      statusMeta.textContent += ` 最近刷新失败：${pageContextState.lastError}`;
    } else if (pageContextState.lastRefreshMessage) {
      statusMeta.textContent += ` ${pageContextState.lastRefreshMessage}`;
    }
    updatePageContextChatIdText();
    const titlePrefix = pageContextState.locked ? '页面：' : '最近快照：';
    pageTitle.textContent = `${titlePrefix}${pageContextState.snapshot.title || '(无标题)'}`;
    pageUrl.textContent = `URL：${pageContextState.snapshot.url || '(未知地址)'}`;
  }

  function resetPageContextState() {
    pageContextState.enabled = false;
    pageContextState.locked = false;
    pageContextState.forceRefreshPage = false;
    pageContextState.refreshing = false;
    pageContextState.pageContextId = '';
    pageContextState.snapshot = null;
    pageContextState.lastError = '';
    pageContextState.lastRefreshMessage = '';
    updatePageContextUi();
  }

  function clearPageContextSnapshot() {
    pageContextState.locked = false;
    pageContextState.forceRefreshPage = false;
    pageContextState.refreshing = false;
    pageContextState.pageContextId = '';
    pageContextState.snapshot = null;
    pageContextState.lastError = '';
    pageContextState.lastRefreshMessage = '';
    updatePageContextUi();
  }

  async function refreshPageContextSnapshot({ silent = false, forceRefresh = false } = {}) {
    try {
      const currentPage = await readCurrentPageContext();
      pageContextState.lastError = '';
      pageContextState.lastRefreshMessage = '';
      pageContextState.snapshot = clonePageContext(currentPage);
      pageContextState.pageContextId = pageContextState.locked ? `pagectx_${createMessageId()}` : '';
      pageContextState.forceRefreshPage = Boolean(forceRefresh);
      updatePageContextUi();
      return clonePageContext(pageContextState.snapshot);
    } catch (error) {
      console.warn('Failed to refresh current page context', error);
      pageContextState.lastError = String(error?.message || '').trim() || '无法读取当前网页上下文，请确认页面可访问并允许读取。';
      if (!pageContextState.locked) {
        pageContextState.snapshot = null;
        pageContextState.forceRefreshPage = false;
      }
      updatePageContextUi();
      if (!silent && !error?.userCancelled) {
        alert(pageContextState.lastError);
      }
      return null;
    }
  }

  async function resolvePageContextForSend() {
    if (!pageContextState.enabled) {
      return {
        currentPage: null,
        pageContextId: ''
      };
    }

    let currentPage = null;
    if (pageContextState.locked && pageContextState.snapshot) {
      currentPage = clonePageContext(pageContextState.snapshot);
    } else {
      currentPage = await refreshPageContextSnapshot({ silent: true });
    }

    if (!currentPage) {
      if (pageContextState.lastError && pageContextState.lastError !== PAGE_CONTEXT_CANCELLED_MESSAGE) {
        alert(pageContextState.lastError);
      }
      return null;
    }

    const validationError = getPageContextValidationError(currentPage, pageContextState.lastError);
    if (validationError) {
      alert(validationError);
      return null;
    }

    const pageContextId = pageContextState.locked
      ? (pageContextState.pageContextId || `pagectx_${createMessageId()}`)
      : `pagectx_${createMessageId()}`;

    if (pageContextState.locked) {
      pageContextState.pageContextId = pageContextId;
    }

    updatePageContextUi();
    return {
      currentPage: clonePageContext(currentPage),
      pageContextId
    };
  }

  async function getStoredApiCredential() {
    const [{ apiKey: sessionApiKey, apiKeyApiUrl }, { apiKey: localApiKey }] = await Promise.all([
      chrome.storage.session.get(['apiKey', 'apiKeyApiUrl']),
      chrome.storage.local.get(['apiKey'])
    ]);

    if (localApiKey) {
      if (!sessionApiKey) {
        await chrome.storage.session.set({ apiKey: localApiKey, apiKeyApiUrl: DEFAULT_API_URL });
      }
      await chrome.storage.local.remove(['apiKey']);
      return {
        apiKey: sessionApiKey || localApiKey,
        apiKeyApiUrl: apiKeyApiUrl || DEFAULT_API_URL
      };
    }

    return {
      apiKey: sessionApiKey || '',
      apiKeyApiUrl: apiKeyApiUrl || ''
    };
  }

  function callApiJson(url, options) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(
        {
          type: 'CALL_API_JSON',
          url,
          options
        },
        (response) => {
          const runtimeError = chrome.runtime.lastError;
          if (runtimeError) {
            reject(new Error(runtimeError.message || '后台请求失败'));
            return;
          }

          if (!response?.ok) {
            reject(new Error(response?.error || '后台请求失败'));
            return;
          }

          resolve(response.body);
        }
      );
    });
  }

  async function refreshPageContextIndexNow({ silent = false } = {}) {
    if (!pageContextState.enabled || pageContextState.refreshing) {
      return null;
    }

    pageContextState.refreshing = true;
    pageContextState.lastError = '';
    pageContextState.lastRefreshMessage = '正在刷新后端索引...';
    updatePageContextUi();

    try {
      const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
      if (!(await ensurePrivacyNoticeAccepted())) {
        throw createPageContextError('已取消刷新网页索引。', true);
      }

      const currentPage = await readCurrentPageContext();
      const validationError = getPageContextValidationError(currentPage);
      if (validationError) {
        throw new Error(validationError);
      }

      const chatId = await getOrCreateCurrentChatId();
      const pageContextId = pageContextState.locked
        ? (pageContextState.pageContextId || `pagectx_${createMessageId()}`)
        : `pagectx_${createMessageId()}`;
      const requestHeaders = {
        'Content-Type': 'application/json'
      };
      if (String(apiKey || '').trim()) {
        requestHeaders.Authorization = `Bearer ${String(apiKey).trim()}`;
      }

      const result = await callApiJson(
        buildBackendEndpointUrl(safeApiUrl, PAGE_REFRESH_ENDPOINT_PATH),
        {
          method: 'POST',
          headers: requestHeaders,
          body: JSON.stringify({
            chat_id: chatId,
            page_context_id: pageContextId,
            current_page: currentPage,
            force_refresh: true
          })
        }
      );

      pageContextState.snapshot = clonePageContext(currentPage);
      pageContextState.pageContextId = pageContextState.locked ? pageContextId : '';
      pageContextState.forceRefreshPage = false;
      pageContextState.lastError = '';
      pageContextState.lastRefreshMessage = result?.vector_cleanup_error
        ? `后端索引已刷新；旧向量清理失败：${result.vector_cleanup_error}`
        : '后端索引已刷新。';
      return clonePageContext(pageContextState.snapshot);
    } catch (error) {
      console.warn('Failed to refresh page index', error);
      pageContextState.lastError = String(error?.message || '').trim() || '刷新网页索引失败。';
      pageContextState.lastRefreshMessage = '';
      if (!silent && !error?.userCancelled) {
        alert(pageContextState.lastError);
      }
      return null;
    } finally {
      pageContextState.refreshing = false;
      updatePageContextUi();
    }
  }

  function compactMessageForHistory(message) {
    if (!Array.isArray(message.content)) {
      return {
        ...message,
        content: sanitizeHistoryText(message.content)
      };
    }

    return {
      ...message,
      content: message.content.map((part) => {
        if (part?.type === 'image_url') {
          return {
            type: 'text',
            text: '[之前上传过一张图片，已从本地历史中移除以保护隐私]'
          };
        }

        if (part?.type === 'text') {
          return {
            ...part,
            text: sanitizeHistoryText(part.text)
          };
        }

        return part;
      })
    };
  }

  function compactConversationHistory(history, options = {}) {
    const recentHistory = history.slice(-MAX_HISTORY_MESSAGES);
    const lastIndex = recentHistory.length - 1;

    return recentHistory.map((message, index) => {
      if (options.preserveLastMessageImages && index === lastIndex) {
        return message;
      }

      return compactMessageForHistory(message);
    });
  }

  function isAllowedDataImageUrl(value) {
    const text = String(value || '').trim();
    const match = text.match(/^data:(image\/(?:png|jpe?g|webp|gif));base64,([A-Za-z0-9+/=]+)$/i);
    if (!match) return false;
    if (!isAllowedImageMime(match[1])) return false;

    const estimatedBytes = Math.floor(match[2].length * 0.75);
    return estimatedBytes <= MAX_IMAGE_BYTES;
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

  function parseImageHttpUrl(value) {
    try {
      const rawValue = String(value || '').trim();
      if (!rawValue || rawValue.length > MAX_URL_LENGTH) return null;

      const parsed = new URL(rawValue);
      if (!['http:', 'https:'].includes(parsed.protocol)) return null;
      if (parsed.username || parsed.password) return null;
      return parsed;
    } catch {
      return null;
    }
  }

  function isAllowedImageHttpUrl(value) {
    return Boolean(parseImageHttpUrl(value));
  }

  function getHostPermissionPattern(value) {
    const parsed = new URL(String(value || '').trim());
    return `${parsed.protocol}//${parsed.hostname}/*`;
  }

  function getPageHostPermissionPattern(value) {
    const parsed = new URL(String(value || '').trim());
    if (!['http:', 'https:'].includes(parsed.protocol)) return '';
    return `${parsed.protocol}//${parsed.hostname}/*`;
  }

  async function ensureImageHostPermission(value) {
    if (!chrome.permissions?.request) {
      throw new Error('当前浏览器不支持运行时站点授权');
    }

    const permission = { origins: [getHostPermissionPattern(value)] };
    const granted = await chrome.permissions.request(permission);
    if (!granted) {
      throw new Error('需要允许访问该图片站点后才能下载外部图片');
    }
  }

  async function confirmRiskyImageUrl(value) {
    const parsed = parseImageHttpUrl(value);
    if (!parsed) return false;
    if (parsed.protocol === 'https:' && !isPrivateOrLocalHost(parsed.hostname)) return true;

    return confirm([
      `确认加载图片地址：${parsed.href}`,
      '',
      '该地址使用 HTTP、本机地址或内网地址。',
      '请只在你信任该来源时继续。'
    ].join('\n'));
  }

  async function ensureCapturePermission() {
    if (!chrome.permissions?.request) return false;
    try {
      const permission = { origins: ['<all_urls>'] };
      if (chrome.permissions.contains && await chrome.permissions.contains(permission)) return true;
      return await chrome.permissions.request(permission);
    } catch {
      return false;
    }
  }

  async function ensurePageContextPermission() {
    if (!chrome.permissions?.request) return true;
    try {
      const permission = { origins: ['<all_urls>'] };
      if (chrome.permissions.contains && await chrome.permissions.contains(permission)) return true;
      return await chrome.permissions.request(permission);
    } catch {
      return false;
    }
  }

  async function ensurePageHostPermission(value) {
    if (!chrome.permissions?.request) return true;

    const pattern = getPageHostPermissionPattern(value);
    if (!pattern) return true;

    const permission = { origins: [pattern] };
    try {
      if (chrome.permissions.contains && await chrome.permissions.contains(permission)) {
        return true;
      }
      return await chrome.permissions.request(permission);
    } catch {
      return false;
    }
  }

  async function ensurePrivacyNoticeAccepted() {
    const { [PRIVACY_NOTICE_KEY]: accepted } = await chrome.storage.local.get([PRIVACY_NOTICE_KEY]);
    if (accepted) return true;

    const ok = confirm([
      '首次使用前请确认：',
      '',
      '1. 你的输入、主动选择的网页文本、上传图片和框选截图会发送到配置的模型 API。',
      '2. API Key 仅保存在当前浏览器会话中，重启浏览器后可能需要重新输入。',
      '3. 扩展不会在后台持续读取网页内容，也不会自动发送网页内容。'
    ].join('\n'));

    if (ok) {
      await chrome.storage.local.set({ [PRIVACY_NOTICE_KEY]: true });
    }
    return ok;
  }

  function sanitizeHistoryText(text) {
    const value = String(text || '').trim();
    return value.length > MAX_PROMPT_LENGTH ? `${value.slice(0, MAX_PROMPT_LENGTH - 1)}…` : value;
  }

  function readBlobAsDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(reader.error || new Error('读取图片失败'));
      reader.readAsDataURL(blob);
    });
  }

  function deriveImageName(sourceUrl) {
    if (String(sourceUrl || '').startsWith('data:')) {
      return 'image';
    }

    try {
      const pathname = new URL(sourceUrl).pathname;
      const lastSegment = pathname.split('/').filter(Boolean).pop();
      return lastSegment || 'image';
    } catch {
      return 'image';
    }
  }

  async function loadImageFromSource(image) {
    if (!image) throw new Error('没有可用图片');

    if (image.dataUrl) {
      if (!isAllowedDataImageUrl(image.dataUrl)) {
        throw new Error('仅支持 10MB 以内的 PNG、JPG/JPEG、WebP 或 GIF 图片');
      }

      const normalizedDataUrl = await normalizeDataUrlToPng(image.dataUrl);
      return {
        name: image.name || 'image',
        type: image.type || 'image/*',
        size: image.size || 0,
        dataUrl: normalizedDataUrl
      };
    }

    throw new Error('图片导入仅支持 data URL');
  }

  function applyAttachedImage(image) {
    attachedImage = image;

    const imagePreview = document.getElementById('imagePreview');
    const imagePreviewImg = document.getElementById('imagePreviewImg');
    const imagePreviewName = document.getElementById('imagePreviewName');
    const imagePreviewSize = document.getElementById('imagePreviewSize');
    const clearImageBtn = document.getElementById('clearImageBtn');

    imagePreviewImg.src = image.dataUrl;
    imagePreviewName.textContent = image.name || 'image';
    imagePreviewSize.textContent = `${image.type || 'image'} · ${formatFileSize(image.size || 0)}`;
    imagePreview.classList.remove('hidden');
    clearImageBtn.hidden = false;
  }

  function clearAttachedImage() {
    attachedImage = null;
    const imageInput = document.getElementById('imageInput');
    const imagePreview = document.getElementById('imagePreview');
    const clearImageBtn = document.getElementById('clearImageBtn');

    imageInput.value = '';
    imagePreview.classList.add('hidden');
    clearImageBtn.hidden = true;
  }

  async function setAttachedImage(file) {
    if (!file) return;
    if (!isAllowedImageMime(file.type)) {
      alert('请选择 PNG、JPG/JPEG、WebP 或 GIF 图片。');
      return;
    }

    if (file.size > MAX_IMAGE_BYTES) {
      alert('图片太大了，请选择 10MB 以内的图片。');
      return;
    }

    const dataUrl = await readBlobAsDataUrl(file);
    const normalizedDataUrl = await normalizeDataUrlToPng(dataUrl);
    applyAttachedImage({
      name: file.name,
      type: file.type,
      size: file.size,
      dataUrl: normalizedDataUrl
    });
  }

  async function attachImageFromSource(image) {
    const loadedImage = await loadImageFromSource(image);
    applyAttachedImage(loadedImage);
    return loadedImage;
  }

  async function requestPageRegionSelection(tabId) {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => new Promise((resolve) => {
        const existing = document.getElementById('__llm_assistant_capture_overlay__');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.id = '__llm_assistant_capture_overlay__';
        overlay.style.cssText = [
          'position:fixed',
          'inset:0',
          'z-index:2147483647',
          'cursor:crosshair',
          'background:rgba(15,23,42,0.18)',
          'user-select:none'
        ].join(';');

        const hint = document.createElement('div');
        hint.textContent = '拖动选择截图区域，点击右上角取消';
        hint.style.cssText = [
          'position:fixed',
          'top:16px',
          'left:50%',
          'transform:translateX(-50%)',
          'padding:8px 12px',
          'border-radius:6px',
          'background:rgba(15,23,42,0.92)',
          'color:#fff',
          'font:13px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
          'box-shadow:0 8px 24px rgba(0,0,0,0.2)',
          'pointer-events:none'
        ].join(';');

        const selectionBox = document.createElement('div');
        selectionBox.style.cssText = [
          'position:fixed',
          'display:none',
          'border:2px solid #38bdf8',
          'background:rgba(56,189,248,0.18)',
          'box-shadow:0 0 0 99999px rgba(15,23,42,0.45)',
          'box-sizing:border-box',
          'pointer-events:none'
        ].join(';');

        overlay.appendChild(hint);
        overlay.appendChild(selectionBox);
        const cancelButton = document.createElement('button');
        cancelButton.type = 'button';
        cancelButton.textContent = '取消';
        cancelButton.style.cssText = [
          'position:fixed',
          'top:16px',
          'right:16px',
          'z-index:2147483647',
          'padding:8px 12px',
          'border:1px solid rgba(255,255,255,0.35)',
          'border-radius:6px',
          'background:rgba(15,23,42,0.92)',
          'color:#fff',
          'font:13px/1.2 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif',
          'cursor:pointer'
        ].join(';');
        overlay.appendChild(cancelButton);
        document.documentElement.appendChild(overlay);

        let startX = 0;
        let startY = 0;
        let isDragging = false;
        let resolved = false;

        function cleanup(result) {
          if (resolved) return;
          resolved = true;
          overlay.removeEventListener('mousedown', onMouseDown, true);
          overlay.removeEventListener('mousemove', onMouseMove, true);
          overlay.removeEventListener('mouseup', onMouseUp, true);
          overlay.removeEventListener('contextmenu', onContextMenu, true);
          cancelButton.removeEventListener('mousedown', onCancelButtonMouseDown, true);
          cancelButton.removeEventListener('click', onCancelButtonClick, true);
          overlay.remove();
          resolve(result);
        }

        function updateBox(currentX, currentY) {
          const left = Math.min(startX, currentX);
          const top = Math.min(startY, currentY);
          const width = Math.abs(currentX - startX);
          const height = Math.abs(currentY - startY);

          selectionBox.style.display = 'block';
          selectionBox.style.left = `${left}px`;
          selectionBox.style.top = `${top}px`;
          selectionBox.style.width = `${width}px`;
          selectionBox.style.height = `${height}px`;
        }

        function onMouseDown(event) {
          if (event.target === cancelButton) return;
          if (event.button !== 0) return;
          event.preventDefault();
          event.stopPropagation();
          hint.style.display = 'none';
          cancelButton.style.display = 'none';
          isDragging = true;
          startX = event.clientX;
          startY = event.clientY;
          updateBox(startX, startY);
        }

        function onMouseMove(event) {
          if (!isDragging) return;
          event.preventDefault();
          event.stopPropagation();
          updateBox(event.clientX, event.clientY);
        }

        function onMouseUp(event) {
          if (!isDragging) return;
          event.preventDefault();
          event.stopPropagation();
          isDragging = false;

          const left = Math.max(0, Math.min(startX, event.clientX));
          const top = Math.max(0, Math.min(startY, event.clientY));
          const width = Math.min(window.innerWidth - left, Math.abs(event.clientX - startX));
          const height = Math.min(window.innerHeight - top, Math.abs(event.clientY - startY));

          if (width < 8 || height < 8) {
            cleanup({ cancelled: true });
            return;
          }

          cleanup({
            x: left,
            y: top,
            width,
            height,
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight
          });
        }

        function onContextMenu(event) {
          event.preventDefault();
          event.stopPropagation();
          cleanup({ cancelled: true });
        }

        function onCancelButtonMouseDown(event) {
          event.preventDefault();
          event.stopPropagation();
        }

        function onCancelButtonClick(event) {
          event.preventDefault();
          event.stopPropagation();
          cleanup({ cancelled: true });
        }

        overlay.addEventListener('mousedown', onMouseDown, true);
        overlay.addEventListener('mousemove', onMouseMove, true);
        overlay.addEventListener('mouseup', onMouseUp, true);
        overlay.addEventListener('contextmenu', onContextMenu, true);
        cancelButton.addEventListener('mousedown', onCancelButtonMouseDown, true);
        cancelButton.addEventListener('click', onCancelButtonClick, true);
      })
    });

    return result;
  }

  async function cropDataUrlToSelection(dataUrl, selection) {
    const image = await loadImageElement(dataUrl);
    const imageWidth = image.naturalWidth || image.width;
    const imageHeight = image.naturalHeight || image.height;
    assertImageDimensionsSafe(imageWidth, imageHeight);

    const viewportWidth = Number(selection?.viewportWidth || 0);
    const viewportHeight = Number(selection?.viewportHeight || 0);
    if (!viewportWidth || !viewportHeight) {
      throw new Error('截图区域参数无效');
    }

    const scaleX = imageWidth / viewportWidth;
    const scaleY = imageHeight / viewportHeight;
    const sourceX = Math.max(0, Math.round(Number(selection.x || 0) * scaleX));
    const sourceY = Math.max(0, Math.round(Number(selection.y || 0) * scaleY));
    const sourceWidth = Math.max(1, Math.min(imageWidth - sourceX, Math.round(Number(selection.width || 0) * scaleX)));
    const sourceHeight = Math.max(1, Math.min(imageHeight - sourceY, Math.round(Number(selection.height || 0) * scaleY)));

    assertImageDimensionsSafe(sourceWidth, sourceHeight);

    const canvas = document.createElement('canvas');
    canvas.width = sourceWidth;
    canvas.height = sourceHeight;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('无法创建截图画布');

    context.drawImage(image, sourceX, sourceY, sourceWidth, sourceHeight, 0, 0, sourceWidth, sourceHeight);
    const croppedDataUrl = canvas.toDataURL('image/png');
    if (!isAllowedDataImageUrl(croppedDataUrl)) {
      throw new Error('裁剪后的截图超过 10MB');
    }
    return croppedDataUrl;
  }

  async function captureSelectedRegionAsImage() {
    if (!(await ensurePrivacyNoticeAccepted())) return;

    const tab = await getActiveBrowserTab();
    if (!tab?.windowId) {
      throw new Error('没有可截图的当前标签页');
    }

    await ensureCapturePermission();

    if (tab.url && isReadablePageUrl(tab.url)) {
      await ensurePageHostPermission(tab.url);
    }

    let selection = null;
    try {
      selection = await requestPageRegionSelection(tab.id);
      if (!selection) {
        throw new Error('当前页面不允许注入框选层，请使用系统截图后粘贴或上传。');
      }
    } catch (error) {
      throw new Error(error.message || '当前页面不允许注入框选层，请使用系统截图后粘贴或上传。');
    }

    if (selection?.cancelled) return;

    let dataUrl;
    try {
      dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
    } catch {
      throw new Error('无法截取当前标签页。请确认已授予截图权限，并避开 Chrome 商店页、浏览器内置页或受保护页面。');
    }
    const croppedDataUrl = await cropDataUrlToSelection(dataUrl, selection);

    await attachImageFromSource({
      dataUrl: croppedDataUrl,
      type: 'image/png',
      name: `screenshot-region-${new Date().toISOString().replace(/[:.]/g, '-')}.png`,
      size: estimateDataUrlBytes(croppedDataUrl)
    });
  }

  function getImageToolElements() {
    return {
      statusPill: document.getElementById('imageToolStatus'),
      sourceUrlInput: document.getElementById('imageSourceUrl'),
      pageUrlInput: document.getElementById('imagePageUrl'),
      fileInput: document.getElementById('imageFileInput'),
      loadBtn: document.getElementById('imageLoadBtn'),
      copyBtn: document.getElementById('imageCopyBtn'),
      downloadLink: document.getElementById('imageDownloadLink'),
      previewImg: document.getElementById('imageToolPreviewImg'),
      emptyState: document.getElementById('imageToolEmptyState'),
      metaText: document.getElementById('imageMetaText')
    };
  }

  function setImageToolStatus(text, variant = 'idle') {
    const { statusPill } = getImageToolElements();
    if (!statusPill) return;
    statusPill.textContent = text;
    statusPill.dataset.variant = variant;
  }

  function normalizeImageToolFileName(rawName, sourceUrl = '') {
    const fallbackName = deriveImageName(sourceUrl) || 'image';
    const baseName = String(rawName || '').trim() || fallbackName;
    const sanitizedBaseName = baseName.replace(/\.(png|jpe?g|webp|gif|bmp|tiff?)$/i, '');
    return `${sanitizedBaseName || fallbackName}.png`;
  }

  function resetImageToolResult() {
    imageToolCurrentDataUrl = '';
    imageToolCurrentFileName = 'image.png';

    const { previewImg, emptyState, copyBtn, downloadLink, metaText } = getImageToolElements();
    if (previewImg) {
      previewImg.removeAttribute('src');
      previewImg.hidden = true;
    }
    if (previewImg?.parentElement) previewImg.parentElement.classList.remove('has-image');
    if (emptyState) emptyState.hidden = false;
    if (copyBtn) copyBtn.disabled = true;
    if (downloadLink) {
      downloadLink.href = '#';
      downloadLink.download = 'image.png';
      downloadLink.classList.add('is-disabled');
    }
    if (metaText) metaText.textContent = '尚未加载图片';
  }

  function setImageToolResult(dataUrl, meta = {}) {
    imageToolCurrentDataUrl = dataUrl;
    if (meta.downloadName) {
      imageToolCurrentFileName = meta.downloadName;
    }

    const { previewImg, emptyState, copyBtn, downloadLink, metaText } = getImageToolElements();
    if (previewImg) {
      previewImg.hidden = false;
      previewImg.onload = () => {
        if (previewImg.parentElement) previewImg.parentElement.classList.add('has-image');
      };
      previewImg.onerror = () => {
        if (emptyState) emptyState.hidden = false;
      };
      previewImg.src = dataUrl;
    }
    if (emptyState) emptyState.hidden = true;
    if (previewImg) {
      previewImg.hidden = false;
    }
    if (previewImg?.parentElement) previewImg.parentElement.classList.add('has-image');
    if (copyBtn) copyBtn.disabled = false;
    if (downloadLink) {
      downloadLink.href = dataUrl;
      downloadLink.download = imageToolCurrentFileName;
      downloadLink.classList.remove('is-disabled');
    }

    const parts = [];
    if (meta.name) parts.push(meta.name);
    if (meta.type) parts.push(meta.type);
    if (meta.size != null) parts.push(formatFileSize(meta.size));
    if (metaText) metaText.textContent = parts.length ? parts.join(' · ') : '已生成 PNG Base64';
  }

  async function fetchImageToolDataUrl(srcUrl, pageUrl) {
    const value = String(srcUrl || '').trim();

    if (value.startsWith('data:')) {
      if (!isAllowedDataImageUrl(value)) {
        throw new Error('仅允许 10MB 以内的 PNG、JPG/JPEG、WebP 或 GIF Data URL');
      }
      return normalizeDataUrlToPng(value);
    }

    if (!isAllowedImageHttpUrl(value)) {
      throw new Error('图片 URL 仅支持 HTTP 或 HTTPS，且不能包含用户名、密码、查询过长或片段');
    }

    if (pageUrl && !isAllowedImageHttpUrl(pageUrl)) {
      throw new Error('页面 URL 仅支持 HTTP 或 HTTPS，且不能包含用户名、密码、查询过长或片段');
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 10000);

    try {
      const response = await fetch(value, {
        credentials: 'omit',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
        signal: controller.signal
      });

      if (!response.ok) {
        throw new Error(`下载失败: HTTP ${response.status}`);
      }

      const contentLength = Number(response.headers.get('content-length') || 0);
      if (contentLength && contentLength > MAX_IMAGE_BYTES) {
        throw new Error('图片太大了，请选择 10MB 以内的图片。');
      }

      const contentType = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
      if (!isAllowedImageMime(contentType)) {
        throw new Error(`返回内容不是允许的图片类型，而是 ${contentType || '未知内容'}`);
      }

      const blob = await response.blob();
      if (blob.size > MAX_IMAGE_BYTES) {
        throw new Error('图片太大了，请选择 10MB 以内的图片。');
      }

      const blobType = (blob.type || contentType).split(';')[0].trim().toLowerCase();
      if (!isAllowedImageMime(blobType)) {
        throw new Error('返回内容不是允许的图片类型');
      }

      const objectUrl = URL.createObjectURL(blob);
      try {
        const image = await loadImageElement(objectUrl);
        const width = image.naturalWidth || image.width;
        const height = image.naturalHeight || image.height;
        assertImageDimensionsSafe(width, height);

        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext('2d');
        if (!context) throw new Error('无法创建图片画布');
        context.drawImage(image, 0, 0, width, height);
        const normalizedDataUrl = canvas.toDataURL('image/png');
        if (!isAllowedDataImageUrl(normalizedDataUrl)) {
          throw new Error('转换后的图片超过 10MB');
        }
        return normalizedDataUrl;
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async function loadImageToolFromUrl() {
    const { sourceUrlInput, pageUrlInput, loadBtn } = getImageToolElements();
    const srcUrl = sourceUrlInput?.value.trim() || '';
    const pageUrl = pageUrlInput?.value.trim() || '';

    if (!srcUrl) {
      setImageToolStatus('请输入图片 URL', 'warn');
      return;
    }

    if (srcUrl.startsWith('data:')) {
      if (!(await ensurePrivacyNoticeAccepted())) return;
      setImageToolStatus('正在转换 Data URL...', 'busy');
      if (loadBtn) loadBtn.disabled = true;

      try {
        const dataUrl = await fetchImageToolDataUrl(srcUrl, pageUrl);
        setImageToolResult(dataUrl, {
          name: 'data-url-image',
          type: 'image/png',
          downloadName: 'data-url-image.png'
        });
        setImageToolStatus('转换完成', 'ok');
      } catch (error) {
        resetImageToolResult();
        setImageToolStatus(error.message || '转换失败', 'error');
      } finally {
        if (loadBtn) loadBtn.disabled = false;
      }
      return;
    }

    if (!isAllowedImageHttpUrl(srcUrl)) {
      setImageToolStatus('图片 URL 仅支持 HTTP 或 HTTPS', 'warn');
      return;
    }

    if (pageUrl && !isAllowedImageHttpUrl(pageUrl)) {
      setImageToolStatus('页面 URL 仅支持 HTTP 或 HTTPS', 'warn');
      return;
    }

    if (!(await confirmRiskyImageUrl(srcUrl))) {
      setImageToolStatus('已取消加载图片 URL', 'idle');
      return;
    }

    try {
      await ensureImageHostPermission(srcUrl);
    } catch (error) {
      setImageToolStatus(error.message || '站点授权失败', 'error');
      return;
    }

    if (!(await ensurePrivacyNoticeAccepted())) return;

    setImageToolStatus('正在下载并转换...', 'busy');
    if (loadBtn) loadBtn.disabled = true;

    try {
      const dataUrl = await fetchImageToolDataUrl(srcUrl, pageUrl);
      const sourceName = deriveImageName(srcUrl) || 'image.png';
      const downloadName = normalizeImageToolFileName(sourceName, srcUrl);
      setImageToolResult(dataUrl, {
        name: sourceName,
        type: 'image/png',
        downloadName
      });
      setImageToolStatus('转换完成', 'ok');
    } catch (error) {
      resetImageToolResult();
      setImageToolStatus(error.message || '转换失败', 'error');
    } finally {
      if (loadBtn) loadBtn.disabled = false;
    }
  }

  async function loadImageToolFromFile(file) {
    if (!file) return;
    if (!isAllowedImageMime(file.type)) {
      setImageToolStatus('请选择 PNG、JPG/JPEG、WebP 或 GIF 图片', 'warn');
      return;
    }

    if (file.size > MAX_IMAGE_BYTES) {
      setImageToolStatus('图片太大了，请选择 10MB 以内的图片。', 'warn');
      return;
    }

    const { loadBtn } = getImageToolElements();
    setImageToolStatus('正在读取本地图片...', 'busy');
    if (loadBtn) loadBtn.disabled = true;

    try {
      const fileDataUrl = await readBlobAsDataUrl(file);
      const normalizedDataUrl = await normalizeDataUrlToPng(fileDataUrl);
      setImageToolResult(normalizedDataUrl, {
        name: file.name,
        type: 'image/png',
        size: file.size,
        downloadName: normalizeImageToolFileName(file.name)
      });
      setImageToolStatus('本地图片转换完成', 'ok');
    } catch (error) {
      resetImageToolResult();
      setImageToolStatus(error.message || '转换失败', 'error');
    } finally {
      if (loadBtn) loadBtn.disabled = false;
    }
  }

  async function copyImageToolResult() {
    if (!imageToolCurrentDataUrl) return;

    const { copyBtn } = getImageToolElements();
    try {
      await navigator.clipboard.writeText(imageToolCurrentDataUrl);
      setImageToolStatus('Base64 已复制', 'ok');
      if (copyBtn) {
        copyBtn.textContent = '已复制';
        setTimeout(() => {
          copyBtn.textContent = '复制 Base64';
        }, 1200);
      }
    } catch {
      const fallbackInput = document.createElement('textarea');
      fallbackInput.value = imageToolCurrentDataUrl;
      fallbackInput.setAttribute('readonly', 'readonly');
      fallbackInput.className = 'clipboard-fallback';
      document.body.appendChild(fallbackInput);
      fallbackInput.select();
      document.execCommand('copy');
      fallbackInput.remove();
      setImageToolStatus('已复制到剪贴板', 'ok');
    }
  }

  function clearImageTool() {
    const { sourceUrlInput, pageUrlInput, fileInput } = getImageToolElements();
    if (sourceUrlInput) sourceUrlInput.value = '';
    if (pageUrlInput) pageUrlInput.value = '';
    if (fileInput) fileInput.value = '';
    resetImageToolResult();
    setImageToolStatus('等待输入', 'idle');
  }

  function activateTab(target) {
    document.querySelectorAll('.tab-btn').forEach((button) => {
      button.classList.toggle('active', button.dataset.target === target);
    });

    document.querySelectorAll('.tab-content').forEach((content) => {
      content.classList.toggle('active', content.id === target);
    });
  }

  function waitForNextFrame() {
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
  }

  function setChatInputText(text) {
    const input = document.getElementById('chatInput');
    if (!input) return;

    input.value = String(text || '');
    input.focus();
  }

  function validatePendingAction(action) {
    if (!action || typeof action !== 'object') return false;
    if (!['AUTO_SEND_PROMPT', 'AUTO_IMAGE_TOOL'].includes(action.type)) return false;
    if (typeof action.actionId !== 'string' || !action.actionId) return false;
    if (action.source !== 'context_menu' || action.userGesture !== true) return false;

    const createdAt = Number(action.createdAt || 0);
    if (!Number.isFinite(createdAt) || Math.abs(Date.now() - createdAt) > MAX_ACTION_AGE_MS) return false;

    if (action.type === 'AUTO_SEND_PROMPT') {
      return ['explain', 'translate'].includes(String(action.taskType || '').trim())
        && typeof action.focusText === 'string'
        && action.focusText.trim().length > 0
        && action.focusText.length <= MAX_PROMPT_LENGTH;
    }

    const image = action.image || {};
    const srcUrl = String(image.srcUrl || '').trim();
    const pageUrl = String(image.pageUrl || '').trim();
    if (!srcUrl) return false;
    if (srcUrl.startsWith('data:')) {
      if (!isAllowedDataImageUrl(srcUrl)) return false;
    } else if (!isAllowedImageHttpUrl(srcUrl)) {
      return false;
    }

    return !pageUrl || isAllowedImageHttpUrl(pageUrl);
  }

  function confirmPendingAction(action) {
    if (action.type === 'AUTO_SEND_PROMPT') {
      const taskTypeLabel = getTaskTypeLabel(action.taskType);
      return confirm(`即将把右键选中的文本带入为“${taskTypeLabel}”任务，是否继续？`);
    }

    return confirm('即将加载外部图片 URL 并转换为 Base64，是否继续？');
  }

  async function handleAutoSendPromptAction(action) {
    activateTab('chat');
    await waitForNextFrame();
    setTaskState(action.taskType, action.focusText, 'context_menu');
    setChatInputText('');
    return true;
  }

  async function handleAutoImageToolAction(action) {
    activateTab('imageTool');
    await waitForNextFrame();

    const { sourceUrlInput, pageUrlInput } = getImageToolElements();
    const image = action.image || {};
    if (sourceUrlInput) sourceUrlInput.value = image.srcUrl || '';
    if (pageUrlInput) pageUrlInput.value = image.pageUrl || '';

    imageToolCurrentFileName = normalizeImageToolFileName(image.title || '', image.srcUrl || '');

    if (image.srcUrl) {
      resetImageToolResult();
      setImageToolStatus('已填入图片 URL，请点击“下载并转 Base64”授权并转换', 'idle');
      return true;
    }

    resetImageToolResult();
    setImageToolStatus('等待输入', 'idle');
    return true;
  }

  async function handlePendingAction(action) {
    if (!validatePendingAction(action)) return false;
    if (handledAutoSendActionIds.has(action.actionId)) return true;
    handledAutoSendActionIds.add(action.actionId);
    if (!confirmPendingAction(action)) return false;

    if (action.type === 'AUTO_SEND_PROMPT') return handleAutoSendPromptAction(action);
    if (action.type === 'AUTO_IMAGE_TOOL') return handleAutoImageToolAction(action);
    return false;
  }

  // 1. Tab 切换逻辑
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      activateTab(e.currentTarget.dataset.target);
    });
  });

  // 2. 加载与保存设置
  const config = await chrome.storage.local.get(['apiUrl', 'modelName']);
  const storedCredential = await getStoredApiCredential();
  const apiUrlInput = document.getElementById('apiUrl');
  const apiKeyInput = document.getElementById('apiKey');
  const modelNameInput = document.getElementById('modelName');
  const apiKeyStatus = document.getElementById('apiKeyStatus');
  const saveMsg = document.getElementById('saveMsg');

  function updateApiKeyStatus(hasKey, apiKeyUrl = '') {
    apiKeyInput.value = '';
    apiKeyInput.placeholder = hasKey ? '已保存 API Key，留空则保留' : 'sk-...';
    if (apiKeyStatus) {
      apiKeyStatus.textContent = hasKey
        ? `已保存 API Key；页面不会显示明文。适用地址：${apiKeyUrl || '当前 API 地址'}`
        : '尚未保存 API Key。';
    }
  }

  function showSettingsMessage(text, variant) {
    saveMsg.textContent = text;
    saveMsg.className = variant === 'error' ? 'settings-message-error' : 'settings-message-ok';
    setTimeout(() => {
      saveMsg.textContent = '';
      saveMsg.className = '';
    }, 2500);
  }

  apiUrlInput.value = config.apiUrl || DEFAULT_API_URL;
  modelNameInput.value = config.modelName || 'gpt-3.5-turbo';
  updateApiKeyStatus(Boolean(storedCredential.apiKey), storedCredential.apiKeyApiUrl);

  document.getElementById('saveSettingsBtn').addEventListener('click', async () => {
    const apiUrl = apiUrlInput.value.replace(/\/$/, '');
    const enteredApiKey = apiKeyInput.value.trim();
    const modelName = modelNameInput.value.trim() || 'gpt-3.5-turbo';
    const savedCredential = await getStoredApiCredential();
    const effectiveApiKey = enteredApiKey || savedCredential.apiKey || '';

    let safeApiUrl;
    try {
      safeApiUrl = normalizeApiBaseUrl(apiUrl);
      const isLocalApi = isPrivateOrLocalHost(new URL(safeApiUrl).hostname);
      if (!isLocalApi && !enteredApiKey && savedCredential.apiKeyApiUrl && savedCredential.apiKeyApiUrl !== safeApiUrl) {
        throw new Error('切换 API 地址时请重新输入该服务对应的 API Key');
      }
      if (!isLocalApi && !DEFAULT_API_BASE_URLS.has(safeApiUrl) && !enteredApiKey && savedCredential.apiKeyApiUrl !== safeApiUrl) {
        throw new Error('添加自定义 API 地址时请同时输入该服务对应的 API Key');
      }
      await ensureCustomApiBaseUrlAllowed(safeApiUrl);
      safeApiUrl = await validateOpenAIApiConfig(safeApiUrl, effectiveApiKey);
    } catch (error) {
      showSettingsMessage(error.message || '配置无效', 'error');
      return;
    }

    if (enteredApiKey) {
      await chrome.storage.session.set({ apiKey: enteredApiKey, apiKeyApiUrl: safeApiUrl });
    }

    await chrome.storage.local.set({ apiUrl: safeApiUrl, modelName });
    apiUrlInput.value = safeApiUrl;
    updateApiKeyStatus(Boolean(enteredApiKey || effectiveApiKey), safeApiUrl);
    showSettingsMessage('保存成功！', 'ok');
  });

  document.getElementById('clearApiKeyBtn')?.addEventListener('click', async () => {
    if (!confirm('确认清除已保存的 API Key？')) return;
    await Promise.all([
      chrome.storage.session.remove(['apiKey', 'apiKeyApiUrl']),
      chrome.storage.local.remove(['apiKey'])
    ]);
    updateApiKeyStatus(false);
    showSettingsMessage('API Key 已清除', 'ok');
  });

  // 3. UI 辅助函数
  function createMessageNode(role) {
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = role === 'user' ? '🧑' : '🤖';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    msgDiv.append(avatar, bubble);
    document.getElementById('chatHistory').appendChild(msgDiv);
    return bubble;
  }

  function showTypingIndicator(container) {
    container.textContent = '';
    const indicator = document.createElement('div');
    indicator.className = 'typing-indicator';
    for (let index = 0; index < 3; index += 1) {
      const dot = document.createElement('div');
      dot.className = 'dot';
      indicator.appendChild(dot);
    }
    container.appendChild(indicator);
  }

  function setRenderedMarkdown(container, markdown) {
    const template = document.createElement('template');
    template.innerHTML = renderMarkdown(markdown);
    container.replaceChildren(template.content.cloneNode(true));
  }

  function extractSourceIds(text) {
    return String(text || '')
      .split(/[\s,，]+/)
      .map((token) => token.trim().toUpperCase())
      .filter((token) => SOURCE_ID_PATTERN.test(token));
  }

  function normalizeSourceCitationText(text, sources) {
    const rawText = String(text || '');
    if (!rawText) {
      return '';
    }

    const validIds = new Set(
      Array.isArray(sources)
        ? sources
          .map((source) => String(source?.source_id || '').trim().toUpperCase())
          .filter(Boolean)
        : []
    );

    return rawText.replace(SOURCE_BLOCK_PATTERN, (match, innerText) => {
      const parsedIds = extractSourceIds(innerText);
      if (!parsedIds.length) {
        return match;
      }

      const normalizedIds = validIds.size
        ? parsedIds.filter((sourceId) => validIds.has(sourceId))
        : parsedIds;

      if (!normalizedIds.length) {
        return '';
      }

      return `[${normalizedIds.join(', ')}]`;
    });
  }

  function buildCitationGroup(sourceIds) {
    const group = document.createElement('span');
    group.className = 'source-citation-group';
    group.append('[');

    sourceIds.forEach((sourceId, index) => {
      if (index > 0) {
        group.append(', ');
      }

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'source-citation';
      button.dataset.sourceId = sourceId;
      button.textContent = sourceId;
      group.appendChild(button);
    });

    group.append(']');
    return group;
  }

  function buildSourceHeaderText(source) {
    const sourceId = source?.source_id || '?';
    const sourceTitle = String(source?.title || source?.type || '来源片段').trim();
    return `[${sourceId}] ${sourceTitle}`;
  }

  function buildSourceMetaText(source) {
    const parts = [];
    const url = String(source?.url || '').trim();
    if (url) {
      parts.push(url);
    }
    if (typeof source?.score === 'number' && Number.isFinite(source.score)) {
      parts.push(`score=${source.score.toFixed(4)}`);
    }
    return parts.join(' · ');
  }

  function decorateSourceCitations(container) {
    const textNodes = [];
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.textContent || !node.textContent.includes('[')) {
          return NodeFilter.FILTER_REJECT;
        }

        const parent = node.parentElement;
        if (!parent || parent.closest('code, pre, a, button, .message-sources, .katex')) {
          return NodeFilter.FILTER_REJECT;
        }

        return NodeFilter.FILTER_ACCEPT;
      }
    });

    while (walker.nextNode()) {
      textNodes.push(walker.currentNode);
    }

    textNodes.forEach((node) => {
      const text = node.textContent;
      const matches = Array.from(text.matchAll(SOURCE_BLOCK_PATTERN));
      if (!matches.length) {
        return;
      }

      const fragment = document.createDocumentFragment();
      let lastIndex = 0;
      let hasDecorated = false;

      matches.forEach((match) => {
        const matchIndex = match.index ?? 0;
        if (matchIndex > lastIndex) {
          fragment.append(text.slice(lastIndex, matchIndex));
        }

        const sourceIds = extractSourceIds(match[1]);
        if (sourceIds.length) {
          fragment.append(buildCitationGroup(sourceIds));
          hasDecorated = true;
        } else {
          fragment.append(match[0]);
        }

        lastIndex = matchIndex + match[0].length;
      });

      if (!hasDecorated) {
        return;
      }

      if (lastIndex < text.length) {
        fragment.append(text.slice(lastIndex));
      }

      node.replaceWith(fragment);
    });
  }

  function buildSourceSummaryPreview(source) {
    const previewText = String(source?.preview || source?.content || '').replace(/\s+/g, ' ').trim();
    return summarizeInlineText(previewText, '无预览', 56);
  }

  function setSourcePanelOpen(container, isOpen) {
    const wrapper = container.querySelector('.message-sources');
    const toggleButton = container.querySelector('.message-sources-toggle');
    const body = container.querySelector('.message-sources-body');
    if (!wrapper || !toggleButton || !body) {
      return;
    }

    wrapper.classList.toggle('is-open', isOpen);
    toggleButton.classList.toggle('is-open', isOpen);
    toggleButton.setAttribute('aria-expanded', String(isOpen));
    body.hidden = !isOpen;

    if (isOpen) {
      if (activeSourceContainer && activeSourceContainer !== container) {
        closeSourcePanel(activeSourceContainer);
      }
      activeSourceContainer = container;
      return;
    }

    if (activeSourceContainer === container) {
      activeSourceContainer = null;
    }
  }

  function closeSourcePanel(container) {
    if (!container) {
      return;
    }

    container.querySelectorAll('.source-citation.is-active, .message-source-summary.is-active, .message-source-item.is-active')
      .forEach((element) => element.classList.remove('is-active'));

    const detailPanel = container.querySelector('.message-source-detail');
    if (detailPanel) {
      detailPanel.hidden = true;
    }

    container.querySelectorAll('.message-source-item').forEach((item) => {
      item.hidden = true;
    });

    setSourcePanelOpen(container, false);
  }

  function setActiveSourceState(container, sourceIds, options = {}) {
    const uniqueSourceIds = Array.from(
      new Set(
        sourceIds
          .map((sourceId) => String(sourceId || '').trim().toUpperCase())
          .filter(Boolean)
      )
    );

    container.querySelectorAll('.source-citation.is-active, .message-source-summary.is-active, .message-source-item.is-active')
      .forEach((element) => element.classList.remove('is-active'));

    const detailPanel = container.querySelector('.message-source-detail');
    const sourceCards = Array.from(container.querySelectorAll('.message-source-item'));
    sourceCards.forEach((item) => {
      item.hidden = true;
    });

    if (!uniqueSourceIds.length) {
      if (detailPanel) {
        detailPanel.hidden = true;
      }
      if (options.keepPanelOpen) {
        setSourcePanelOpen(container, true);
      } else {
        closeSourcePanel(container);
      }
      return;
    }

    setSourcePanelOpen(container, true);

    const activeSourceId = uniqueSourceIds[0];
    const citationButtons = Array.from(container.querySelectorAll('.source-citation'))
      .filter((button) => activeSourceId === (button.dataset.sourceId || ''));
    const summaryButtons = Array.from(container.querySelectorAll('.message-source-summary'))
      .filter((button) => activeSourceId === (button.dataset.sourceId || ''));
    const activeSourceCard = sourceCards.find((item) => activeSourceId === (item.dataset.sourceId || ''));

    citationButtons.forEach((button) => button.classList.add('is-active'));
    summaryButtons.forEach((button) => button.classList.add('is-active'));

    if (!detailPanel || !activeSourceCard) {
      return;
    }

    detailPanel.hidden = false;
    activeSourceCard.hidden = false;
    activeSourceCard.classList.add('is-active');

    if (options.scrollToCard) {
      detailPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  function bindSourceInteractions(container) {
    if (container.dataset.sourceInteractionsBound === 'true') {
      return;
    }

    container.dataset.sourceInteractionsBound = 'true';
    container.addEventListener('click', (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) {
        return;
      }

      const citation = target.closest('.source-citation');
      if (citation && container.contains(citation)) {
        const sourceId = citation.dataset.sourceId || '';
        const detailPanel = container.querySelector('.message-source-detail');
        const isActive = citation.classList.contains('is-active') && detailPanel && !detailPanel.hidden;
        setActiveSourceState(container, isActive ? [] : [sourceId], { keepPanelOpen: true, scrollToCard: true });
        return;
      }

      const sourceToggle = target.closest('.message-sources-toggle');
      if (sourceToggle && container.contains(sourceToggle)) {
        const body = container.querySelector('.message-sources-body');
        if (body && !body.hidden) {
          closeSourcePanel(container);
        } else {
          setActiveSourceState(container, [], { keepPanelOpen: true });
        }
        return;
      }

      const summaryButton = target.closest('.message-source-summary');
      if (summaryButton && container.contains(summaryButton)) {
        const sourceId = summaryButton.dataset.sourceId || '';
        const detailPanel = container.querySelector('.message-source-detail');
        const isActive = summaryButton.classList.contains('is-active') && detailPanel && !detailPanel.hidden;
        setActiveSourceState(container, isActive ? [] : [sourceId], { keepPanelOpen: true, scrollToCard: true });
        return;
      }

      const closeButton = target.closest('.message-source-close');
      if (closeButton && container.contains(closeButton)) {
        setActiveSourceState(container, [], { keepPanelOpen: true });
      }
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !activeSourceContainer) {
      return;
    }
    closeSourcePanel(activeSourceContainer);
  });

  function renderCitedSources(container, sources) {
    const existing = container.querySelector('.message-sources');
    if (existing) {
      existing.remove();
    }
    if (activeSourceContainer === container) {
      activeSourceContainer = null;
    }

    if (!Array.isArray(sources) || !sources.length) {
      return;
    }

    const wrapper = document.createElement('div');
    wrapper.className = 'message-sources';

    const toggleButton = document.createElement('button');
    toggleButton.type = 'button';
    toggleButton.className = 'message-sources-toggle';
    toggleButton.setAttribute('aria-expanded', 'false');
    toggleButton.textContent = `参考依据 (${sources.length})`;
    wrapper.appendChild(toggleButton);

    const body = document.createElement('div');
    body.className = 'message-sources-body';
    body.hidden = true;

    const summaryList = document.createElement('div');
    summaryList.className = 'message-source-summary-list';

    sources.forEach((source) => {
      const summaryButton = document.createElement('button');
      summaryButton.type = 'button';
      summaryButton.className = 'message-source-summary';
      summaryButton.dataset.sourceId = String(source.source_id || '').trim().toUpperCase();

      const summaryHeader = document.createElement('div');
      summaryHeader.className = 'message-source-header';
      summaryHeader.textContent = buildSourceHeaderText(source);

      const summaryMeta = document.createElement('div');
      summaryMeta.className = 'message-source-meta';
      summaryMeta.textContent = buildSourceMetaText(source) || buildSourceSummaryPreview(source);

      const summaryPreview = document.createElement('div');
      summaryPreview.className = 'message-source-summary-preview';
      summaryPreview.textContent = buildSourceSummaryPreview(source);

      summaryButton.append(summaryHeader, summaryMeta, summaryPreview);
      summaryList.appendChild(summaryButton);
    });

    body.appendChild(summaryList);

    const detailPanel = document.createElement('div');
    detailPanel.className = 'message-source-detail';
    detailPanel.hidden = true;

    const head = document.createElement('div');
    head.className = 'message-source-detail-head';

    const detailTitle = document.createElement('div');
    detailTitle.className = 'message-source-detail-title';
    detailTitle.textContent = '引用内容';

    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'message-source-close';
    closeButton.textContent = '收起';

    head.append(detailTitle, closeButton);
    detailPanel.appendChild(head);

    sources.forEach((source) => {
      const item = document.createElement('div');
      item.className = 'message-source-item';
      item.dataset.sourceId = String(source.source_id || '').trim().toUpperCase();
      item.hidden = true;

      const header = document.createElement('div');
      header.className = 'message-source-header';
      header.textContent = buildSourceHeaderText(source);

      const meta = document.createElement('div');
      meta.className = 'message-source-meta';
      meta.textContent = buildSourceMetaText(source);

      const preview = document.createElement('div');
      preview.className = 'message-source-preview';
      preview.textContent = source.content || source.preview || '';

      item.appendChild(header);
      if (meta.textContent) {
        item.appendChild(meta);
      }
      item.appendChild(preview);
      detailPanel.appendChild(item);
    });

    body.appendChild(detailPanel);
    wrapper.appendChild(body);
    container.appendChild(wrapper);
  }

  function scrollToBottom() {
    const chatHistory = document.getElementById('chatHistory');
    chatHistory.scrollTo({ top: chatHistory.scrollHeight, behavior: 'smooth' });
  }

  function showCaptureBanner(text) {
    const chatHistory = document.getElementById('chatHistory');
    const banner = document.createElement('div');
    banner.className = 'capture-warning-banner';
    banner.textContent = text;
    chatHistory.appendChild(banner);
    scrollToBottom();
  }

  // 4. 发送与流式接收核心逻辑
  async function handleSend() {
    const input = document.getElementById('chatInput');
    const queryText = input.value.trim();
    const hasImage = Boolean(attachedImage);
    const taskType = hasImage ? 'chat' : normalizeTaskType(taskState.taskType);
    const focusText = hasImage ? '' : String(taskState.focusText || '').trim();
    const defaultImagePrompt = '请帮我分析这张图片并给出关键信息。';
    const chatText = queryText || (hasImage ? defaultImagePrompt : '');

    if (hasImage && taskState.taskType !== 'chat') {
      alert('当前翻译或解释任务暂不支持图片附件，请先清空选中文本或移除图片。');
      return;
    }
    if (taskType === 'chat' && !chatText && !hasImage) return;
    if ((taskType === 'explain' || taskType === 'translate') && !focusText) {
      alert('当前任务需要先提供一段选中文本。');
      return;
    }
    if (queryText.length > MAX_PROMPT_LENGTH) {
      alert(`单次发送文本不能超过 ${MAX_PROMPT_LENGTH} 字。`);
      return;
    }
    if (pageContextState.enabled && hasImage) {
      alert('当前网页上下文只支持文本提问，请先移除图片附件。');
      return;
    }

    let apiKey = '';
    let modelName = '';
    let safeApiUrl = '';
    try {
      ({ apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig());
    } catch (error) {
      alert(error.message || 'API 配置无效');
      return;
    }
    if (!(await ensurePrivacyNoticeAccepted())) return;

    const pageContextResult = await resolvePageContextForSend();
    if (!pageContextResult) return;
    const currentPage = pageContextResult.currentPage;

    const safeModelName = String(modelName || '').trim() || 'gpt-3.5-turbo';
    input.value = '';

    // 绘制用户消息
    const userBubble = createMessageNode('user');
    if (!hasImage && taskType !== 'chat') {
      renderUserTaskSummary(userBubble, taskType, focusText, queryText);
    } else if (chatText) {
      const userTextNode = document.createElement('div');
      userTextNode.textContent = chatText;
      userBubble.appendChild(userTextNode);
    }

    if (hasImage) {
      const previewImage = document.createElement('img');
      previewImage.className = 'user-upload-preview';
      previewImage.src = attachedImage.dataUrl;
      previewImage.alt = attachedImage.name || '上传图片';
      userBubble.appendChild(previewImage);
    }
    scrollToBottom();

    // 推入上下文记忆
    const userMessage = {
      role: 'user',
      content: hasImage
        ? [
            { type: 'text', text: chatText || defaultImagePrompt },
            { type: 'image_url', image_url: { url: attachedImage.dataUrl } }
          ]
        : buildConversationUserContent(taskType, focusText, queryText)
    };
    conversationHistory.push(userMessage);

    if (hasImage) {
      clearAttachedImage();
    }

    // 创建 AI 等待气泡
    const aiBubble = createMessageNode('ai');
    bindSourceInteractions(aiBubble);
    showTypingIndicator(aiBubble);
    scrollToBottom();

    // 构造带历史记录的请求数据
    const messagesPayload = buildMessagesPayload(conversationHistory);
    conversationHistory = compactConversationHistory(conversationHistory);

    const msgId = createMessageId(); // 唯一请求ID
    const chatId = await getOrCreateCurrentChatId();
    let fullReply = ''; // 用于拼接流式文本
    let citedSources = [];
    let isStreamDone = false;
    let finalizeTimer = null;
    const requestBody = {
      model: safeModelName,
      messages: messagesPayload,
      stream: true,
      task_type: taskType,
      focus_text: focusText,
      query_text: queryText,
      chat_id: chatId,
      use_current_page: pageContextState.enabled,
      force_refresh_page: Boolean(pageContextState.enabled && pageContextState.forceRefreshPage)
    };
    if (pageContextResult.pageContextId) {
      requestBody.page_context_id = pageContextResult.pageContextId;
    }
    if (currentPage) {
      requestBody.current_page = currentPage;
    }
    if (requestBody.force_refresh_page) {
      pageContextState.forceRefreshPage = false;
      updatePageContextUi();
    }

    const requestHeaders = {
      'Content-Type': 'application/json'
    };
    if (String(apiKey || '').trim()) {
      requestHeaders.Authorization = `Bearer ${String(apiKey).trim()}`;
    }

    // 向 background.js 发出流式请求指令
    chrome.runtime.sendMessage({
      type: 'CALL_LLM_STREAM',
      msgId: msgId,
      url: `${safeApiUrl}/chat/completions`,
      options: {
        method: 'POST',
        headers: requestHeaders,
        body: JSON.stringify(requestBody)
      }
    });

    const finalizeAssistantResponse = () => {
      if (finalizeTimer) {
        clearTimeout(finalizeTimer);
        finalizeTimer = null;
      }
      if (!fullReply) {
        aiBubble.textContent = '响应为空。';
      }
      conversationHistory.push({ role: 'assistant', content: fullReply });
      conversationHistory = compactConversationHistory(conversationHistory);
      chrome.runtime.onMessage.removeListener(messageListener);
    };

    // 监听后台传回的字元块
    const messageListener = (msg) => {
      if (msg.msgId !== msgId) return; // 过滤非本次请求的流
      
      if (msg.type === 'LLM_CHUNK') {
        fullReply += msg.chunk;
        setRenderedMarkdown(aiBubble, fullReply);
        enhanceCodeBlocks(aiBubble);
        renderMathInContainer(aiBubble);
        decorateSourceCitations(aiBubble);
        if (citedSources.length) {
          renderCitedSources(aiBubble, citedSources);
        }
        scrollToBottom();
      }
      else if (msg.type === 'LLM_SOURCES') {
        citedSources = Array.isArray(msg.sources) ? msg.sources : [];
        fullReply = normalizeSourceCitationText(fullReply, citedSources);
        setRenderedMarkdown(aiBubble, fullReply);
        enhanceCodeBlocks(aiBubble);
        renderMathInContainer(aiBubble);
        decorateSourceCitations(aiBubble);
        renderCitedSources(aiBubble, citedSources);
        scrollToBottom();
      }
      else if (msg.type === 'LLM_DONE') {
        if (isStreamDone) return;
        isStreamDone = true;
        finalizeTimer = setTimeout(finalizeAssistantResponse, 150);
      } 
      else if (msg.type === 'LLM_ERROR') {
        if (finalizeTimer) {
          clearTimeout(finalizeTimer);
          finalizeTimer = null;
        }
        aiBubble.textContent = '';
        const errorSpan = document.createElement('span');
        errorSpan.className = 'error-text';
        errorSpan.textContent = `⚠️ 错误: ${msg.error}`;
        aiBubble.appendChild(errorSpan);
        chrome.runtime.onMessage.removeListener(messageListener);
      }
    };
    chrome.runtime.onMessage.addListener(messageListener);
  }

  // 5. 绑定各种交互事件
  updateTaskUi();
  updatePageContextUi();
  getOrCreateCurrentChatId().catch(console.error);

  document.getElementById('useCurrentPageToggle')?.addEventListener('change', async (event) => {
    pageContextState.enabled = Boolean(event.currentTarget?.checked);
    if (!pageContextState.enabled) {
      resetPageContextState();
      return;
    }

    pageContextState.lastError = '';
    updatePageContextUi();
    await refreshPageContextSnapshot();
  });

  document.getElementById('lockCurrentPageToggle')?.addEventListener('change', async (event) => {
    if (!pageContextState.enabled) {
      updatePageContextUi();
      return;
    }

    const shouldLock = Boolean(event.currentTarget?.checked);
    if (!shouldLock) {
      pageContextState.locked = false;
      pageContextState.pageContextId = '';
      updatePageContextUi();
      return;
    }

    if (!pageContextState.snapshot) {
      const snapshot = await refreshPageContextSnapshot();
      if (!snapshot) {
        pageContextState.locked = false;
        updatePageContextUi();
        return;
      }
    }

    const validationError = getPageContextValidationError(pageContextState.snapshot);
    if (validationError) {
      alert(validationError);
      pageContextState.locked = false;
      updatePageContextUi();
      return;
    }

    pageContextState.locked = true;
    pageContextState.pageContextId = `pagectx_${createMessageId()}`;
    updatePageContextUi();
  });

  document.getElementById('refreshPageContextBtn')?.addEventListener('click', async () => {
    if (!pageContextState.enabled) return;

    const snapshot = await refreshPageContextIndexNow();
    if (!snapshot) return;

    const validationError = getPageContextValidationError(snapshot);
    if (validationError) {
      alert(validationError);
    }
  });

  document.getElementById('clearPageSnapshotBtn')?.addEventListener('click', () => {
    clearPageContextSnapshot();
  });

  document.getElementById('toggleTaskDrawerBtn')?.addEventListener('click', () => {
    drawerState.taskOpen = !drawerState.taskOpen;
    updateTaskUi();
  });

  document.getElementById('togglePageContextDrawerBtn')?.addEventListener('click', () => {
    drawerState.pageContextOpen = !drawerState.pageContextOpen;
    updatePageContextUi();
  });

  document.getElementById('clearFocusTextBtn')?.addEventListener('click', () => {
    resetTaskState();
  });

  document.querySelectorAll('.task-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const nextTaskType = normalizeTaskType(btn.dataset.taskType);
      if (!taskState.focusText) {
        alert('请先通过右键划词提供一段选中文本。');
        return;
      }
      setTaskState(nextTaskType, taskState.focusText, taskState.source);
      document.getElementById('chatInput')?.focus();
    });
  });

  document.getElementById('sendBtn').addEventListener('click', handleSend);
  document.getElementById('chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  });

  document.getElementById('uploadImageBtn').addEventListener('click', () => {
    document.getElementById('imageInput').click();
  });

  document.getElementById('captureVisibleTabBtn').addEventListener('click', async () => {
    const captureButton = document.getElementById('captureVisibleTabBtn');
    captureButton.disabled = true;
    try {
      await captureSelectedRegionAsImage();
    } catch (error) {
      showCaptureBanner(error.message || '当前页面不允许注入框选层，请使用系统截图后粘贴或上传。');
    } finally {
      captureButton.disabled = false;
    }
  });

  document.getElementById('imageInput').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    try {
      await setAttachedImage(file);
    } catch (error) {
      alert(error.message || '图片读取失败');
      clearAttachedImage();
    }
  });

  document.getElementById('clearImageBtn').addEventListener('click', clearAttachedImage);

  document.getElementById('imageLoadBtn').addEventListener('click', loadImageToolFromUrl);
  document.getElementById('imageCopyBtn').addEventListener('click', copyImageToolResult);
  document.getElementById('imageClearBtn').addEventListener('click', clearImageTool);
  document.getElementById('imageFileInput').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    try {
      await loadImageToolFromFile(file);
    } catch (error) {
      alert(error.message || '图片读取失败');
      clearImageTool();
    }
  });

  document.getElementById('imageSourceUrl').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      loadImageToolFromUrl();
    }
  });

  document.getElementById('chatInput').addEventListener('paste', async (event) => {
    const pastedText = event.clipboardData?.getData('text/plain') || '';
    const normalizedDataUrl = pastedText.trim().replace(/\s+/g, '');
    if (!isAllowedDataImageUrl(normalizedDataUrl)) return;
    const dataUrlMatch = normalizedDataUrl.match(/^data:([^;]+);base64,/i);

    event.preventDefault();
    try {
      await attachImageFromSource({
        dataUrl: normalizedDataUrl,
        type: dataUrlMatch[1],
        name: 'pasted-image'
      });
    } catch (error) {
      alert(error.message || '无法识别粘贴的图片 Base64');
    }
  });

  // 清空对话只开启新 chat，保留当前网页快照和锁定状态。
  document.getElementById('clearChatBtn')?.addEventListener('click', async () => {
    document.getElementById('chatHistory').replaceChildren();
    conversationHistory = []; // 清除记忆！
    document.getElementById('chatInput').value = '';
    clearAttachedImage();
    await resetCurrentChatId();
  });

  // 快捷指令填入
  document.querySelectorAll('.prompt-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      setChatInputText(btn.dataset.prompt || '');
    });
  });

  function isReadablePageUrl(url) {
    try {
      return ['http:', 'https:'].includes(new URL(url).protocol);
    } catch {
      return false;
    }
  }

  function looksSensitivePageUrl(url) {
    try {
      const hostname = new URL(url).hostname.toLowerCase();
      return [
        'mail.google.com',
        'docs.google.com',
        'drive.google.com',
        'slack.com',
        'notion.so'
      ].some((domain) => hostname === domain || hostname.endsWith(`.${domain}`))
        || /(bank|account|admin|console|dashboard|intranet|internal)/i.test(hostname);
    } catch {
      return false;
    }
  }

  // 接收右键划词传来的文本
  chrome.runtime.onMessage.addListener((msg, sender) => {
    if (sender?.id && sender.id !== chrome.runtime.id) return;

    if (msg.type === 'AUTO_SEND_PROMPT' || msg.type === 'AUTO_IMAGE_TOOL') {
      handlePendingAction(msg).finally(() => {
        chrome.storage.session.remove('pendingSidePanelAction');
      });
    }
  });

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== 'session') return;

    const pendingChange = changes.pendingSidePanelAction;
    if (!pendingChange?.newValue) return;

    handlePendingAction(pendingChange.newValue).finally(() => {
      chrome.storage.session.remove('pendingSidePanelAction');
    });
  });

  chrome.storage.session.get(['pendingSidePanelAction']).then(({ pendingSidePanelAction }) => {
    if (!pendingSidePanelAction) return;

    handlePendingAction(pendingSidePanelAction).finally(() => {
      chrome.storage.session.remove('pendingSidePanelAction');
    });
  });
});
