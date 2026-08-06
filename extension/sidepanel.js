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
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
  const MAX_IMAGE_PIXELS = 20_000_000;
  const MAX_ACTION_AGE_MS = 5 * 60 * 1000;
  const MAX_URL_LENGTH = 2048;
  const PRIVACY_NOTICE_KEY = 'privacyNoticeAccepted';
  const CURRENT_CHAT_ID_KEY = 'currentChatId';
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
  let _sendingLock = false;
  async function handleSend() {
    if (_sendingLock) return;
    _sendingLock = true;
    const sendBtn = document.getElementById('sendBtn');
    if (sendBtn) sendBtn.disabled = true;
    try {
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
    let _renderTimer = null;
    let _lastActivity = Date.now();
    const _STALL_TIMEOUT = 30000;

    const _stallChecker = setInterval(() => {
      if (Date.now() - _lastActivity > _STALL_TIMEOUT) {
        clearInterval(_stallChecker);
        chrome.runtime.onMessage.removeListener(messageListener);
        if (_renderTimer) { clearTimeout(_renderTimer); _renderTimer = null; }
        aiBubble.textContent = '';
        aiBubble.appendChild(Object.assign(document.createElement('span'), {
          className: 'error-text',
          textContent: '⚠️ 响应超时（30秒无数据）'
        }));
      }
    }, 5000);

    const messageListener = (msg) => {
      if (msg.msgId !== msgId) return;
      _lastActivity = Date.now();

      if (msg.type === 'LLM_CHUNK') {
        fullReply += msg.chunk;
        if (!_renderTimer) {
          _renderTimer = setTimeout(() => {
            _renderTimer = null;
            setRenderedMarkdown(aiBubble, fullReply);
            enhanceCodeBlocks(aiBubble);
            renderMathInContainer(aiBubble);
            scrollToBottom();
          }, 100);
        }
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
        clearInterval(_stallChecker);
        if (_renderTimer) { clearTimeout(_renderTimer); _renderTimer = null; }
        setRenderedMarkdown(aiBubble, fullReply);
        enhanceCodeBlocks(aiBubble);
        renderMathInContainer(aiBubble);
        decorateSourceCitations(aiBubble);
        if (citedSources.length) renderCitedSources(aiBubble, citedSources);
        scrollToBottom();
        finalizeTimer = setTimeout(finalizeAssistantResponse, 150);
      }
      else if (msg.type === 'LLM_ERROR') {
        clearInterval(_stallChecker);
        if (_renderTimer) { clearTimeout(_renderTimer); _renderTimer = null; }
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
    } finally {
      _sendingLock = false;
      if (sendBtn) sendBtn.disabled = false;
    }
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

  // ═══════════════════════════════════════════════════════════════════
  // Agent 页面自动化模块
  // ═══════════════════════════════════════════════════════════════════

  const SLASH_COMMANDS = [
    { name: '/browser-operation', description: '浏览器页面自动化操作（点击、输入、滚动等）', handler: 'agent' },
  ];
  const AGENT_COMMAND = '/browser-operation ';
  const AGENT_SETTLE_TIMEOUT_MS = 3000;
  const AGENT_ACTION_TIMEOUT_MS = 10000;
  const AGENT_TOTAL_TIMEOUT_MS = 300000;

  const agentState = {
    active: false,
    sessionId: '',
    task: '',
    currentStep: 0,
    status: 'idle'
  };

  function shouldUseAgent(text) {
    const trimmed = text.trim();
    // 方式1: /browser-operation 前缀触发
    if (trimmed.startsWith(AGENT_COMMAND.trim())) return true;
    // 方式2: 自动化开关打开时，所有输入走 agent
    const toggle = document.getElementById('agentModeToggle');
    if (toggle && toggle.checked) return true;
    return false;
  }

  async function waitForPageSettle(tabId, timeoutMs) {
    const timeout = timeoutMs || AGENT_SETTLE_TIMEOUT_MS;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId, allFrames: false },
        func: (maxWait) => {
          return new Promise(resolve => {
            let timer = null;
            let settled = false;
            const observer = new MutationObserver(() => {
              clearTimeout(timer);
              timer = setTimeout(() => {
                observer.disconnect();
                settled = true;
                resolve(true);
              }, 300);
            });
            observer.observe(document.body || document.documentElement, {
              childList: true, subtree: true,
              attributes: true, characterData: true
            });
            timer = setTimeout(() => {
              observer.disconnect();
              if (!settled) resolve(true);
            }, 300);
            setTimeout(() => {
              if (!settled) { observer.disconnect(); resolve(false); }
            }, maxWait);
          });
        },
        args: [timeout]
      });
      return results?.[0]?.result !== false;
    } catch {
      await new Promise(r => setTimeout(r, 500));
      return true;
    }
  }

  async function observePageState() {
    const tab = await getActiveBrowserTab();
    if (!tab?.id) throw new Error('无法获取当前标签页');

    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: () => {
        const INTERACTIVE_SELECTORS = [
          'a[href]', 'button', 'input', 'textarea', 'select',
          '[role="button"]', '[role="link"]', '[role="tab"]',
          '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
          '[role="option"]', '[role="switch"]', '[role="slider"]',
          '[contenteditable="true"]', '[onclick]', '[tabindex]:not([tabindex="-1"])',
          '[data-component-name]',
          '.jmtd-base-input', '.jmtd-selector', '.jmtd-select',
          '.ant-select', '.ant-picker', '.ant-input',
          '.el-input', '.el-select', '.el-date-editor',
          '[class*="picker"]:not([class*="icon"])',
          '[class*="selector"]:not([class*="icon"])'
        ];

        // 弹出层内额外需要采集的选择器（主页面不需要这些细粒度元素）
        const POPUP_EXTRA_SELECTORS = [
          'td[role="gridcell"]',
          'li[role="option"]',
          '[data-event-content]',
          '.jmtd-date-picker-header-btn',
          '.jmtd-date-picker-cell-content-inner',
          '.jmtd-date-picker-switch-panel-item',
          '.ant-picker-cell',
          '.ant-picker-header-btn',
          '.ant-picker-header-super-prev-btn',
          '.ant-picker-header-prev-btn',
          '.ant-picker-header-next-btn',
          '.ant-picker-header-super-next-btn',
          '.ant-select-item-option',
          '.el-picker-panel__btn',
          '.el-select-dropdown__item',
          '[class*="picker-cell"]',
          '[class*="switch-panel-item"]',
          'li[class*="option"]',
          // 通用列表选项（下拉面板内的可点击项）
          '[class*="select-branch__item"]',
          '[class*="dropdown-item"]',
          '[class*="list-item"]',
          '[class*="menu-item"]:not([class*="icon"])',
          '.el-dropdown-menu__item',
          '[data-action-id]',
          // 通用菜单项（有 data-id 或 cursor-pointer 的可点击容器）
          '[data-id][class*="cursor-pointer"]',
          '[menu-item]',
          // 搜索建议/自动补全下拉
          'li.sc-v-center',
          '[class*="search__dropdown"] li',
          '[class*="autocomplete"] li',
          '.el-autocomplete-suggestion li'
        ];

        // 弹出层容器检测选择器
        const POPUP_CONTAINER_SELECTORS = [
          '[role="dialog"]', '[role="listbox"]', '[role="menu"]',
          '.jmtd-dropdown-panel', '.jmtd-dropdown-list',
          '.jmtd-popup', '.jmtd-modal',
          '.jmtd-date-picker-panel', '.jmtd-select-dropdown',
          '.ant-modal-content', '.ant-dropdown',
          '.ant-picker-panel', '.ant-picker-dropdown',
          '.ant-select-dropdown', '.ant-popover-inner',
          '.el-dialog', '.el-dropdown-menu',
          '.el-picker-panel', '.el-select-dropdown', '.el-popover',
          '[class*="popup"]:not([style*="display: none"])',
          '[class*="dropdown-list"]', '[class*="picker-panel"]',
          '[class*="search__dropdown"]', '[class*="autocomplete"]',
          '[class*="popover__content"]', '[class*="popper"]',
          '[class*="dropdownWrap"]', '[class*="dropdown__"]',
          '[class*="select-branch"]',
          '.modal[style*="display: block"]', '.modal.show'
        ];

        const MAX_POPUP_ELEMENTS = 100;
        const MAX_MAIN_WHEN_POPUP = 50;
        const MAX_TOTAL_NO_POPUP = 150;

        function isVisible(el) {
          const style = window.getComputedStyle(el);
          if (style.display === 'none' || style.visibility === 'hidden') return false;
          if (parseFloat(style.opacity) === 0) return false;
          const rect = el.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) return false;
          // 完全在视口外的元素（如隐藏的弹出层 left:-9999px）
          if (rect.right < -100 || rect.bottom < -100) return false;
          if (rect.left > window.innerWidth + 100) return false;
          if (rect.top > window.innerHeight + 100) return false;
          return true;
        }

        function buildCssSelector(el) {
          if (el.id && !el.id.match(/^[\d:]/) && !el.id.includes('--')) {
            return `#${CSS.escape(el.id)}`;
          }
          const testId = el.getAttribute('data-testid') || el.getAttribute('data-cy') || el.getAttribute('data-test');
          if (testId) return `[data-testid="${CSS.escape(testId)}"]`;

          const tag = el.tagName.toLowerCase();
          const attrs = [];
          if (el.name) attrs.push(`[name="${CSS.escape(el.name)}"]`);
          if (el.type && el.tagName === 'INPUT') attrs.push(`[type="${el.type}"]`);
          const ariaLabel = el.getAttribute('aria-label');
          if (ariaLabel) attrs.push(`[aria-label="${CSS.escape(ariaLabel)}"]`);
          if (el.className && typeof el.className === 'string') {
            const stableClasses = el.className.trim().split(/\s+/)
              .filter(c => !c.match(/^(css|sc|_|jsx|svelte)-|--|[a-z0-9]{6,}$/i))
              .slice(0, 2);
            if (stableClasses.length) attrs.push(stableClasses.map(c => `.${CSS.escape(c)}`).join(''));
          }
          let selector = tag + attrs.join('');
          if (document.querySelectorAll(selector).length === 1) return selector;
          const parent = el.parentElement;
          if (parent) {
            const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
            if (siblings.length > 1) {
              const idx = siblings.indexOf(el) + 1;
              selector = `${tag}${attrs.join('')}:nth-of-type(${idx})`;
            }
          }
          return selector;
        }

        function buildElementInfo(el, id, inPopup) {
          const rect = el.getBoundingClientRect();
          return {
            id,
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute('type') || '',
            role: el.getAttribute('role') || '',
            name: el.getAttribute('name') || '',
            placeholder: el.getAttribute('placeholder') || '',
            value: (el.value || '').slice(0, 100),
            text: (el.textContent || '').trim().slice(0, 100),
            aria_label: el.getAttribute('aria-label') || '',
            data_testid: el.getAttribute('data-testid') || '',
            component: el.getAttribute('data-component-name') || '',
            css_selector: buildCssSelector(el),
            bounding_box: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
            visible: true,
            enabled: !el.disabled && !el.classList.contains('jmtd-date-picker-cell-disabled'),
            checked: el.checked !== undefined ? el.checked : null,
            contenteditable: el.isContentEditable || false,
            in_popup: inPopup
          };
        }

        // 检测可见弹出层（取最顶层的）
        function findVisiblePopups() {
          const popups = [];
          for (const sel of POPUP_CONTAINER_SELECTORS) {
            for (const el of document.querySelectorAll(sel)) {
              if (!isVisible(el)) continue;
              // 排除已经被另一个 popup 包含的
              const dominated = popups.some(p => p.contains(el));
              if (dominated) continue;
              // 移除被当前 el 包含的
              for (let i = popups.length - 1; i >= 0; i--) {
                if (el.contains(popups[i])) popups.splice(i, 1);
              }
              popups.push(el);
            }
          }
          // 按 z-index 排序，最高的在前
          popups.sort((a, b) => {
            const za = parseInt(window.getComputedStyle(a).zIndex) || 0;
            const zb = parseInt(window.getComputedStyle(b).zIndex) || 0;
            return zb - za;
          });
          return popups;
        }

        // 采集元素的通用函数
        // skipNestedDedup=true 时禁用嵌套去重（弹出层内部元素不需要）
        function collectElements(root, selectors, maxCount, seen, annotationIdStart, inPopup, excludeContainers, skipNestedDedup) {
          const collected = [];
          let annotationId = annotationIdStart;
          for (const selector of selectors) {
            const candidates = root === document
              ? document.querySelectorAll(selector)
              : root.querySelectorAll(selector);
            for (const el of candidates) {
              if (collected.length >= maxCount) break;
              if (seen.has(el)) continue;
              if (!isVisible(el)) continue;
              // 排除已在弹出层采集过的
              if (excludeContainers && excludeContainers.some(c => c.contains(el))) continue;
              // 嵌套去重：仅在主页面采集时启用
              if (!skipNestedDedup) {
                let skipNested = false;
                let parent = el.parentElement;
                for (let i = 0; i < 3 && parent; i++) {
                  if (seen.has(parent)) { skipNested = true; break; }
                  parent = parent.parentElement;
                }
                if (skipNested) continue;
              }
              seen.add(el);

              const id = annotationId++;
              el.setAttribute('data-agent-id', id);
              collected.push(buildElementInfo(el, id, inPopup));
            }
          }
          return { collected, nextId: annotationId };
        }

        // 清除旧标记
        document.querySelectorAll('[data-agent-id]').forEach(el => el.removeAttribute('data-agent-id'));

        const seen = new Set();
        let elements = [];
        let annotationId = 1;
        let activePopup = null;

        // Step 1: 检测弹出层
        const popupContainers = findVisiblePopups();

        if (popupContainers.length > 0) {
          // Step 2a: 优先采集弹出层元素（细粒度优先，禁用嵌套去重）
          const popupSelectors = [...POPUP_EXTRA_SELECTORS, ...INTERACTIVE_SELECTORS];
          for (const popup of popupContainers) {
            if (elements.length >= MAX_POPUP_ELEMENTS) break;
            const remaining = MAX_POPUP_ELEMENTS - elements.length;
            const result = collectElements(popup, popupSelectors, remaining, seen, annotationId, true, null, true);
            elements.push(...result.collected);
            annotationId = result.nextId;
          }

          // 构建弹出层信息
          const topPopup = popupContainers[0];
          const headerEl = topPopup.querySelector('.jmtd-date-picker-header-content, .ant-picker-header, .el-date-picker__header, [class*="header-content"]');
          activePopup = {
            type: detectPopupType(topPopup),
            header_text: headerEl ? headerEl.textContent.trim() : '',
            selector: buildCssSelector(topPopup)
          };

          // Step 2b: 主页面剩余配额（启用嵌套去重）
          const mainResult = collectElements(document, INTERACTIVE_SELECTORS, MAX_MAIN_WHEN_POPUP, seen, annotationId, false, popupContainers, false);
          elements.push(...mainResult.collected);
          annotationId = mainResult.nextId;
        } else {
          // 无弹出层：正常采集（启用嵌套去重）
          const result = collectElements(document, INTERACTIVE_SELECTORS, MAX_TOTAL_NO_POPUP, seen, annotationId, false, null, false);
          elements.push(...result.collected);
          annotationId = result.nextId;
        }

        function detectPopupType(el) {
          const cls = el.className || '';
          if (cls.includes('date-picker') || cls.includes('picker-panel')) return 'date_picker';
          if (cls.includes('select-dropdown') || cls.includes('dropdown-list')) return 'dropdown';
          if (cls.includes('modal') || cls.includes('dialog')) return 'modal';
          if (cls.includes('menu')) return 'menu';
          if (el.getAttribute('role') === 'listbox') return 'dropdown';
          if (el.getAttribute('role') === 'dialog') return 'modal';
          if (el.getAttribute('role') === 'menu') return 'menu';
          return 'popup';
        }

        const truncated = elements.length >= (popupContainers.length > 0 ? MAX_POPUP_ELEMENTS + MAX_MAIN_WHEN_POPUP : MAX_TOTAL_NO_POPUP);

        // 检测弹窗/对话框内的滚动容器状态
        let scrollable_container = null;
        const modalSelectors = '[role="dialog"], .modal, .modal-body, .ant-modal-body, .el-dialog__body, [class*="modal"], [class*="dialog"], [class*="popup"]';
        const modals = document.querySelectorAll(modalSelectors);
        for (const m of modals) {
          const style = window.getComputedStyle(m);
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          const scrollEls = [m, ...m.querySelectorAll('*')];
          for (const el of scrollEls) {
            if (el.scrollHeight > el.clientHeight + 10) {
              const s = window.getComputedStyle(el);
              if (s.overflowY === 'auto' || s.overflowY === 'scroll' || el === m) {
                scrollable_container = {
                  scroll_top: Math.round(el.scrollTop),
                  scroll_height: el.scrollHeight,
                  client_height: el.clientHeight,
                  at_bottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 10
                };
                break;
              }
            }
          }
          if (scrollable_container) break;
        }

        // Loading 检测
        const isLoading = !!document.querySelector(
          '.loading, .spinner, [class*="loading"]:not([class*="loading-"]), ' +
          '[class*="spinner"], .ant-spin-spinning, .el-loading-mask, ' +
          '.jmtd-loading, .jmtd-spin-spinning, [class*="mask"][style*="display"]'
        );

        // 页面结构指纹（用于知识库跨域名匹配）
        const pageFingerprint = {
          site: location.hostname,
          title_keywords: document.title.split(/[\s\-_|]+/).filter(w => w.length > 1).slice(0, 5),
          menu_texts: Array.from(document.querySelectorAll(
            'nav a, [class*="menu"] a, [class*="nav"] a, [class*="nav"] span, [role="menuitem"], [data-id]'
          )).map(el => (el.textContent || '').trim()).filter(t => t && t.length <= 12).slice(0, 20),
          url_pattern: location.pathname
        };

        return {
          url: location.href,
          title: document.title,
          viewport: { width: window.innerWidth, height: window.innerHeight },
          scroll_position: { x: Math.round(window.scrollX), y: Math.round(window.scrollY) },
          document_height: document.documentElement.scrollHeight,
          scrollable_container,
          active_popup: activePopup,
          page_fingerprint: pageFingerprint,
          is_loading: isLoading,
          focused_element: document.activeElement?.id ? `#${document.activeElement.id}` : null,
          interactive_elements: elements,
          element_count_truncated: truncated,
          text_content_summary: (document.body?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 3000),
          forms: Array.from(document.forms).slice(0, 10).map(f => ({
            action: f.action,
            method: f.method,
            fields: Array.from(f.elements).map(e => e.name).filter(Boolean)
          }))
        };
      }
    });

    // 合并所有 frame 的结果（第一个是主 frame，后续是子 frame）
    const mainResult = results[0]?.result;
    if (!mainResult) throw new Error('页面观察失败');

    for (let i = 1; i < results.length; i++) {
      const frameResult = results[i]?.result;
      if (!frameResult || !frameResult.interactive_elements?.length) continue;
      // 给子 frame 元素续编 annotation_id，并同步更新 iframe 内 DOM
      const offset = mainResult.interactive_elements.length;
      const idMapping = [];
      for (const el of frameResult.interactive_elements) {
        const originalId = el.id;
        el.id = offset + originalId;
        el.in_iframe = true;
        el.iframe_index = i;
        idMapping.push({ original: originalId, newId: el.id });
        mainResult.interactive_elements.push(el);
      }
      // 更新 iframe 内的 data-agent-id 属性以匹配 offset 后的值
      chrome.scripting.executeScript({
        target: { tabId: tab.id, frameIds: [results[i].frameId || i] },
        func: (mapping) => {
          for (const { original, newId } of mapping) {
            const el = document.querySelector(`[data-agent-id="${original}"]`);
            if (el) el.setAttribute('data-agent-id', newId);
          }
        },
        args: [idMapping]
      }).catch(() => {});
      if (!mainResult.element_count_truncated && frameResult.element_count_truncated) {
        mainResult.element_count_truncated = true;
      }
    }
    return mainResult;
  }

  async function executePageAction(action) {
    if (action.type === 'wait') {
      const ms = Math.min(action.params?.ms || 1000, 5000);
      await new Promise(resolve => setTimeout(resolve, ms));
      return { success: true, action_type: 'wait', details: `等待了 ${ms}ms`, timestamp: Date.now() };
    }

    if (action.type === 'navigate') {
      const url = action.params?.url;
      if (!url) return { success: false, action_type: 'navigate', error: '缺少 URL', timestamp: Date.now() };
      // 安全校验：只允许 http/https 协议
      try {
        const parsed = new URL(url);
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          return { success: false, action_type: 'navigate', error: `不允许的 URL 协议: ${parsed.protocol}`, timestamp: Date.now() };
        }
      } catch {
        return { success: false, action_type: 'navigate', error: `无效的 URL: ${url}`, timestamp: Date.now() };
      }
      const tab = await getActiveBrowserTab();
      if (!tab?.id) return { success: false, action_type: 'navigate', error: '无法获取标签页', timestamp: Date.now() };
      await chrome.tabs.update(tab.id, { url });
      // 等待页面加载完成
      await new Promise((resolve) => {
        const listener = (tabId, info) => {
          if (tabId === tab.id && info.status === 'complete') {
            chrome.tabs.onUpdated.removeListener(listener);
            clearTimeout(timeout);
            resolve();
          }
        };
        const timeout = setTimeout(() => {
          chrome.tabs.onUpdated.removeListener(listener);
          resolve();
        }, 10000);
        chrome.tabs.onUpdated.addListener(listener);
      });
      return { success: true, action_type: 'navigate', details: `导航到 ${url}`, timestamp: Date.now() };
    }

    if (action.type === 'wait_for_element') {
      const tab = await getActiveBrowserTab();
      if (!tab?.id) return { success: false, action_type: 'wait_for_element', error: '无法获取标签页', timestamp: Date.now() };
      const selector = action.locator?.value || action.params?.selector;
      if (!selector) return { success: false, action_type: 'wait_for_element', error: '缺少选择器', timestamp: Date.now() };
      const timeout = Math.min(action.params?.timeout || 5000, 10000);
      const waitResults = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        func: async (sel, maxWait) => {
          const start = Date.now();
          while (Date.now() - start < maxWait) {
            const el = document.querySelector(sel);
            if (el) return { found: true };
            await new Promise(r => setTimeout(r, 200));
          }
          return { found: false };
        },
        args: [selector, timeout]
      });
      const found = waitResults.some(fr => fr?.result?.found);
      if (found) {
        return { success: true, action_type: 'wait_for_element', details: `元素已出现: ${selector}`, timestamp: Date.now() };
      }
      return { success: false, action_type: 'wait_for_element', error: `等待超时: ${selector}`, timestamp: Date.now() };
    }

    const tab = await getActiveBrowserTab();
    if (!tab?.id) throw new Error('无法获取当前标签页');

    const execPromise = chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: async (actionData) => {
        function findByMethod(method, value) {
          if (method === 'css') {
            return document.querySelector(value);
          }
          if (method === 'text') {
            const normalized = value.toLowerCase().trim();
            const candidates = [];
            // 搜索所有可见元素
            for (const el of document.querySelectorAll('*')) {
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') continue;
              const rect = el.getBoundingClientRect();
              if (rect.width <= 0 || rect.height <= 0) continue;
              const elText = (el.textContent || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
              if (!elText.includes(normalized)) continue;

              // 计算交互性权重
              let score = 0;
              const tag = el.tagName.toLowerCase();
              // 标签权重
              if (['button', 'a', 'input', 'select', 'textarea'].includes(tag)) score += 50;
              if (el.getAttribute('role') === 'button' || el.getAttribute('role') === 'link') score += 45;
              if (el.getAttribute('role') === 'option' || el.getAttribute('role') === 'menuitem') score += 40;
              if (el.onclick || el.getAttribute('tabindex')) score += 30;
              if (el.getAttribute('data-event-content') || el.getAttribute('data-event-name')) score += 35;
              // 在可交互父元素内的叶子节点
              if (el.closest('button, a, [role="button"]') && el.closest('button, a, [role="button"]') !== el) score += 15;
              // 精确匹配 vs 包含匹配
              if (elText === normalized) score += 40;
              else if (elText.startsWith(normalized)) score += 20;
              // 叶子节点优先（子元素越少越好）
              score += Math.max(0, 10 - el.children.length);
              // 尺寸合理（太大的通常是容器）
              if (rect.width < 500 && rect.height < 100) score += 10;
              // disabled 惩罚
              if (el.disabled || el.classList.contains('disabled')) score -= 20;

              candidates.push({ el, score });
            }
            if (!candidates.length) return null;
            candidates.sort((a, b) => b.score - a.score);
            return candidates[0].el;
          }
          if (method === 'annotation_id') {
            const el = document.querySelector(`[data-agent-id="${value}"]`);
            if (el) return el;
            return null;
          }
          return null;
        }

        function resolveLocator(locator) {
          if (!locator) return null;
          let el = findByMethod(locator.method, locator.value);
          // annotation_id 失效时：尝试 fallback，再尝试 text 匹配
          if (!el && locator.fallback) {
            el = findByMethod(locator.fallback.method, locator.fallback.value);
          }
          if (!el && locator.method === 'annotation_id' && locator.value) {
            // 尝试用 css_selector 回退（如果前端之前存过）
            el = findByMethod('text', locator.value);
          }
          if (!el && locator.method !== 'text' && locator.value) {
            el = findByMethod('text', locator.value);
          }
          return el;
        }

        function findBestScrollableContainer() {
          // 1. 找到当前可见的最顶层弹窗/对话框
          const modalSelectors = [
            '[role="dialog"]', '[role="listbox"]', '.modal', '.ant-modal',
            '.el-dialog', '.ant-drawer', '[class*="modal"]', '[class*="dialog"]',
            '[class*="popup"]', '[class*="overlay"]', '[class*="drawer"]'
          ];
          let modalEl = null;
          for (const sel of modalSelectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
              const style = window.getComputedStyle(el);
              if (style.display === 'none' || style.visibility === 'hidden') continue;
              const rect = el.getBoundingClientRect();
              if (rect.width <= 0 || rect.height <= 0) continue;
              modalEl = el;
              break;
            }
            if (modalEl) break;
          }

          // 2. 在弹窗内（或整个页面）找所有可滚动的后代
          const searchRoot = modalEl || document.body;
          let best = null;
          let bestScrollable = 0;

          const walker = document.createTreeWalker(searchRoot, NodeFilter.SHOW_ELEMENT);
          let node = walker.nextNode();
          while (node) {
            if (node.scrollHeight > node.clientHeight + 20) {
              const style = window.getComputedStyle(node);
              const overflow = style.overflowY;
              // 任何能滚动的容器：overflow auto/scroll，或者元素本身就是可滚动的
              if (overflow === 'auto' || overflow === 'scroll' || overflow === 'overlay' || node.scrollHeight > node.clientHeight + 50) {
                const scrollableAmount = node.scrollHeight - node.clientHeight;
                if (scrollableAmount > bestScrollable) {
                  bestScrollable = scrollableAmount;
                  best = node;
                }
              }
            }
            node = walker.nextNode();
          }
          return best;
        }

        const { type, locator, params = {} } = actionData;
        const element = (type !== 'scroll' && type !== 'press_key') ? resolveLocator(locator) : null;

        if (type !== 'scroll' && type !== 'press_key' && !element) {
          return { success: false, error: `找不到目标元素 (${locator?.method}:${locator?.value})`, action_type: type };
        }

        // 高亮目标元素
        if (element) {
          const origOutline = element.style.outline;
          const origTransition = element.style.transition;
          element.style.transition = 'outline 0.15s ease';
          element.style.outline = '3px solid #ff6b35';
          setTimeout(() => {
            element.style.outline = origOutline;
            element.style.transition = origTransition;
          }, 1200);
        }

        try {
          switch (type) {
            case 'click': {
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              await new Promise(r => setTimeout(r, 300));
              const rect = element.getBoundingClientRect();
              const cx = rect.left + rect.width / 2;
              const cy = rect.top + rect.height / 2;
              // 计算在顶层视口中的绝对坐标（处理 iframe 偏移）
              let offsetX = 0, offsetY = 0;
              let win = window;
              while (win !== win.parent) {
                try {
                  const frame = win.frameElement;
                  if (frame) {
                    const frameRect = frame.getBoundingClientRect();
                    offsetX += frameRect.left;
                    offsetY += frameRect.top;
                  }
                  win = win.parent;
                } catch (e) { break; } // 跨域时跳出
              }
              return {
                success: true,
                details: `点击了 ${locator?.value || '元素'}`,
                action_type: type,
                _clickCoords: { x: cx + offsetX, y: cy + offsetY }
              };
            }

            case 'type': {
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              await new Promise(r => setTimeout(r, 100));
              element.focus();
              const text = params.text || '';
              const isContentEditable = element.isContentEditable || element.getAttribute('contenteditable') === 'true';

              if (isContentEditable) {
                if (params.clear !== false) {
                  element.innerHTML = '';
                  element.dispatchEvent(new Event('input', { bubbles: true }));
                }
                element.focus();
                for (const char of text) {
                  element.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: char }));
                  document.execCommand('insertText', false, char);
                  element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: char }));
                }
              } else {
                // 安全地设置 value（兼容 isolated world）
                function safeSetValue(el, val) {
                  try {
                    const proto = el instanceof HTMLTextAreaElement
                      ? HTMLTextAreaElement.prototype
                      : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) { setter.call(el, val); return; }
                  } catch (e) { /* Illegal invocation in isolated world */ }
                  el.value = val;
                }
                if (params.clear !== false) {
                  safeSetValue(element, '');
                  element.dispatchEvent(new Event('input', { bubbles: true }));
                }
                safeSetValue(element, (params.clear !== false ? '' : element.value) + text);
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
              }
              return { success: true, details: `输入了 "${text.slice(0, 20)}"`, action_type: type };
            }

            case 'clear': {
              const isContentEditable = element.isContentEditable || element.getAttribute('contenteditable') === 'true';
              if (isContentEditable) {
                element.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                element.dispatchEvent(new Event('input', { bubbles: true }));
              } else {
                function safeSetValueClear(el, val) {
                  try {
                    const proto = el instanceof HTMLTextAreaElement
                      ? HTMLTextAreaElement.prototype
                      : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) { setter.call(el, val); return; }
                  } catch (e) { /* fallback */ }
                  el.value = val;
                }
                safeSetValueClear(element, '');
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
              }
              return { success: true, details: `清空了 ${locator?.value || '元素'}`, action_type: type };
            }

            case 'select': {
              const isNativeSelect = element.tagName.toLowerCase() === 'select';
              if (isNativeSelect) {
                if (params.value) {
                  element.value = params.value;
                } else if (params.option_text) {
                  const option = Array.from(element.options).find(
                    o => o.textContent.trim().toLowerCase() === params.option_text.toLowerCase()
                  );
                  if (option) element.value = option.value;
                }
                element.dispatchEvent(new Event('change', { bubbles: true }));
              } else {
                // 自定义下拉组件：先点击触发器，再点击选项
                const rect = element.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                const evtOpts = { bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window };
                element.dispatchEvent(new PointerEvent('pointerdown', evtOpts));
                element.dispatchEvent(new MouseEvent('mousedown', evtOpts));
                element.dispatchEvent(new PointerEvent('pointerup', evtOpts));
                element.dispatchEvent(new MouseEvent('mouseup', evtOpts));
                element.dispatchEvent(new MouseEvent('click', evtOpts));
                await new Promise(r => setTimeout(r, 500));
                // 在下拉弹出层中查找选项
                const optionText = (params.option_text || params.value || '').toLowerCase().trim();
                const dropdownItems = document.querySelectorAll(
                  '[role="option"], [role="listbox"] li, .ant-select-item, .el-select-dropdown__item, ' +
                  '[class*="option"], [class*="menu-item"], [class*="dropdown"] li'
                );
                let targetOption = null;
                for (const item of dropdownItems) {
                  const t = (item.textContent || '').trim().toLowerCase();
                  if (t === optionText || t.includes(optionText)) {
                    targetOption = item;
                    break;
                  }
                }
                if (targetOption) {
                  targetOption.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
                  targetOption.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                  targetOption.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
                  targetOption.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                  targetOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                } else {
                  return { success: false, error: `下拉菜单中找不到选项: ${optionText}`, action_type: type };
                }
              }
              return { success: true, details: `选择了 ${params.value || params.option_text || '?'}`, action_type: type };
            }

            case 'scroll': {
              const dir = params.direction || 'down';
              const amount = params.amount || 300;
              const yDelta = (dir === 'down') ? amount : (dir === 'up' ? -amount : 0);
              const xDelta = (dir === 'right') ? amount : (dir === 'left' ? -amount : 0);
              let scrollTarget = locator ? resolveLocator(locator) : null;
              if (!scrollTarget) {
                scrollTarget = findBestScrollableContainer();
              }
              let scrolledEl;
              const beforeScrollY = scrollTarget
                ? scrollTarget.scrollTop
                : window.scrollY;
              if (scrollTarget && scrollTarget !== document.documentElement && scrollTarget !== document.body) {
                scrollTarget.scrollBy(xDelta, yDelta);
                scrollTarget.dispatchEvent(new WheelEvent('wheel', { deltaX: xDelta, deltaY: yDelta, bubbles: true }));
                scrolledEl = scrollTarget;
              } else {
                window.scrollBy(xDelta, yDelta);
                scrolledEl = document.documentElement;
              }
              await new Promise(r => setTimeout(r, 100));
              const afterScrollY = scrolledEl ? scrolledEl.scrollTop : window.scrollY;
              const actualDelta = Math.abs(afterScrollY - beforeScrollY);
              const atBottom = scrolledEl
                ? (scrolledEl.scrollTop + scrolledEl.clientHeight >= scrolledEl.scrollHeight - 10)
                : (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 10);
              const atBoundary = actualDelta < Math.abs(amount) * 0.1;
              const detail = atBottom
                ? `滚动 ${dir} ${actualDelta}px（已到底部）`
                : atBoundary
                  ? `滚动 ${dir} 仅移动 ${actualDelta}px（已到边界）`
                  : `滚动 ${dir} ${actualDelta}px（可继续滚动）`;
              return { success: true, details: detail, action_type: type, at_boundary: atBoundary };
            }

            case 'scroll_to_element': {
              if (!element) return { success: false, error: '找不到目标元素', action_type: type };
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              await new Promise(r => setTimeout(r, 300));
              const rect = element.getBoundingClientRect();
              const inViewport = rect.top >= 0 && rect.bottom <= window.innerHeight;
              return {
                success: true,
                details: inViewport
                  ? `已滚动到 ${locator?.value || '元素'}，元素现在在视口内`
                  : `已尝试滚动到 ${locator?.value || '元素'}`,
                action_type: type
              };
            }

            case 'hover': {
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              await new Promise(r => setTimeout(r, 200));
              const rect = element.getBoundingClientRect();
              const evtOpts = { bubbles: true, clientX: rect.left + rect.width/2, clientY: rect.top + rect.height/2 };
              element.dispatchEvent(new PointerEvent('pointerenter', evtOpts));
              element.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
              element.dispatchEvent(new MouseEvent('mouseenter', evtOpts));
              element.dispatchEvent(new MouseEvent('mousemove', evtOpts));
              return { success: true, details: `悬停在 ${locator?.value || '元素'}`, action_type: type };
            }

            case 'focus':
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              element.focus();
              return { success: true, details: `聚焦到 ${locator?.value || '元素'}`, action_type: type };

            case 'press_key': {
              const target = element || document.activeElement || document.body;
              const key = params.key || 'Enter';
              const modifiers = params.modifiers || [];
              const code = key.length === 1 ? `Key${key.toUpperCase()}` : key;
              const eventInit = {
                key,
                code,
                bubbles: true,
                cancelable: true,
                ctrlKey: modifiers.includes('ctrl'),
                shiftKey: modifiers.includes('shift'),
                altKey: modifiers.includes('alt'),
                metaKey: modifiers.includes('meta')
              };
              target.dispatchEvent(new KeyboardEvent('keydown', eventInit));
              target.dispatchEvent(new KeyboardEvent('keypress', eventInit));
              target.dispatchEvent(new KeyboardEvent('keyup', eventInit));
              return { success: true, details: `按下了 ${key}`, action_type: type };
            }

            default:
              return { success: false, error: `不支持的操作: ${type}`, action_type: type };
          }
        } catch (err) {
          return { success: false, error: err.message || '执行失败', action_type: type };
        }
      },
      args: [action]
    });

    const timeoutPromise = new Promise((_, reject) =>
      setTimeout(() => reject(new Error('操作执行超时')), AGENT_ACTION_TIMEOUT_MS)
    );

    const frameResults = await Promise.race([execPromise, timeoutPromise]);
    // 从所有 frame 结果中取第一个成功的（非"找不到元素"的）
    let result = null;
    for (const fr of frameResults) {
      if (fr?.result?.success) { result = fr.result; break; }
    }
    // 如果没有成功的，取第一个有内容的结果（可能是报错）
    if (!result) {
      for (const fr of frameResults) {
        if (fr?.result) { result = fr.result; break; }
      }
    }
    if (!result) result = { success: false, error: '所有 frame 均无响应', action_type: 'unknown' };

    // click 操作：用 debugger 派发真实鼠标事件（isTrusted=true），失败时回退合成事件
    if (result.success && result._clickCoords && action.type === 'click') {
      const { x, y } = result._clickCoords;
      let debuggerOk = false;
      try {
        const resp = await chrome.runtime.sendMessage({
          type: 'DEBUGGER_CLICK', tabId: tab.id, x: Math.round(x), y: Math.round(y)
        });
        debuggerOk = resp?.ok;
      } catch (e) { /* debugger 不可用 */ }

      if (!debuggerOk) {
        // 回退：合成事件，用完整 locator 逻辑查找元素
        await chrome.scripting.executeScript({
          target: { tabId: tab.id, allFrames: true },
          func: (locatorData) => {
            let el = null;
            if (!locatorData) return;
            if (locatorData.method === 'css') {
              el = document.querySelector(locatorData.value);
            } else if (locatorData.method === 'annotation_id') {
              el = document.querySelector(`[data-agent-id="${locatorData.value}"]`);
            } else if (locatorData.method === 'text') {
              const normalized = locatorData.value.toLowerCase().trim();
              for (const candidate of document.querySelectorAll('*')) {
                const t = (candidate.textContent || '').trim().toLowerCase();
                if (t === normalized || t.includes(normalized)) {
                  const r = candidate.getBoundingClientRect();
                  if (r.width > 0 && r.height > 0) { el = candidate; break; }
                }
              }
            }
            if (!el && locatorData.fallback) {
              el = document.querySelector(`[data-agent-id="${locatorData.fallback.value}"]`);
            }
            if (!el) return;
            const rect = el.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            const opts = { bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window, button: 0 };
            el.dispatchEvent(new PointerEvent('pointerdown', opts));
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new PointerEvent('pointerup', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.dispatchEvent(new MouseEvent('click', opts));
            el.click();
          },
          args: [action.locator || null]
        }).catch(() => {});
      }
      delete result._clickCoords;
    }

    return { ...result, timestamp: Date.now() };
  }

  function buildAgentApiUrl(safeApiUrl, path) {
    return buildBackendEndpointUrl(safeApiUrl, path);
  }

  async function callAgentApi(safeApiUrl, path, body, apiKey) {
    const url = buildAgentApiUrl(safeApiUrl, path);
    const headers = { 'Content-Type': 'application/json' };
    if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
    return callApiJson(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body)
    });
  }

  function renderAgentPlan(bubble, plan) {
    if (!plan) return;
    const planDiv = document.createElement('div');
    planDiv.className = 'agent-plan';
    planDiv.dataset.planContainer = 'true';

    const titleEl = document.createElement('div');
    titleEl.className = 'agent-plan-title';
    titleEl.textContent = '📋 执行进度';
    planDiv.appendChild(titleEl);

    _renderPlanContent(planDiv, plan);
    bubble.appendChild(planDiv);
    scrollToBottom();
  }

  function updateAgentPlan(bubble, plan) {
    if (!plan) return;
    const container = bubble.querySelector('[data-plan-container]');
    if (!container) {
      renderAgentPlan(bubble, plan);
      return;
    }
    // 保存当前执行步骤区域
    const actionsArea = container.querySelector('.agent-goal-actions');

    // 清除旧内容（保留标题）
    const title = container.querySelector('.agent-plan-title');
    container.innerHTML = '';
    if (title) container.appendChild(title);
    _renderPlanContent(container, plan);

    // 将执行步骤区域挂到新的当前目标下
    if (actionsArea) {
      const newCurrentGoal = container.querySelector('.agent-goal.in_progress');
      if (newCurrentGoal) {
        newCurrentGoal.appendChild(actionsArea);
      }
    }
  }

  function _renderPlanContent(container, plan) {
    // 已完成目标
    if (plan.completed_goals && plan.completed_goals.length > 0) {
      plan.completed_goals.forEach(g => {
        const item = document.createElement('div');
        item.className = 'agent-goal completed';
        item.textContent = `✓ ${g}`;
        container.appendChild(item);
      });
    }
    // 当前目标
    if (plan.current_goal && !plan.task_done) {
      const current = document.createElement('div');
      current.className = 'agent-goal in_progress';
      current.dataset.currentGoal = 'true';
      current.textContent = `▶ ${plan.current_goal}`;
      container.appendChild(current);
    }
    // 剩余
    if (plan.remaining) {
      const remaining = document.createElement('div');
      remaining.className = 'agent-goal pending';
      remaining.textContent = `◇ ${plan.remaining}`;
      container.appendChild(remaining);
    }
  }

  function renderAgentStepInBubble(bubble, step, thought, action, result) {
    const stepDiv = document.createElement('div');
    stepDiv.className = 'agent-step';

    if (thought) {
      const thoughtEl = document.createElement('div');
      thoughtEl.className = 'agent-thought';
      thoughtEl.textContent = `💭 ${thought}`;
      stepDiv.appendChild(thoughtEl);
    }

    if (action) {
      const actionEl = document.createElement('div');
      actionEl.className = 'agent-action';
      let actionDesc = `🔧 [步骤${step}] ${action.type}`;
      if (action.locator) actionDesc += ` → ${action.locator.value}`;
      if (action.params?.text) actionDesc += ` (文本: "${action.params.text.slice(0, 20)}")`;
      if (action.params?.direction) actionDesc += ` (${action.params.direction})`;
      if (action.params?.key) actionDesc += ` (${action.params.key})`;
      actionEl.textContent = actionDesc;
      stepDiv.appendChild(actionEl);
    }

    if (result) {
      const resultEl = document.createElement('div');
      resultEl.className = result.success ? 'agent-result-success' : 'agent-result-fail';
      resultEl.textContent = result.success
        ? `✓ ${result.details || '成功'}`
        : `✗ ${result.error || '失败'}`;
      stepDiv.appendChild(resultEl);
    }

    // 追加到当前活跃目标的步骤区域内（而非 bubble 根级别）
    const container = bubble.querySelector('[data-plan-container]');
    if (container) {
      const activeGoal = container.querySelector('.agent-goal.in_progress');
      if (activeGoal) {
        let stepsArea = activeGoal.querySelector('.agent-goal-actions');
        if (!stepsArea) {
          stepsArea = document.createElement('div');
          stepsArea.className = 'agent-goal-actions';
          activeGoal.appendChild(stepsArea);
        }
        stepsArea.appendChild(stepDiv);
        scrollToBottom();
        return;
      }
    }
    // 无计划面板时直接追加到 bubble
    bubble.appendChild(stepDiv);
    scrollToBottom();
  }

  function renderAgentComplete(bubble, summary, success) {
    const completeEl = document.createElement('div');
    completeEl.className = success !== false ? 'agent-complete' : 'agent-error';
    completeEl.textContent = success !== false
      ? `✅ 任务完成: ${summary || '已完成所有操作'}`
      : `⚠️ 任务未能完成: ${summary || ''}`;
    bubble.appendChild(completeEl);
    scrollToBottom();
  }

  function renderEvaluationCard(bubble, info) {
    const card = document.createElement('div');
    card.className = 'agent-eval-card';

    const title = document.createElement('div');
    title.className = 'agent-eval-title';
    title.textContent = `本次执行是否成功？接受后存入知识库供下次参考（${info.executedSteps.length}步）`;
    card.appendChild(title);

    // 可折叠的待保存步骤 trace（默认隐藏）
    const traceToggle = document.createElement('div');
    traceToggle.className = 'agent-trace-toggle';
    traceToggle.textContent = '▸ 查看待保存的操作步骤';
    card.appendChild(traceToggle);

    const traceList = document.createElement('div');
    traceList.className = 'agent-record-steps';
    traceList.style.display = 'none';
    info.executedSteps.forEach((s, i) => {
      const stepEl = document.createElement('div');
      stepEl.className = 'agent-record-step';
      let desc = `${i + 1}. ${s.action}`;
      if (s.target_text) desc += ` → ${s.target_text}`;
      if (s.text) desc += ` (输入: "${s.text.slice(0, 20)}")`;
      stepEl.textContent = desc;
      traceList.appendChild(stepEl);
    });
    card.appendChild(traceList);

    traceToggle.addEventListener('click', () => {
      const hidden = traceList.style.display === 'none';
      traceList.style.display = hidden ? 'block' : 'none';
      traceToggle.textContent = hidden ? '▾ 收起操作步骤' : '▸ 查看待保存的操作步骤';
    });

    const noteInput = document.createElement('textarea');
    noteInput.className = 'agent-eval-note';
    noteInput.placeholder = '备注（可选）：例如"第一步要先点Coding"';
    card.appendChild(noteInput);

    const btnRow = document.createElement('div');
    btnRow.className = 'agent-eval-btns';

    const acceptBtn = document.createElement('button');
    acceptBtn.className = 'agent-btn-allow';
    acceptBtn.textContent = '✓ 接受并保存';
    acceptBtn.onclick = async () => {
      acceptBtn.disabled = true;
      // 引用回报：如果本次参照了知识库记录，回报成功
      if (info.referencedId) {
        await callAgentApi(info.safeApiUrl, '/v1/knowledge/usage',
          { record_id: info.referencedId, success: true }, info.apiKey).catch(() => {});
      }
      // 保存新记录
      try {
        await callAgentApi(info.safeApiUrl, '/v1/knowledge/record', {
          task_description: info.task,
          trigger_prompt: info.task,
          source: 'confirmed',
          page_fingerprint: info.fingerprint,
          steps: info.executedSteps,
          user_note: noteInput.value.trim()
        }, info.apiKey);
        card.innerHTML = '<div class="agent-eval-done">✓ 已存入知识库</div>';
      } catch (e) {
        card.innerHTML = `<div class="agent-eval-done">保存失败: ${e.message || '未知错误'}</div>`;
      }
    };

    const rejectBtn = document.createElement('button');
    rejectBtn.className = 'agent-btn-deny';
    rejectBtn.textContent = '✗ 不保存';
    rejectBtn.onclick = async () => {
      // 引用回报：如果参照了知识库但用户拒绝，回报失败
      if (info.referencedId) {
        await callAgentApi(info.safeApiUrl, '/v1/knowledge/usage',
          { record_id: info.referencedId, success: false }, info.apiKey).catch(() => {});
      }
      card.remove();
    };

    btnRow.append(acceptBtn, rejectBtn);
    card.appendChild(btnRow);
    bubble.appendChild(card);
    scrollToBottom();
  }

  function renderRecordingConfirmCard(info) {
    const bubble = createMessageNode('ai');
    const card = document.createElement('div');
    card.className = 'agent-eval-card';

    const title = document.createElement('div');
    title.className = 'agent-eval-title';
    title.textContent = `录制完成：「${info.task}」共 ${info.steps.length} 步。确认后存入知识库：`;
    card.appendChild(title);

    // 展示录制的步骤列表
    const stepsList = document.createElement('div');
    stepsList.className = 'agent-record-steps';
    info.steps.forEach((s, i) => {
      const stepEl = document.createElement('div');
      stepEl.className = 'agent-record-step';
      let desc = `${i + 1}. ${s.action}`;
      if (s.target_text) desc += ` → ${s.target_text}`;
      if (s.text) desc += ` (输入: "${s.text.slice(0, 20)}")`;
      stepEl.textContent = desc;
      stepsList.appendChild(stepEl);
    });
    card.appendChild(stepsList);

    const noteInput = document.createElement('textarea');
    noteInput.className = 'agent-eval-note';
    noteInput.placeholder = '备注（可选）：例如"这个流程用于查询本月订单"';
    card.appendChild(noteInput);

    const btnRow = document.createElement('div');
    btnRow.className = 'agent-eval-btns';

    const acceptBtn = document.createElement('button');
    acceptBtn.className = 'agent-btn-allow';
    acceptBtn.textContent = '✓ 确认保存';
    acceptBtn.onclick = async () => {
      acceptBtn.disabled = true;
      try {
        const { apiKey, safeApiUrl } = await resolveApiRequestConfig();
        await callAgentApi(safeApiUrl, '/v1/knowledge/record', {
          task_description: info.task,
          trigger_prompt: info.task,
          source: 'recorded',
          page_fingerprint: info.fingerprint,
          steps: info.steps,
          user_note: noteInput.value.trim()
        }, apiKey);
        card.innerHTML = `<div class="agent-eval-done">✓ 已保存 ${info.steps.length} 步到知识库</div>`;
      } catch (e) {
        card.innerHTML = `<div class="agent-eval-done">保存失败: ${e.message || '未知错误'}</div>`;
      }
    };

    const rejectBtn = document.createElement('button');
    rejectBtn.className = 'agent-btn-deny';
    rejectBtn.textContent = '✗ 放弃';
    rejectBtn.onclick = () => {
      card.innerHTML = '<div class="agent-eval-done">已放弃本次录制</div>';
    };

    btnRow.append(acceptBtn, rejectBtn);
    card.appendChild(btnRow);
    bubble.appendChild(card);
    scrollToBottom();
  }

  function renderAgentError(bubble, error) {
    const errorEl = document.createElement('div');
    errorEl.className = 'agent-error';
    errorEl.textContent = `❌ 错误: ${error}`;
    bubble.appendChild(errorEl);
    scrollToBottom();
  }

  function showAgentConfirmDialog(bubble, action, thought) {
    return new Promise((resolve) => {
      const confirmDiv = document.createElement('div');
      confirmDiv.className = 'agent-confirm';

      const msgEl = document.createElement('div');
      msgEl.className = 'agent-confirm-msg';
      let desc = `⚠️ 即将执行: ${action.type}`;
      if (action.locator) desc += ` → ${action.locator.value}`;
      if (thought) desc += `\n原因: ${thought}`;
      msgEl.textContent = desc;
      confirmDiv.appendChild(msgEl);

      const btnContainer = document.createElement('div');
      btnContainer.className = 'agent-confirm-btns';

      const allowBtn = document.createElement('button');
      allowBtn.textContent = '允许';
      allowBtn.className = 'agent-btn-allow';
      allowBtn.onclick = () => { confirmDiv.remove(); resolve(true); };

      const denyBtn = document.createElement('button');
      denyBtn.textContent = '取消';
      denyBtn.className = 'agent-btn-deny';
      denyBtn.onclick = () => { confirmDiv.remove(); resolve(false); };

      btnContainer.append(allowBtn, denyBtn);
      confirmDiv.appendChild(btnContainer);
      bubble.appendChild(confirmDiv);
      scrollToBottom();
    });
  }

  async function runAgentTask(task) {
    const sessionId = `agent_${createMessageId()}`;
    agentState.active = true;
    agentState.sessionId = sessionId;
    agentState.task = task;
    agentState.status = 'running';
    agentState.currentStep = 0;

    // 记录本次执行的操作序列和页面指纹，供知识库保存
    const executedSteps = [];
    let firstFingerprint = null;

    let apiKey, modelName, safeApiUrl;
    try {
      ({ apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig());
    } catch (err) {
      alert(err.message || 'API 配置无效');
      agentState.active = false;
      agentState.status = 'idle';
      return;
    }

    const userBubble = createMessageNode('user');
    const userText = document.createElement('div');
    userText.textContent = `🤖 [自动化] ${task}`;
    userBubble.appendChild(userText);

    const aiBubble = createMessageNode('ai');
    showTypingIndicator(aiBubble);
    scrollToBottom();

    let cancelBtn = null;

    try {
      let pageState = await observePageState();
      firstFingerprint = pageState?.page_fingerprint || null;
      aiBubble.textContent = '';

      const headerEl = document.createElement('div');
      headerEl.className = 'agent-header';
      headerEl.textContent = '🤖 Agent 自动化执行中...';
      aiBubble.appendChild(headerEl);

      cancelBtn = document.createElement('button');
      cancelBtn.className = 'agent-btn-cancel';
      cancelBtn.textContent = '⏹ 停止';
      cancelBtn.onclick = async () => {
        agentState.active = false;
        cancelBtn.disabled = true;
        cancelBtn.textContent = '已停止';
        await callAgentApi(safeApiUrl, '/v1/agent/cancel', { session_id: sessionId }, apiKey).catch(() => {});
        cancelBtn.remove();
      };
      headerEl.appendChild(cancelBtn);

      let response = await callAgentApi(safeApiUrl, '/v1/agent/execute', {
        task,
        page_state: pageState,
        session_id: sessionId,
        model: modelName || 'gpt-4o',
        require_confirmation: []
      }, apiKey);

      // 首次渲染计划面板
      if (response.plan) {
        renderAgentPlan(aiBubble, response.plan);
      }

      const agentStartTime = Date.now();
      while (response.status === 'action_required' || response.status === 'confirm_required') {
        if (!agentState.active) break;
        if (Date.now() - agentStartTime > AGENT_TOTAL_TIMEOUT_MS) {
          renderAgentError(aiBubble, '执行超时（5分钟）');
          break;
        }

        // 每步更新目标进度
        if (response.plan) updateAgentPlan(aiBubble, response.plan);

        agentState.currentStep = response.step;
        const action = response.action;

        // 子任务切换时 action 为 null，直接继续请求下一步
        if (!action) {
          const freshState = await observePageState();
          response = await callAgentApi(safeApiUrl, '/v1/agent/step', {
            session_id: sessionId,
            action_result: { success: true, action_type: 'sub_task_switch', details: '子任务切换' },
            page_state: freshState
          }, apiKey);
          continue;
        }

        if (response.status === 'confirm_required') {
          const confirmed = await showAgentConfirmDialog(aiBubble, action, response.thought);
          if (!confirmed) {
            await callAgentApi(safeApiUrl, '/v1/agent/cancel', { session_id: sessionId }, apiKey).catch(() => {});
            renderAgentError(aiBubble, '用户取消了操作');
            break;
          }
        }

        renderAgentStepInBubble(aiBubble, response.step, response.thought, action, null);

        // 记录操作前状态
        const preUrl = pageState?.url || '';
        const prePopup = pageState?.active_popup || null;
        const preElementCount = (pageState?.interactive_elements || []).length;

        const actionResult = await executePageAction(action);

        renderAgentStepInBubble(aiBubble, response.step, null, null, actionResult);

        // 记录成功的操作步骤（供知识库保存）
        if (actionResult.success && action.type !== 'wait') {
          executedSteps.push({
            action: action.type,
            target_text: action.locator?.value || '',
            css_selector: action.locator?.method === 'css' ? action.locator.value : '',
            text: action.params?.text || '',
            url_pattern: pageState?.url ? new URL(pageState.url).pathname : ''
          });
        }
        if (!firstFingerprint && pageState?.page_fingerprint) {
          firstFingerprint = pageState.page_fingerprint;
        }

        // 智能等待页面稳定（替代固定延时）
        const activeTab = await getActiveBrowserTab().catch(() => null);
        if (activeTab?.id) {
          await waitForPageSettle(activeTab.id);
        }

        const newPageState = await observePageState();

        // 附加状态变化信息
        actionResult.state_changes = {
          url_changed: newPageState.url !== preUrl,
          popup_appeared: !prePopup && !!newPageState.active_popup,
          popup_disappeared: !!prePopup && !newPageState.active_popup,
          element_count_delta: newPageState.interactive_elements.length - preElementCount
        };
        pageState = newPageState;

        // 两阶段调用：先规划（快速更新目标面板），再执行
        const planResponse = await callAgentApi(safeApiUrl, '/v1/agent/plan', {
          session_id: sessionId,
          action_result: actionResult,
          page_state: newPageState
        }, apiKey);

        // 立即更新目标面板
        if (planResponse.plan) updateAgentPlan(aiBubble, planResponse.plan);

        // 如果规划阶段就完成/出错了，不再调执行
        if (planResponse.status === 'completed' || planResponse.status === 'error') {
          response = planResponse;
        } else {
          // 调执行 LLM 获取 action
          response = await callAgentApi(safeApiUrl, '/v1/agent/action', {
            session_id: sessionId
          }, apiKey);
        }
      }

      if (response.status === 'completed') {
        if (response.plan) updateAgentPlan(aiBubble, response.plan);
        renderAgentComplete(aiBubble, response.summary, true);
        // 弹出评价窗口，用户接受则存入知识库
        if (executedSteps.length >= 2 && firstFingerprint) {
          renderEvaluationCard(aiBubble, {
            task, executedSteps, fingerprint: firstFingerprint,
            safeApiUrl, apiKey, referencedId: response.plan?.referenced_id
          });
        }
      } else if (response.status === 'error') {
        renderAgentError(aiBubble, response.error || '未知错误');
      }

    } catch (err) {
      aiBubble.textContent = '';
      renderAgentError(aiBubble, err.message || '执行失败');
    } finally {
      agentState.active = false;
      agentState.status = 'idle';
      if (cancelBtn && cancelBtn.parentNode) cancelBtn.remove();
      // 释放 debugger 连接
      const activeTab = await getActiveBrowserTab().catch(() => null);
      if (activeTab?.id) {
        chrome.runtime.sendMessage({ type: 'DEBUGGER_DETACH', tabId: activeTab.id }).catch(() => {});
      }
    }
  }

  // 拦截发送：如果用户输入匹配 agent 关键词，走 agent 流程而非普通聊天
  (function installAgentInterceptor() {
    const inputEl = document.getElementById('chatInput');
    const btnEl = document.getElementById('sendBtn');
    const toggleEl = document.getElementById('agentModeToggle');
    const agentBtn = document.getElementById('agentModeBtn');
    if (!inputEl || !btnEl) return;

    // 按钮点击切换自动化模式
    if (agentBtn && toggleEl) {
      agentBtn.addEventListener('click', () => {
        toggleEl.checked = !toggleEl.checked;
        agentBtn.classList.toggle('is-active', toggleEl.checked);
        inputEl.placeholder = toggleEl.checked
          ? '输入自动化指令 (如: 帮我点击搜索按钮)...'
          : '输入问题 (Enter发送, Shift+Enter换行)...';
      });
    }

    // ═══ 斜杠命令菜单 ═══
    let slashMenu = null;

    function createSlashMenu() {
      if (slashMenu) return slashMenu;
      slashMenu = document.createElement('div');
      slashMenu.className = 'slash-menu';
      slashMenu.style.display = 'none';
      SLASH_COMMANDS.forEach((cmd, i) => {
        const item = document.createElement('div');
        item.className = 'slash-menu-item';
        item.dataset.index = i;
        item.innerHTML = `<span class="slash-menu-cmd">${cmd.name}</span><span class="slash-menu-desc">${cmd.description}</span>`;
        item.addEventListener('click', () => {
          inputEl.value = cmd.name + ' ';
          inputEl.focus();
          hideSlashMenu();
        });
        slashMenu.appendChild(item);
      });
      inputEl.parentElement.style.position = 'relative';
      inputEl.parentElement.appendChild(slashMenu);
      return slashMenu;
    }

    function showSlashMenu(filter) {
      const menu = createSlashMenu();
      const items = menu.querySelectorAll('.slash-menu-item');
      let hasVisible = false;
      items.forEach((item, i) => {
        const cmd = SLASH_COMMANDS[i];
        const show = !filter || cmd.name.includes(filter);
        item.style.display = show ? 'flex' : 'none';
        if (show) hasVisible = true;
      });
      menu.style.display = hasVisible ? 'block' : 'none';
    }

    function hideSlashMenu() {
      if (slashMenu) slashMenu.style.display = 'none';
    }

    inputEl.addEventListener('input', () => {
      const val = inputEl.value;
      if (val.startsWith('/') && !val.includes(' ')) {
        showSlashMenu(val);
      } else {
        hideSlashMenu();
      }
    });

    inputEl.addEventListener('blur', () => {
      setTimeout(hideSlashMenu, 150);
    });

    // ═══ 发送拦截 ═══
    function tryAgentIntercept(e) {
      const text = inputEl.value?.trim() || '';
      if (text && shouldUseAgent(text) && !agentState.active) {
        e.preventDefault();
        e.stopImmediatePropagation();
        inputEl.value = '';
        hideSlashMenu();
        // 去掉命令前缀
        const task = text.startsWith(AGENT_COMMAND.trim())
          ? text.slice(AGENT_COMMAND.trim().length).trim()
          : text;
        runAgentTask(task);
        return true;
      }
      return false;
    }

    btnEl.addEventListener('click', (e) => tryAgentIntercept(e), true);
    inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) tryAgentIntercept(e);
    }, true);
  })();

  // ═══════════════════════════════════════════════════════════════════
  // 操作录制模块（存入知识库）
  // ═══════════════════════════════════════════════════════════════════
  (function installRecorder() {
    const recordBtn = document.getElementById('recordBtn');
    if (!recordBtn) return;

    let recording = false;
    let recordedSteps = [];
    let recordTask = '';
    let recordFingerprint = null;

    recordBtn.addEventListener('click', async () => {
      if (!recording) {
        // 开始录制
        const desc = prompt('请输入这次操作的描述（如：在coding中搜索仓库）:');
        if (!desc || !desc.trim()) return;
        recordTask = desc.trim();
        recordedSteps = [];
        recordFingerprint = null;

        const tab = await getActiveBrowserTab().catch(() => null);
        if (!tab?.id) { alert('无法获取当前标签页'); return; }

        // 采集起始页面指纹
        try {
          const fpResults = await chrome.scripting.executeScript({
            target: { tabId: tab.id, allFrames: false },
            func: () => ({
              site: location.hostname,
              title_keywords: document.title.split(/[\s\-_|]+/).filter(w => w.length > 1).slice(0, 5),
              menu_texts: Array.from(document.querySelectorAll(
                'nav a, [class*="menu"] a, [class*="nav"] a, [role="menuitem"], [data-id]'
              )).map(el => (el.textContent || '').trim()).filter(t => t && t.length <= 12).slice(0, 20),
              url_pattern: location.pathname
            })
          });
          recordFingerprint = fpResults?.[0]?.result || null;
        } catch { /* ignore */ }

        // 注入录制拦截脚本到所有 frame
        await chrome.scripting.executeScript({
          target: { tabId: tab.id, allFrames: true },
          func: () => {
            if (window.__agentRecorderInstalled) return;
            window.__agentRecorderInstalled = true;
            window.__agentRecordedSteps = [];

            function selectorOf(el) {
              if (el.id && !el.id.match(/^[\d:]/)) return `#${el.id}`;
              const testId = el.getAttribute('data-testid');
              if (testId) return `[data-testid="${testId}"]`;
              return '';
            }

            document.addEventListener('click', (e) => {
              if (!window.__agentRecording) return;
              const el = e.target;
              window.__agentRecordedSteps.push({
                action: 'click',
                target_text: (el.textContent || '').trim().slice(0, 40),
                css_selector: selectorOf(el),
                text: '',
                url_pattern: location.pathname
              });
            }, true);

            let inputTimer = null;
            document.addEventListener('input', (e) => {
              if (!window.__agentRecording) return;
              const el = e.target;
              clearTimeout(inputTimer);
              inputTimer = setTimeout(() => {
                window.__agentRecordedSteps.push({
                  action: 'type',
                  target_text: el.placeholder || el.name || '',
                  css_selector: selectorOf(el),
                  text: (el.value || '').slice(0, 50),
                  url_pattern: location.pathname
                });
              }, 500);
            }, true);
          }
        }).catch(() => {});

        // 打开录制标志
        await chrome.scripting.executeScript({
          target: { tabId: tab.id, allFrames: true },
          func: () => { window.__agentRecording = true; }
        }).catch(() => {});

        recording = true;
        recordBtn.classList.add('is-active');
        recordBtn.textContent = '⏹️ 停止录制';
      } else {
        // 停止录制并保存
        recording = false;
        recordBtn.classList.remove('is-active');
        recordBtn.textContent = '⏺️ 录制';

        const tab = await getActiveBrowserTab().catch(() => null);
        if (tab?.id) {
          try {
            const results = await chrome.scripting.executeScript({
              target: { tabId: tab.id, allFrames: true },
              func: () => {
                window.__agentRecording = false;
                const steps = window.__agentRecordedSteps || [];
                window.__agentRecordedSteps = [];
                return steps;
              }
            });
            // 合并所有 frame 的录制步骤
            for (const r of results) {
              if (r?.result?.length) recordedSteps.push(...r.result);
            }
          } catch { /* ignore */ }
        }

        if (recordedSteps.length === 0) {
          alert('未录制到任何操作');
          return;
        }

        // 展示录制结果确认卡片，用户确认后才保存
        renderRecordingConfirmCard({
          task: recordTask,
          steps: recordedSteps,
          fingerprint: recordFingerprint || {}
        });
      }
    });
  })();
});
