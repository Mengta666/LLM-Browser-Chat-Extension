// 侧边栏主脚本：管理聊天 UI、网页上下文、长期记忆、计划模式、图片附件和图片工具。
document.addEventListener('DOMContentLoaded', async () => {
  // 页面内状态集中放在 DOMContentLoaded 闭包里，避免暴露到扩展页面全局。
  let conversationHistory = []; // 仅保存当前侧边栏会话的短期上下文。
  const handledAutoSendActionIds = new Set();
  let attachedImage = null;
  let imageToolCurrentDataUrl = '';
  let imageToolCurrentFileName = 'image.png';
  const TASK_TYPE_LABELS = {
    chat: '普通问答',
    explain: '解释',
    translate: '翻译',
    plan: '计划'
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
  const webSearchState = {
    enabled: false
  };
  const markdownParser = window.marked;
  const MAX_HISTORY_MESSAGES = 12;
  const MAX_PROMPT_LENGTH = 8000;
  const MAX_PAGE_CONTEXT_CHARS = 50000;
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
  const MAX_IMAGE_PIXELS = 20_000_000;
  const MAX_ACTION_AGE_MS = 5 * 60 * 1000;
  const MAX_URL_LENGTH = 2048;
  const PRIVACY_NOTICE_KEY = 'privacyNoticeAccepted';
  const CURRENT_CHAT_ID_KEY = 'currentChatId';
  const CUSTOM_API_BASE_URLS_KEY = 'customApiBaseUrls';
  const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
  const CHAT_HISTORY_ENDPOINT_PATH = '/api/chats';
  const MEMORY_ENDPOINT_PATH = '/api/memories';
  const PLAN_ENDPOINT_PREFIX = '/api/plans';
  const PLAN_AUTO_EXECUTE_PROMPT = '开始执行当前计划。请一次性完成已批准计划中的全部未完成步骤，并输出完整结果。不要只执行第一步，也不要只复述计划。';
  const MEMORY_TYPE_FILTER_OPTIONS = new Set([
    'user_profile',
    'project_state',
    'task_state',
    'procedural_feedback',
    'episodic_lesson',
    'external_knowledge_ref'
  ]);
  const ALLOWED_IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  const SOURCE_BLOCK_PATTERN = /\[([^\[\]]+)\]/g;
  const SOURCE_ID_PATTERN = /^S\d+$/i;
  let currentChatId = '';
  const planState = {
    activePlan: null,
    revising: false,
    collapsed: false,
    actionPending: false
  };
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

  // Markdown/HTML 安全渲染工具。模型输出先经过白名单清洗，再进入气泡 DOM。
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
    // 统一转成 PNG，避免后续发送和预览链路处理多种浏览器编码差异。
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
    // 优先使用浏览器原生 UUID；旧环境用 crypto 随机字节兜底。
    if (crypto?.randomUUID) return crypto.randomUUID();

    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  }

  function createChatId() {
    return `chat_${createMessageId()}`;
  }

  async function getOrCreateCurrentChatId() {
    // chat_id 存在 session storage，刷新侧边栏后仍能继续当前会话。
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
    // 自定义 API 地址禁止携带账号密码、查询参数和 hash，降低误发凭证风险。
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
    // 后端兼容 OpenAI 风格的 /v1 地址，这里把业务接口统一映射回服务根路径。
    const normalizedApiBaseUrl = normalizeApiBaseUrl(apiBaseUrl);
    const parsedUrl = new URL(normalizedApiBaseUrl);
    let basePath = parsedUrl.pathname.replace(/\/$/, '');
    if (basePath.endsWith('/v1')) {
      basePath = basePath.slice(0, -3);
    }
    return `${parsedUrl.origin}${basePath}${endpointPath}`;
  }

  async function getAllowedApiBaseUrls() {
    // 默认只信任官方 OpenAI 地址；用户确认过的自定义地址持久化到本地白名单。
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
    // 自定义服务会接收聊天内容与密钥，首次添加必须让用户显式确认风险。
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
    // OpenAI 官方地址要求 sk- 前缀；本机/内网后端允许不带 Authorization。
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
    // 每次发送前重新读取设置，避免侧边栏长时间打开后使用过期的模型或 API 地址。
    const { apiUrl, modelName } = await chrome.storage.local.get(['apiUrl', 'modelName']);
    const { apiKey, apiKeyApiUrl } = await getStoredApiCredential();
    const safeApiUrl = await validateOpenAIApiConfig(apiUrl || DEFAULT_API_URL, apiKey);
    const shouldEnforceApiKeyBinding = Boolean(String(apiKey || '').trim())
      && !isPrivateOrLocalHost(new URL(safeApiUrl).hostname);

    if (shouldEnforceApiKeyBinding && apiKeyApiUrl && apiKeyApiUrl !== safeApiUrl) {
      throw new Error('当前 API Key 与 API 地址不匹配，请在设置中重新保存配置');
    }

    if (requireBackendApi && DEFAULT_API_BASE_URLS.has(safeApiUrl)) {
      throw new Error('该功能需要连接 browser-agent 后端 API 地址，不能使用 OpenAI 官方 API 地址');
    }

    return {
      apiKey,
      modelName,
      safeApiUrl
    };
  }

  function buildMessagesPayload(history) {
    // 直连 Chat Completions 时只发送压缩后的近期上下文，最后一轮图片按需保留。
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
    // 前端只接受固定任务类型，未知值回退普通聊天。
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

    if (taskState.taskType === 'plan') {
      input.placeholder = planState.revising
        ? '说明你希望怎么修改当前计划...'
        : '描述你要形成计划的目标...';
      return;
    }

    input.placeholder = '输入问题 (Enter发送, Shift+Enter换行)...';
  }

  function updateTaskUi() {
    // 任务条只反映当前输入模式，真正发送时仍会重新校验 focusText 和图片状态。
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

    if (taskState.taskType === 'plan') {
      meta.textContent = planState.activePlan
        ? '计划模式会基于当前计划生成修订；点击“同意开始”后才会创建任务状态。'
        : '计划模式只生成计划草稿，不会直接开始执行。';
    } else if (taskState.taskType === 'chat') {
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

    if (!taskState.focusText && !['chat', 'plan'].includes(taskState.taskType)) {
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
    // 当前页正文来自用户显式启用/锁定，不在后台持续读取网页内容。
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
        func: (maxChars) => {
          const rawContent = String(document.body?.innerText || '').trim();
          const content = rawContent.slice(0, maxChars);
          return {
            url: location.href,
            title: document.title || '',
            content,
            contentCharCount: rawContent.length,
            contentTruncated: rawContent.length > maxChars
          };
        },
        args: [MAX_PAGE_CONTEXT_CHARS]
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
      content,
      contentCharCount: Number(result.contentCharCount || content.length),
      contentTruncated: Boolean(result.contentTruncated)
    };
  }

  function getPageContextElements() {
    return {
      useToggle: document.getElementById('useCurrentPageToggle'),
      lockToggle: document.getElementById('lockCurrentPageToggle'),
      webSearchToggle: document.getElementById('useWebSearchToggle'),
      refreshButton: document.getElementById('refreshPageContextBtn'),
      clearButton: document.getElementById('clearPageSnapshotBtn'),
      summary: document.getElementById('pageContextSummary'),
      webSearchStatus: document.getElementById('webSearchStatusInline'),
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

    const content = String(currentPage.content || '').trim();
    return {
      url: String(currentPage.url || '').trim(),
      title: String(currentPage.title || '').trim(),
      content,
      contentCharCount: Number(currentPage.contentCharCount || content.length),
      contentTruncated: Boolean(currentPage.contentTruncated)
    };
  }

  function formatPageContextSize(currentPage) {
    if (!currentPage) return '';

    const contentLength = Number(currentPage.contentCharCount || currentPage.content?.length || 0);
    if (!contentLength) return '';

    const truncatedText = currentPage.contentTruncated ? '，已截断' : '';
    return `已读取 ${contentLength.toLocaleString()} 字${truncatedText}`;
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
    // 网页上下文 UI 同时展示采集状态、锁定状态和最近一次后端刷新结果。
    const {
      useToggle,
      lockToggle,
      webSearchToggle,
      refreshButton,
      clearButton,
      summary,
      webSearchStatus,
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
    if (webSearchToggle) {
      webSearchToggle.checked = webSearchState.enabled;
    }
    if (webSearchStatus) {
      webSearchStatus.textContent = webSearchState.enabled
        ? '联网搜索：发送时会检索外部网页'
        : '联网搜索：未启用';
    }
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
    const pageSizeText = formatPageContextSize(pageContextState.snapshot);
    if (pageSizeText) {
      statusMeta.textContent += ` ${pageSizeText}。`;
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
    // 读取当前活动页正文，并把结果保存成侧边栏内存快照。
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
    // 发送前统一解析网页上下文：锁定则复用快照，未锁定则重新读取当前页。
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
    // 旧版本曾把 API Key 写入 local storage；读取时迁移到 session storage，减少长期落盘。
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
    // 所有后端 JSON API 都经 background 转发，统一做 URL 白名单和鉴权校验。
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
    // 手动刷新会把当前页正文送到后端重建该 chat 的网页索引。
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
    // 本地上下文只保留文本，历史图片用占位文本替换，避免重复携带大体积或隐私图片。
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
    // Data URL 只接受明确图片 MIME 与 base64 内容，并按 base64 长度预估大小上限。
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
    // 外部图片地址只接受 HTTP(S) 且禁止 URL 内嵌用户名密码。
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

    // HTTP、本机和内网图片可能泄露内网资源，加载前需要再次确认。
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
    // 首次发送前统一展示隐私说明，之后用 local storage 记录用户已确认。
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
    // 附件状态和预览 UI 同步更新；真正发送后会立即清除本地附件。
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
    // 框选层注入到当前页中执行，只返回矩形坐标，不读取页面 DOM 内容。
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
          // 无论确认或取消，都移除事件监听与覆盖层，避免干扰原页面。
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
    // captureVisibleTab 返回整张可视区截图，这里按用户框选区域裁剪成独立 PNG。
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
    // 截图流程分三步：请求权限、注入框选层、裁剪并作为聊天附件。
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
    // 图片工具页的 DOM 查询集中在这里，减少各流程直接散落选择器。
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
    // 转换结果同时用于预览、复制和下载链接，三处必须保持同一份 data URL。
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
    // 图片工具把 Data URL 或远程图片规范化为 PNG Data URL，供复制给其他模型入口。
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
        // 不携带 cookie、不跟随跳转，降低把用户登录态带到第三方图片请求的风险。
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
    // 右键菜单写入 session 的动作必须带来源、用户手势和时效，避免旧动作被误执行。
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
    // 同一个 actionId 只处理一次，防止 onMessage 与 storage 监听同时触发造成重复填充。
    if (!validatePendingAction(action)) return false;
    if (handledAutoSendActionIds.has(action.actionId)) return true;
    handledAutoSendActionIds.add(action.actionId);
    if (!confirmPendingAction(action)) return false;

    if (action.type === 'AUTO_SEND_PROMPT') return handleAutoSendPromptAction(action);
    if (action.type === 'AUTO_IMAGE_TOOL') return handleAutoImageToolAction(action);
    return false;
  }

  // 顶部标签只负责切换可见面板，不重置各面板内部状态。
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      activateTab(e.currentTarget.dataset.target);
    });
  });

  // 设置页初始化：API Key 只显示保存状态，不回填明文。
  const config = await chrome.storage.local.get(['apiUrl', 'modelName']);
  const storedCredential = await getStoredApiCredential();
  const apiUrlInput = document.getElementById('apiUrl');
  const apiKeyInput = document.getElementById('apiKey');
  const modelNameInput = document.getElementById('modelName');
  const apiKeyStatus = document.getElementById('apiKeyStatus');
  const saveMsg = document.getElementById('saveMsg');

  function updateApiKeyStatus(hasKey, apiKeyUrl = '') {
    // 输入框留空表示保留已保存密钥，避免无意中把密钥暴露到页面 DOM。
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
    // 保存前先校验 URL 白名单与 API Key 绑定关系，防止把旧密钥发往新服务。
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

  // 聊天气泡与 Markdown 渲染辅助函数。
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
    // 所有模型输出都经 Markdown 渲染与 HTML 白名单过滤，再替换到气泡中。
    const template = document.createElement('template');
    template.innerHTML = renderMarkdown(markdown);
    container.replaceChildren(template.content.cloneNode(true));
  }

  function extractSourceIds(text) {
    // 后端引用格式形如 [S1] 或 [S1, S2]，前端只识别固定来源编号。
    return String(text || '')
      .split(/[\s,，]+/)
      .map((token) => token.trim().toUpperCase())
      .filter((token) => SOURCE_ID_PATTERN.test(token));
  }

  function normalizeSourceCitationText(text, sources) {
    // 流式输出可能引用不存在的来源；最终渲染前剔除无效编号。
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
    // 把正文中的来源编号替换成可点击按钮；代码块、链接和来源面板内文本保持原样。
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
    return previewText ? summarizeInlineText(previewText, '', 56) : '';
  }

  function setSourcePanelOpen(container, isOpen) {
    // 同一时间只展开一个回答的来源面板，避免侧边栏空间被多个面板占满。
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
    // 每个回答气泡只绑定一次委托事件，后续流式重渲染不会重复绑定。
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
    // 来源列表由摘要区和详情区组成，点击正文引用或摘要按钮都会定位到同一来源。
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
      const previewText = buildSourceSummaryPreview(source);
      summaryPreview.textContent = previewText;

      summaryButton.append(summaryHeader, summaryMeta);
      if (previewText) {
        summaryButton.appendChild(summaryPreview);
      }
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
    detailTitle.textContent = '来源信息';

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
      if (preview.textContent) {
        item.appendChild(preview);
      }
      detailPanel.appendChild(item);
    });

    body.appendChild(detailPanel);
    wrapper.appendChild(body);
    container.appendChild(wrapper);
  }

  function renderStoredMessage(message) {
    // 从历史记录恢复消息时只重建可见 UI，不重新触发后端请求。
    const role = message?.role === 'user' ? 'user' : 'ai';
    const bubble = createMessageNode(role);
    bindSourceInteractions(bubble);
    const content = String(message?.display_content || '').trim();

    if (role === 'user') {
      bubble.textContent = content;
      return;
    }

    const sources = Array.isArray(message?.sources) ? message.sources : [];
    setRenderedMarkdown(bubble, normalizeSourceCitationText(content, sources));
    enhanceCodeBlocks(bubble);
    renderMathInContainer(bubble);
    decorateSourceCitations(bubble);
    renderCitedSources(bubble, sources);
  }

  function renderChatHistoryItems(chats) {
    // 历史列表只展示摘要元信息；点击后再按 chat_id 拉取完整消息。
    const list = document.getElementById('chatHistoryList');
    if (!list) return;

    list.replaceChildren();
    if (!Array.isArray(chats) || !chats.length) {
      const empty = document.createElement('div');
      empty.className = 'chat-history-item-meta';
      empty.textContent = '暂无历史对话';
      empty.style.padding = '10px';
      list.appendChild(empty);
      return;
    }

    chats.forEach((chat) => {
      const item = document.createElement('div');
      item.className = 'chat-history-item chat-history-entry';
      item.dataset.chatId = String(chat.chat_id || '').trim();

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chat-history-item-main';
      button.dataset.chatId = item.dataset.chatId;

      const title = document.createElement('div');
      title.className = 'chat-history-item-title';
      title.textContent = chat.title || chat.chat_id || '未命名对话';

      const meta = document.createElement('div');
      meta.className = 'chat-history-item-meta';
      const turnCount = Number(chat.turn_count || 0);
      meta.textContent = `${turnCount} 轮 · ${chat.latest_summary || chat.updated_at || ''}`;

      button.append(title, meta);
      const deleteButton = document.createElement('button');
      deleteButton.type = 'button';
      deleteButton.className = 'chat-history-delete-btn';
      deleteButton.dataset.chatId = item.dataset.chatId;
      deleteButton.title = '删除历史对话';
      deleteButton.textContent = '删除';

      item.append(button, deleteButton);
      list.appendChild(item);
    });
  }

  function normalizePlan(plan) {
    return plan && typeof plan === 'object' ? plan : null;
  }

  function getPlanRevision(plan) {
    return normalizePlan(plan)?.current_revision || null;
  }

  function getPlanExecutionSteps(plan) {
    // 执行计划优先使用服务端 steps；老数据没有 steps 时退回当前 revision 的 checklist。
    const normalizedPlan = normalizePlan(plan);
    if (!normalizedPlan) return [];
    const steps = Array.isArray(normalizedPlan.steps) ? normalizedPlan.steps : [];
    if (steps.length) {
      return steps
        .filter((step) => !['done', 'skipped'].includes(String(step.status || '').trim()))
        .map((step) => ({
          title: String(step.title || '').trim(),
          detail: String(step.detail || '').trim()
        }))
        .filter((step) => step.title || step.detail);
    }

    const revision = getPlanRevision(normalizedPlan) || {};
    const checklist = Array.isArray(revision.checklist) ? revision.checklist : [];
    return checklist
      .map((step) => ({
        title: String(step.title || '').trim(),
        detail: String(step.detail || '').trim()
      }))
      .filter((step) => step.title || step.detail);
  }

  function buildPlanExecutionPrompt(plan) {
    // 用户同意计划后，前端生成一条合成用户消息，让后端进入实际执行回合。
    const normalizedPlan = normalizePlan(plan);
    if (!normalizedPlan) return PLAN_AUTO_EXECUTE_PROMPT;
    const steps = getPlanExecutionSteps(normalizedPlan);
    const lines = [
      PLAN_AUTO_EXECUTE_PROMPT,
      '',
      `计划目标：${String(normalizedPlan.objective || '').trim()}`
    ];
    if (steps.length) {
      lines.push('执行步骤：');
      steps.forEach((step, index) => {
        const detail = step.detail ? `：${step.detail}` : '';
        lines.push(`${index + 1}. ${step.title}${detail}`);
      });
    }
    return lines.filter((line) => line !== '').join('\n');
  }

  function buildPlanExecutionSearchQuery(plan) {
    const normalizedPlan = normalizePlan(plan);
    if (!normalizedPlan) return '';
    const steps = getPlanExecutionSteps(normalizedPlan);
    return [
      String(normalizedPlan.objective || '').trim(),
      ...steps.flatMap((step) => [step.title, step.detail])
    ]
      .filter(Boolean)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 600);
  }

  function renderPlanPanel(plan) {
    // 计划面板跟随当前 chat 的 active plan，同一面板承载草稿、修订、执行和取消状态。
    const panel = document.getElementById('planPanel');
    if (!panel) return;
    const previousPlanId = planState.activePlan?.plan_id || '';
    const previousStatus = planState.activePlan?.status || '';
    const nextPlan = normalizePlan(plan);
    const nextPlanId = nextPlan?.plan_id || '';
    const nextStatus = nextPlan?.status || '';
    if (nextPlan && (nextPlanId !== previousPlanId || nextStatus !== previousStatus)) {
      planState.collapsed = nextStatus === 'executing';
    }
    planState.activePlan = nextPlan;
    planState.revising = false;
    panel.replaceChildren();
    if (!planState.activePlan) {
      panel.hidden = true;
      updateTaskUi();
      return;
    }

    const currentPlan = planState.activePlan;
    const revision = getPlanRevision(currentPlan) || {};
    const checklist = Array.isArray(revision.checklist) ? revision.checklist : [];
    const risks = Array.isArray(revision.risks) ? revision.risks : [];
    const acceptanceCriteria = Array.isArray(revision.acceptance_criteria) ? revision.acceptance_criteria : [];

    const head = document.createElement('div');
    head.className = 'plan-panel-head';
    const title = document.createElement('div');
    title.className = 'plan-panel-title';
    title.textContent = currentPlan.title || '当前计划';
    const status = document.createElement('div');
    status.className = 'plan-panel-status';
    status.textContent = currentPlan.status || 'draft';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'plan-panel-toggle';
    toggle.dataset.planAction = 'toggle';
    toggle.textContent = planState.collapsed ? '展开' : '收起';
    head.append(title, status, toggle);

    const body = document.createElement('div');
    body.className = 'plan-panel-body';
    body.hidden = Boolean(planState.collapsed);
    const objective = document.createElement('div');
    objective.className = 'plan-panel-objective';
    objective.textContent = currentPlan.objective || '';

    const list = document.createElement('ol');
    list.className = 'plan-panel-list';
    checklist.forEach((step) => {
      const item = document.createElement('li');
      const stepTitle = document.createElement('div');
      stepTitle.className = 'plan-panel-step-title';
      stepTitle.textContent = step.title || '';
      const detail = document.createElement('div');
      detail.className = 'plan-panel-step-detail';
      detail.textContent = step.detail || '';
      item.append(stepTitle);
      if (detail.textContent) item.append(detail);
      list.appendChild(item);
    });

    const meta = document.createElement('div');
    meta.className = 'plan-panel-meta';
    if (risks.length) {
      meta.appendChild(document.createTextNode(`风险：${risks.join('；')}`));
    }
    if (acceptanceCriteria.length) {
      if (meta.textContent) meta.appendChild(document.createElement('br'));
      meta.appendChild(document.createTextNode(`验收：${acceptanceCriteria.join('；')}`));
    }

    const actions = document.createElement('div');
    actions.className = 'plan-panel-actions';
    if (['draft', 'needs_revision', 'executing'].includes(currentPlan.status)) {
      const reviseButton = document.createElement('button');
      reviseButton.type = 'button';
      reviseButton.className = 'tool-btn secondary';
      reviseButton.dataset.planAction = 'revise';
      reviseButton.textContent = '要求修改';
      actions.appendChild(reviseButton);
    }
    if (['draft', 'needs_revision'].includes(currentPlan.status)) {
      const approveButton = document.createElement('button');
      approveButton.type = 'button';
      approveButton.className = 'tool-btn';
      approveButton.dataset.planAction = 'approve';
      approveButton.textContent = '同意开始';
      actions.appendChild(approveButton);
    }
    if (!['done', 'cancelled'].includes(currentPlan.status)) {
      const cancelButton = document.createElement('button');
      cancelButton.type = 'button';
      cancelButton.className = 'tool-btn secondary';
      cancelButton.dataset.planAction = 'cancel';
      cancelButton.textContent = '取消';
      actions.appendChild(cancelButton);
    }

    body.appendChild(objective);
    if (checklist.length) body.appendChild(list);
    if (meta.textContent) body.appendChild(meta);
    if (actions.childElementCount) body.appendChild(actions);
    panel.append(head, body);
    panel.hidden = false;
    updateTaskUi();
  }

  async function fetchBackendJson(safeApiUrl, apiKey, endpointPath, requestOptions = {}) {
    // 业务后端接口统一走 background 转发，避免侧边栏直接跨域请求。
    const requestHeaders = {
      'Content-Type': 'application/json'
    };
    if (String(apiKey || '').trim()) {
      requestHeaders.Authorization = `Bearer ${String(apiKey).trim()}`;
    }
    const method = String(requestOptions.method || 'GET').toUpperCase();
    const body = requestOptions.body === undefined
      ? (['POST', 'PATCH'].includes(method) ? '{}' : undefined)
      : JSON.stringify(requestOptions.body);

    return callApiJson(
      buildBackendEndpointUrl(safeApiUrl, endpointPath),
      {
        method,
        headers: requestHeaders,
        body
      }
    );
  }

  async function loadChatHistoryList() {
    const list = document.getElementById('chatHistoryList');
    if (!list) return;

    const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
    list.hidden = false;
    list.textContent = '正在加载历史对话...';
    const result = await fetchBackendJson(safeApiUrl, apiKey, CHAT_HISTORY_ENDPOINT_PATH);
    renderChatHistoryItems(result?.chats || []);
  }

  async function loadChatMessages(chatId) {
    // 切换历史对话会重置当前输入态与本地上下文，再按后端记录重建 UI。
    const normalizedChatId = String(chatId || '').trim();
    if (!normalizedChatId) return;

    const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
    const result = await fetchBackendJson(
      safeApiUrl,
      apiKey,
      `${CHAT_HISTORY_ENDPOINT_PATH}/${encodeURIComponent(normalizedChatId)}/messages`
    );

    currentChatId = normalizedChatId;
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: currentChatId });
    updatePageContextUi();
    conversationHistory = [];
    document.getElementById('chatHistory').replaceChildren();
    clearAttachedImage();

    const messages = Array.isArray(result?.messages) ? result.messages : [];
    messages.forEach((message) => {
      renderStoredMessage(message);
      conversationHistory.push({
        role: message.role === 'user' ? 'user' : 'assistant',
        content: String(message.display_content || '')
      });
    });
    conversationHistory = compactConversationHistory(conversationHistory);
    await loadActivePlanForChat(normalizedChatId);
    scrollToBottom();
  }

  async function loadActivePlanForChat(chatId) {
    const normalizedChatId = String(chatId || '').trim();
    if (!normalizedChatId) {
      renderPlanPanel(null);
      return;
    }
    try {
      const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
      const result = await fetchBackendJson(
        safeApiUrl,
        apiKey,
        `${CHAT_HISTORY_ENDPOINT_PATH}/${encodeURIComponent(normalizedChatId)}/plans/active`
      );
      renderPlanPanel(result?.plan || null);
    } catch {
      renderPlanPanel(null);
    }
  }

  async function deleteChatHistoryItem(chatId) {
    const normalizedChatId = String(chatId || '').trim();
    if (!normalizedChatId) return;
    const ok = confirm('删除这条历史对话？删除后不会再出现在历史列表中。');
    if (!ok) return;

    const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
    await fetchBackendJson(
      safeApiUrl,
      apiKey,
      `${CHAT_HISTORY_ENDPOINT_PATH}/${encodeURIComponent(normalizedChatId)}`,
      { method: 'DELETE' }
    );

    if (currentChatId === normalizedChatId) {
      conversationHistory = [];
      document.getElementById('chatHistory').replaceChildren();
      document.getElementById('chatInput').value = '';
      renderPlanPanel(null);
      clearAttachedImage();
      await resetCurrentChatId();
      updatePageContextUi();
    }
    await loadChatHistoryList();
  }

  function formatMemoryDebug(memory) {
    // 调试信息只用于侧边栏排查记忆写入任务，不参与对话上下文。
    const latestJob = memory?.debug?.latest_job;
    if (!latestJob) return '';

    const parts = [];
    if (latestJob.status) {
      parts.push(`job ${latestJob.status}`);
    }
    const warnings = Array.isArray(latestJob.validation_warnings)
      ? latestJob.validation_warnings.filter(Boolean)
      : [];
    if (warnings.length) {
      parts.push(`warnings: ${warnings.join(', ')}`);
    }
    const applied = Array.isArray(latestJob.applied)
      ? latestJob.applied
        .map((item) => [item?.action, item?.memory_id].filter(Boolean).join(':'))
        .filter(Boolean)
      : [];
    if (applied.length) {
      parts.push(`applied: ${applied.join(', ')}`);
    }
    return parts.join(' · ');
  }

  function renderMemoryItems(memories) {
    // 记忆列表展示类型、范围、证据和策略版本，方便判断某条记忆为什么会被保留。
    const list = document.getElementById('memoryList');
    if (!list) return;

    list.replaceChildren();
    if (!Array.isArray(memories) || !memories.length) {
      const empty = document.createElement('div');
      empty.className = 'chat-history-item-meta';
      empty.textContent = '暂无长期记忆';
      empty.style.padding = '10px';
      list.appendChild(empty);
      return;
    }

    memories.forEach((memory) => {
      const item = document.createElement('div');
      item.className = 'chat-history-item memory-item';
      item.dataset.memoryId = String(memory.memory_id || '').trim();

      const head = document.createElement('div');
      head.className = 'memory-item-head';

      const title = document.createElement('div');
      title.className = 'chat-history-item-title';
      const memoryType = String(memory.memory_type || 'memory');
      const taskStatus = String(memory.task_status || '').trim();
      title.textContent = memoryType === 'task_state' && taskStatus
        ? `${memoryType} · ${taskStatus}`
        : memoryType;

      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'memory-delete-btn';
      deleteBtn.title = '删除记忆';
      deleteBtn.textContent = '删除';

      head.append(title, deleteBtn);

      const content = document.createElement('div');
      content.className = 'memory-item-content';
      content.textContent = String(memory.content || '').trim();

      const meta = document.createElement('div');
      meta.className = 'chat-history-item-meta';
      const tags = Array.isArray(memory.tags) && memory.tags.length ? ` · ${memory.tags.join(', ')}` : '';
      const lastUsed = String(memory.last_used_at || '').trim();
      meta.textContent = `重要度 ${Number(memory.importance || 0).toFixed(2)}${tags}${lastUsed ? ` · 最近使用 ${lastUsed}` : ''}`;

      const evidence = document.createElement('div');
      evidence.className = 'chat-history-item-meta memory-item-evidence';
      evidence.textContent = String(memory.evidence || '').trim()
        ? `证据：${String(memory.evidence || '').trim()}`
        : '';

      const reason = document.createElement('div');
      reason.className = 'chat-history-item-meta memory-item-reason';
      reason.textContent = String(memory.classification_reason || '').trim()
        ? `分类原因：${String(memory.classification_reason || '').trim()}`
        : '';

      const policy = document.createElement('div');
      policy.className = 'chat-history-item-meta memory-item-policy';
      policy.textContent = String(memory.policy_version || '').trim()
        ? `策略版本：${String(memory.policy_version || '').trim()}`
        : '';

      const source = document.createElement('div');
      source.className = 'chat-history-item-meta memory-item-source';
      const sourceTurnId = String(memory.source_turn_id || '').trim();
      const scope = String(memory.scope || '').trim();
      const scopeChatId = String(memory.scope_chat_id || '').trim();
      const taskUpdatedBy = String(memory.task_updated_by || '').trim();
      const sourceParts = [];
      if (scope) sourceParts.push(`范围：${scope}${scopeChatId ? `/${scopeChatId}` : ''}`);
      if (taskStatus) sourceParts.push(`任务状态：${taskStatus}${taskUpdatedBy ? `/${taskUpdatedBy}` : ''}`);
      if (sourceTurnId) sourceParts.push(`来源轮次：${sourceTurnId}`);
      source.textContent = sourceParts.join(' · ');

      const debug = document.createElement('div');
      debug.className = 'chat-history-item-meta memory-item-debug';
      const debugText = formatMemoryDebug(memory);
      debug.textContent = debugText ? `调试：${debugText}` : '';

      item.append(head, content, meta);
      if (evidence.textContent) item.appendChild(evidence);
      if (reason.textContent) item.appendChild(reason);
      if (source.textContent) item.appendChild(source);
      if (debug.textContent) item.appendChild(debug);
      if (policy.textContent) item.appendChild(policy);
      list.appendChild(item);
    });
  }

  async function handlePlanSend(queryText) {
    // 计划模式不直接调用普通聊天接口，而是创建或修订后端计划对象。
    const input = document.getElementById('chatInput');
    const objective = String(queryText || '').trim();
    if (!objective) return;
    let apiKey = '';
    let modelName = '';
    let safeApiUrl = '';
    try {
      ({ apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true }));
    } catch (error) {
      alert(error.message || 'API 配置无效');
      return;
    }
    if (!(await ensurePrivacyNoticeAccepted())) return;

    const pageContextResult = await resolvePageContextForSend();
    if (!pageContextResult) return;
    const currentPage = pageContextResult.currentPage;
    const safeModelName = String(modelName || '').trim() || 'gpt-3.5-turbo';
    const isRevision = Boolean(planState.activePlan && ['draft', 'needs_revision', 'executing'].includes(planState.activePlan.status));
    const chatId = isRevision ? await getOrCreateCurrentChatId() : await resetCurrentChatId();

    input.value = '';
    if (!isRevision) {
      document.getElementById('chatHistory').replaceChildren();
      conversationHistory = [];
      renderPlanPanel(null);
      const historyList = document.getElementById('chatHistoryList');
      if (historyList) {
        historyList.hidden = true;
        historyList.replaceChildren();
      }
    }
    const userBubble = createMessageNode('user');
    userBubble.textContent = objective;
    scrollToBottom();

    const aiBubble = createMessageNode('ai');
    showTypingIndicator(aiBubble);
    scrollToBottom();

    const endpointPath = isRevision
      ? `${PLAN_ENDPOINT_PREFIX}/${encodeURIComponent(planState.activePlan.plan_id)}/revise`
      : `${CHAT_HISTORY_ENDPOINT_PATH}/${encodeURIComponent(chatId)}/plans`;
    const body = isRevision
      ? {
          model: safeModelName,
          feedback: objective
        }
      : {
          model: safeModelName,
          objective,
          context_options: {
            use_current_page: pageContextState.enabled,
            use_web_search: webSearchState.enabled,
            force_refresh_page: Boolean(pageContextState.enabled && pageContextState.forceRefreshPage),
            web_search_query: ''
          },
          current_page: currentPage || null
        };

    try {
      const result = await fetchBackendJson(safeApiUrl, apiKey, endpointPath, {
        method: 'POST',
        body
      });
      aiBubble.remove();
      renderPlanPanel(result?.plan || null);
    } catch (error) {
      aiBubble.textContent = error.message || '计划生成失败';
    }
  }

  async function loadMemoryList() {
    const list = document.getElementById('memoryList');
    if (!list) return;

    const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
    const controls = document.getElementById('memoryControls');
    if (controls) controls.hidden = false;
    list.hidden = false;
    list.textContent = '正在加载长期记忆...';
    const selectedType = String(document.getElementById('memoryTypeFilter')?.value || '').trim();
    const params = new URLSearchParams({ include_debug: 'true' });
    if (MEMORY_TYPE_FILTER_OPTIONS.has(selectedType)) {
      params.set('memory_type', selectedType);
    }
    const result = await fetchBackendJson(safeApiUrl, apiKey, `${MEMORY_ENDPOINT_PATH}?${params.toString()}`);
    renderMemoryItems(result?.memories || []);
  }

  async function deleteMemoryItem(memoryId) {
    const normalizedMemoryId = String(memoryId || '').trim();
    if (!normalizedMemoryId) return;

    const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
    await fetchBackendJson(
      safeApiUrl,
      apiKey,
      `${MEMORY_ENDPOINT_PATH}/${encodeURIComponent(normalizedMemoryId)}`,
      { method: 'DELETE' }
    );
    await loadMemoryList();
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

  async function sendStatefulTextChat({
    queryText,
    taskType = 'chat',
    focusText = '',
    displayUserText = '',
    contextOptions = {},
    currentTurnMeta = {},
    requireBackendApi = false,
    forceStatefulBackend = false,
    clearInput = false
  } = {}) {
    // 文本聊天统一走这里：按功能需要选择直连模型或后端有状态接口。
    const normalizedTaskType = normalizeTaskType(taskType);
    const normalizedQueryText = String(queryText || '').trim();
    const normalizedFocusText = String(focusText || '').trim();
    const visibleUserText = String(displayUserText || normalizedQueryText).trim();
    if (normalizedTaskType === 'chat' && !normalizedQueryText) return false;

    const useCurrentPage = contextOptions.use_current_page === undefined
      ? pageContextState.enabled
      : Boolean(contextOptions.use_current_page);
    const useWebSearch = contextOptions.use_web_search === undefined
      ? webSearchState.enabled
      : Boolean(contextOptions.use_web_search);
    const forceRefreshPage = contextOptions.force_refresh_page === undefined
      ? Boolean(useCurrentPage && pageContextState.forceRefreshPage)
      : Boolean(contextOptions.force_refresh_page);
    const webSearchQuery = String(contextOptions.web_search_query || '');

    let apiKey = '';
    let modelName = '';
    let safeApiUrl = '';
    try {
      ({ apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig({
        requireBackendApi: requireBackendApi || forceStatefulBackend || useCurrentPage || useWebSearch
      }));
    } catch (error) {
      alert(error.message || 'API 配置无效');
      return false;
    }
    if (!(await ensurePrivacyNoticeAccepted())) return false;

    let pageContextResult = { currentPage: null, pageContextId: '' };
    if (useCurrentPage) {
      pageContextResult = await resolvePageContextForSend();
      if (!pageContextResult) return false;
    }
    const currentPage = pageContextResult.currentPage;
    const safeModelName = String(modelName || '').trim() || 'gpt-3.5-turbo';
    if (clearInput) {
      const input = document.getElementById('chatInput');
      if (input) input.value = '';
    }

    const userBubble = createMessageNode('user');
    if (normalizedTaskType !== 'chat') {
      renderUserTaskSummary(userBubble, normalizedTaskType, normalizedFocusText, normalizedQueryText);
    } else if (visibleUserText) {
      const userTextNode = document.createElement('div');
      userTextNode.textContent = visibleUserText;
      userBubble.appendChild(userTextNode);
    }
    scrollToBottom();

    conversationHistory.push({
      role: 'user',
      content: buildConversationUserContent(normalizedTaskType, normalizedFocusText, normalizedQueryText)
    });

    const aiBubble = createMessageNode('ai');
    bindSourceInteractions(aiBubble);
    showTypingIndicator(aiBubble);
    scrollToBottom();

    const msgId = createMessageId();
    const chatId = await getOrCreateCurrentChatId();
    let fullReply = '';
    let citedSources = [];
    let isStreamDone = false;
    let finalizeTimer = null;
    const safeApiHost = new URL(safeApiUrl).hostname;
    const useStatefulBackend = forceStatefulBackend
      || useCurrentPage
      || useWebSearch
      || isPrivateOrLocalHost(safeApiHost);
    // 后端有状态接口接收 current_turn；直连 OpenAI 时只能发送标准 messages。
    const requestBody = useStatefulBackend
      ? {
          model: safeModelName,
          stream: true,
          chat_id: chatId,
          current_turn: {
            task_type: normalizedTaskType,
            query_text: normalizedQueryText,
            focus_text: normalizedFocusText,
            origin: String(currentTurnMeta.origin || 'user'),
            synthetic_user: Boolean(currentTurnMeta.synthetic_user),
            plan_id: String(currentTurnMeta.plan_id || '')
          },
          context_options: {
            use_current_page: useCurrentPage,
            use_web_search: useWebSearch,
            force_refresh_page: forceRefreshPage,
            web_search_query: webSearchQuery
          }
        }
      : {
          model: safeModelName,
          messages: buildMessagesPayload(conversationHistory),
          stream: true,
          task_type: normalizedTaskType,
          focus_text: normalizedFocusText,
          query_text: normalizedQueryText,
          chat_id: chatId,
          use_current_page: useCurrentPage,
          force_refresh_page: forceRefreshPage,
          use_web_search: useWebSearch,
          web_search_query: webSearchQuery
        };
    conversationHistory = compactConversationHistory(conversationHistory);
    if (pageContextResult.pageContextId) {
      requestBody.page_context_id = pageContextResult.pageContextId;
    }
    if (currentPage) {
      requestBody.current_page = currentPage;
    }
    if (forceRefreshPage) {
      pageContextState.forceRefreshPage = false;
      updatePageContextUi();
    }

    const requestHeaders = {
      'Content-Type': 'application/json'
    };
    if (String(apiKey || '').trim()) {
      requestHeaders.Authorization = `Bearer ${String(apiKey).trim()}`;
    }

    return new Promise((resolve) => {
      const finalizeAssistantResponse = () => {
        // LLM_DONE 可能早于最后一段文本到达，短暂延迟后再把回答写入本地历史。
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
        resolve({ ok: true, content: fullReply, sources: citedSources });
      };

      const messageListener = (msg) => {
        // background 会广播多个请求的流消息，msgId 用来只接收本次请求。
        if (msg.msgId !== msgId) return;

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
        } else if (msg.type === 'LLM_FINAL_TEXT') {
          fullReply = String(msg.content || '');
          setRenderedMarkdown(aiBubble, fullReply);
          enhanceCodeBlocks(aiBubble);
          renderMathInContainer(aiBubble);
          decorateSourceCitations(aiBubble);
          if (citedSources.length) {
            renderCitedSources(aiBubble, citedSources);
          }
          scrollToBottom();
        } else if (msg.type === 'LLM_SOURCES') {
          citedSources = Array.isArray(msg.sources) ? msg.sources : [];
          fullReply = normalizeSourceCitationText(fullReply, citedSources);
          setRenderedMarkdown(aiBubble, fullReply);
          enhanceCodeBlocks(aiBubble);
          renderMathInContainer(aiBubble);
          decorateSourceCitations(aiBubble);
          renderCitedSources(aiBubble, citedSources);
          scrollToBottom();
        } else if (msg.type === 'LLM_DONE') {
          if (isStreamDone) return;
          isStreamDone = true;
          finalizeTimer = setTimeout(finalizeAssistantResponse, 150);
        } else if (msg.type === 'LLM_ERROR') {
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
          resolve({ ok: false, error: msg.error });
        }
      };

      chrome.runtime.onMessage.addListener(messageListener);
      chrome.runtime.sendMessage({
        type: 'CALL_LLM_STREAM',
        msgId,
        url: `${safeApiUrl}/chat/completions`,
        options: {
          method: 'POST',
          headers: requestHeaders,
          body: JSON.stringify(requestBody)
        }
      });
    });
  }

  // 发送按钮入口：根据当前任务、附件和上下文开关分派到对应发送流程。
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
    if (webSearchState.enabled && hasImage) {
      alert('联网搜索只支持文本提问，请先移除图片附件。');
      return;
    }
    if (taskType === 'plan') {
      await handlePlanSend(queryText);
      return;
    }
    if (!hasImage) {
      await sendStatefulTextChat({
        queryText,
        taskType,
        focusText,
        clearInput: true
      });
      return;
    }

    let apiKey = '';
    let modelName = '';
    let safeApiUrl = '';
    try {
      ({ apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig({
        requireBackendApi: pageContextState.enabled || webSearchState.enabled
      }));
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

    // 先把用户消息画到页面上，后台流式响应再逐块填充 AI 气泡。
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

    // conversationHistory 是发给直连模型的短期上下文，不等同于后端长期记忆。
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

    // 创建 AI 等待气泡后开始监听 background 传回的流式消息。
    const aiBubble = createMessageNode('ai');
    bindSourceInteractions(aiBubble);
    showTypingIndicator(aiBubble);
    scrollToBottom();

    const msgId = createMessageId(); // 唯一请求 ID，用于匹配本次流式响应。
    const chatId = await getOrCreateCurrentChatId();
    let fullReply = ''; // 用于拼接流式文本。
    let citedSources = [];
    let isStreamDone = false;
    let finalizeTimer = null;
    const safeApiHost = new URL(safeApiUrl).hostname;
    const useStatefulBackend = !hasImage
      && (pageContextState.enabled || webSearchState.enabled || isPrivateOrLocalHost(safeApiHost));
    const requestBody = useStatefulBackend
      ? {
          model: safeModelName,
          stream: true,
          chat_id: chatId,
          current_turn: {
            task_type: taskType,
            query_text: queryText,
            focus_text: focusText
          },
          context_options: {
            use_current_page: pageContextState.enabled,
            use_web_search: webSearchState.enabled,
            force_refresh_page: Boolean(pageContextState.enabled && pageContextState.forceRefreshPage),
            web_search_query: ''
          }
        }
      : {
          model: safeModelName,
          messages: buildMessagesPayload(conversationHistory),
          stream: true,
          task_type: taskType,
          focus_text: focusText,
          query_text: queryText,
          chat_id: chatId,
          use_current_page: pageContextState.enabled,
          force_refresh_page: Boolean(pageContextState.enabled && pageContextState.forceRefreshPage),
          use_web_search: webSearchState.enabled,
          web_search_query: ''
        };
    conversationHistory = compactConversationHistory(conversationHistory);
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

    // 向 background.js 发出流式请求指令；真正的 fetch 在后台脚本中执行。
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

    // 监听后台传回的文本块、最终文本、引用来源和错误状态。
    const messageListener = (msg) => {
      if (msg.msgId !== msgId) return; // 过滤非本次请求的流。
      
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
      else if (msg.type === 'LLM_FINAL_TEXT') {
        fullReply = String(msg.content || '');
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

  // 页面初始化与交互事件绑定。
  updateTaskUi();
  updatePageContextUi();
  getOrCreateCurrentChatId()
    .then((chatId) => loadActivePlanForChat(chatId))
    .catch(console.error);

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

  document.getElementById('useWebSearchToggle')?.addEventListener('change', (event) => {
    webSearchState.enabled = Boolean(event.currentTarget?.checked);
    updatePageContextUi();
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

  document.getElementById('planPanel')?.addEventListener('click', async (event) => {
    // 计划面板使用事件委托处理折叠、修订、同意和取消四类动作。
    const button = event.target.closest('[data-plan-action]');
    if (!button || !planState.activePlan?.plan_id) return;
    const action = button.dataset.planAction;
    if (action === 'toggle') {
      planState.collapsed = !planState.collapsed;
      renderPlanPanel(planState.activePlan);
      return;
    }
    if (action === 'revise') {
      planState.revising = true;
      setTaskState('plan', '', 'manual');
      document.getElementById('chatInput')?.focus();
      updateTaskUi();
      return;
    }

    try {
      if (planState.actionPending) return;
      planState.actionPending = true;
      button.disabled = true;
      const { apiKey, safeApiUrl } = await resolveApiRequestConfig({ requireBackendApi: true });
      const endpoint = `${PLAN_ENDPOINT_PREFIX}/${encodeURIComponent(planState.activePlan.plan_id)}/${action}`;
      const result = await fetchBackendJson(safeApiUrl, apiKey, endpoint, { method: 'POST' });
      const userText = action === 'approve' ? '同意开始执行计划' : '取消计划';
      const assistantText = String(result?.display_message || (action === 'approve' ? '计划已进入执行。' : '计划已取消。'));
      const userBubble = createMessageNode('user');
      userBubble.textContent = userText;
      const aiBubble = createMessageNode('ai');
      setRenderedMarkdown(aiBubble, assistantText);
      renderPlanPanel(result?.plan || null);
      scrollToBottom();
      if (action === 'approve') {
        const executionPlan = planState.activePlan || result?.plan || null;
        const executionResult = await sendStatefulTextChat({
          queryText: buildPlanExecutionPrompt(executionPlan),
          taskType: 'chat',
          focusText: '',
          displayUserText: '开始执行当前计划',
          contextOptions: {
            use_current_page: pageContextState.enabled,
            use_web_search: webSearchState.enabled,
            force_refresh_page: Boolean(pageContextState.enabled && pageContextState.forceRefreshPage),
            web_search_query: buildPlanExecutionSearchQuery(executionPlan)
          },
          currentTurnMeta: {
            origin: 'plan_auto_execution',
            synthetic_user: true,
            plan_id: String(planState.activePlan?.plan_id || '')
          },
          requireBackendApi: true,
          forceStatefulBackend: true
        });
        if (executionResult?.ok) {
          const completed = await fetchBackendJson(
            safeApiUrl,
            apiKey,
            `${PLAN_ENDPOINT_PREFIX}/${encodeURIComponent(planState.activePlan.plan_id)}/complete`,
            { method: 'POST' }
          );
          renderPlanPanel(completed?.plan || null);
        }
      }
    } catch (error) {
      alert(error.message || '计划操作失败');
    } finally {
      planState.actionPending = false;
      if (button.isConnected) {
        button.disabled = false;
      }
    }
  });

  document.querySelectorAll('.task-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const nextTaskType = normalizeTaskType(btn.dataset.taskType);
      if (!taskState.focusText && !['chat', 'plan'].includes(nextTaskType)) {
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
    const historyList = document.getElementById('chatHistoryList');
    if (historyList) {
      historyList.hidden = true;
      historyList.replaceChildren();
    }
    const memoryList = document.getElementById('memoryList');
    if (memoryList) {
      memoryList.hidden = true;
      memoryList.replaceChildren();
    }
    const memoryControls = document.getElementById('memoryControls');
    if (memoryControls) {
      memoryControls.hidden = true;
    }
    conversationHistory = []; // 清除当前侧边栏短期上下文。
    document.getElementById('chatInput').value = '';
    renderPlanPanel(null);
    clearAttachedImage();
    await resetCurrentChatId();
  });

  document.getElementById('toggleChatHistoryBtn')?.addEventListener('click', async () => {
    const historyList = document.getElementById('chatHistoryList');
    if (!historyList) return;

    if (!historyList.hidden) {
      historyList.hidden = true;
      return;
    }

    try {
      const memoryList = document.getElementById('memoryList');
      if (memoryList) {
        memoryList.hidden = true;
      }
      const memoryControls = document.getElementById('memoryControls');
      if (memoryControls) {
        memoryControls.hidden = true;
      }
      await loadChatHistoryList();
    } catch (error) {
      historyList.hidden = false;
      historyList.textContent = error.message || '加载历史对话失败';
    }
  });

  document.getElementById('toggleMemoryBtn')?.addEventListener('click', async () => {
    const memoryList = document.getElementById('memoryList');
    if (!memoryList) return;

    if (!memoryList.hidden) {
      memoryList.hidden = true;
      const memoryControls = document.getElementById('memoryControls');
      if (memoryControls) {
        memoryControls.hidden = true;
      }
      return;
    }

    try {
      const historyList = document.getElementById('chatHistoryList');
      if (historyList) {
        historyList.hidden = true;
      }
      await loadMemoryList();
    } catch (error) {
      memoryList.hidden = false;
      const memoryControls = document.getElementById('memoryControls');
      if (memoryControls) {
        memoryControls.hidden = false;
      }
      memoryList.textContent = error.message || '加载长期记忆失败';
    }
  });

  document.getElementById('memoryTypeFilter')?.addEventListener('change', async () => {
    const memoryList = document.getElementById('memoryList');
    if (!memoryList || memoryList.hidden) return;
    try {
      await loadMemoryList();
    } catch (error) {
      memoryList.textContent = error.message || '加载长期记忆失败';
    }
  });

  document.getElementById('memoryList')?.addEventListener('click', async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const deleteButton = target?.closest('.memory-delete-btn');
    if (!deleteButton) return;

    const item = deleteButton.closest('.memory-item');
    try {
      await deleteMemoryItem(item?.dataset.memoryId || '');
    } catch (error) {
      alert(error.message || '删除记忆失败');
    }
  });

  document.getElementById('chatHistoryList')?.addEventListener('click', async (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const deleteButton = target?.closest('.chat-history-delete-btn');
    if (deleteButton) {
      const item = deleteButton.closest('.chat-history-item');
      try {
        await deleteChatHistoryItem(item?.dataset.chatId || '');
      } catch (error) {
        alert(error.message || '删除历史对话失败');
      }
      return;
    }

    const item = target?.closest('.chat-history-item');
    if (!item) return;

    try {
      await loadChatMessages(item.dataset.chatId || '');
      document.getElementById('chatHistoryList').hidden = true;
    } catch (error) {
      alert(error.message || '加载历史消息失败');
    }
  });

  // 快捷指令只填入输入框，让用户仍可编辑后再发送。
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
    // 常见办公/账号域名或敏感关键词页面读取前会弹出额外确认。
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

  // 接收右键菜单传来的划词或图片动作；storage 监听覆盖侧边栏尚未打开的情况。
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
