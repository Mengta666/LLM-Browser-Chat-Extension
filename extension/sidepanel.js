document.addEventListener('DOMContentLoaded', async () => {
  let attachedImage = null;
  let currentChatId = '';
  // 轻量聊天的多轮历史(user/assistant 交替);带给后端做上下文 + 记忆抽取。
  // 只保留最近若干轮,防无限增长(图片消息不入历史,避免 base64 累积撑爆请求)。
  let chatMessages = [];
  const MAX_CHAT_HISTORY_MESSAGES = 20;
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
  const MAX_IMAGE_PIXELS = 20_000_000;
  const MAX_URL_LENGTH = 2048;
  const PRIVACY_NOTICE_KEY = 'privacyNoticeAccepted';
  const CURRENT_CHAT_ID_KEY = 'currentChatId';
  const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
  const ALLOWED_IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  const DEFAULT_API_BASE_URLS = new Set([
    'https://api.openai.com/v1'
  ]);
  const DEFAULT_API_URL = 'https://api.openai.com/v1';

  // 取当前活动标签页:所有截图/观察/执行动作的入口(observePageState、executePageAction 等)。
  // 两种 query 兜底:优先 lastFocusedWindow(侧边栏聚焦时更准),再退 currentWindow。
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

  // ── Markdown 渲染管线 ──────────────────────────────────────────────
  // marked(解析) → 补全未闭合行内标记(仅流式期) → DOMPurify(消毒) → innerHTML
  //   → KaTeX(仅流末) → 代码块复制钮。依赖为 vendor 全局,缺失时降级为纯文本。
  if (window.marked && typeof window.marked.setOptions === 'function') {
    window.marked.setOptions({ gfm: true, breaks: true });
  }

  // DOMPurify:允许链接开新标签,并给外链补 rel。class 属性默认在白名单内,
  // KaTeX / 代码高亮的 class 得以保留(未自定义 ALLOWED_ATTR,不会被剥)。
  if (window.DOMPurify && typeof window.DOMPurify.addHook === 'function') {
    window.DOMPurify.addHook('afterSanitizeAttributes', (node) => {
      if (node.tagName === 'A' && node.hasAttribute('href')) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    });
  }

  // 流式期间补全尾部悬空的行内标记,避免 `**bold` 在闭合符到达前以字面 `**` 闪现。
  // 只处理最后一个未闭合标记的常见场景;代码块 fence 由 marked 自动闭合,无需干预。
  function closeOpenMarks(buffer) {
    let text = buffer || '';
    // fence 代码块内部不补(奇数个 ``` 说明正处于未闭合代码块,marked 会自渲)。
    const fenceCount = (text.match(/```/g) || []).length;
    if (fenceCount % 2 === 1) return text;
    // 行内 code:奇数个反引号 → 末尾补一个。
    const backticks = (text.match(/`/g) || []).length;
    if (backticks % 2 === 1) text += '`';
    // 未完成链接 `[text](` 或 `[text` → 降级为纯文本,避免半截链接语法。
    if (/\[[^\]]*\]\([^)]*$/.test(text) || /\[[^\]]*$/.test(text)) {
      text = text.replace(/\[([^\]]*)\]\([^)]*$/, '$1').replace(/\[([^\]]*)$/, '$1');
    }
    // 加粗 **:成对补全(不误伤 `- ` 列表项后的裸 **,故要求 ** 后紧跟非空白)。
    const boldCount = (text.match(/\*\*/g) || []).length;
    if (boldCount % 2 === 1 && /\*\*\S[^*]*$/.test(text)) text += '**';
    return text;
  }

  // 给渲染后的每个 <pre> 包一层 .code-block 并加复制按钮(复用已有 CSS)。
  function addCodeCopyButtons(el) {
    el.querySelectorAll('pre').forEach((pre) => {
      if (pre.parentElement && pre.parentElement.classList.contains('code-block')) return;
      const wrap = document.createElement('div');
      wrap.className = 'code-block';
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      const btn = document.createElement('button');
      btn.className = 'code-copy-btn';
      btn.type = 'button';
      btn.textContent = '复制';
      btn.addEventListener('click', () => {
        const code = pre.innerText;
        navigator.clipboard.writeText(code).then(() => {
          btn.textContent = '已复制';
          setTimeout(() => { btn.textContent = '复制'; }, 1500);
        }).catch(() => { btn.textContent = '复制失败'; });
      });
      wrap.appendChild(btn);
    });
  }

  // 把 markdown 文本渲染进目标节点。opts.streaming=true 时补全未闭合标记且跳过 KaTeX;
  // 流末(streaming=false)用原始文本渲染并跑一次 KaTeX。marked 缺失时降级纯文本。
  function renderMarkdownInto(el, text, opts) {
    const streaming = opts && opts.streaming;
    const src = streaming ? closeOpenMarks(text) : (text || '');
    let html;
    if (window.marked && typeof window.marked.parse === 'function') {
      html = window.marked.parse(src);
      if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
        html = window.DOMPurify.sanitize(html);
      }
      el.innerHTML = html;
      addCodeCopyButtons(el);
      if (!streaming && typeof window.renderMathInElement === 'function') {
        try {
          window.renderMathInElement(el, {
            delimiters: [
              { left: '$$', right: '$$', display: true },
              { left: '\\[', right: '\\]', display: true },
              { left: '\\(', right: '\\)', display: false },
              { left: '$', right: '$', display: false }
            ],
            throwOnError: false
          });
        } catch (_) { /* KaTeX 失败不影响已渲染的文本 */ }
      }
    } else {
      el.textContent = src;
    }
  }

  // rAF 节流:流式期间把重渲染对齐到显示器刷新率,避免每个 token 都重解析。
  function createMarkdownStreamer(el) {
    let pending = '';
    let frame = 0;
    const flush = () => {
      frame = 0;
      renderMarkdownInto(el, pending, { streaming: true });
    };
    return {
      update(text) {
        pending = text;
        if (!frame) frame = requestAnimationFrame(flush);
      },
      finalize(text) {
        if (frame) { cancelAnimationFrame(frame); frame = 0; }
        renderMarkdownInto(el, text, { streaming: false });
      },
      cancel() {
        if (frame) { cancelAnimationFrame(frame); frame = 0; }
      }
    };
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
      return currentChatId;
    }

    currentChatId = createChatId();
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: currentChatId });
    return currentChatId;
  }

  async function resetCurrentChatId() {
    currentChatId = createChatId();
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: currentChatId });
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

  // 会话历史 + 记忆管理 CRUD:走 background 的 CALL_BACKEND_API 通道(支持 GET/POST/PATCH/DELETE)。
  function callBackendApi(url, method = 'GET', body = null) {
    const options = { method, headers: { 'Content-Type': 'application/json' } };
    if (body != null) options.body = JSON.stringify(body);
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(
        { type: 'CALL_BACKEND_API', url, options },
        (response) => {
          const runtimeError = chrome.runtime.lastError;
          if (runtimeError) { reject(new Error(runtimeError.message || '后台请求失败')); return; }
          if (!response?.ok) { reject(new Error(response?.error || '后台请求失败')); return; }
          resolve(response.body);
        }
      );
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

  function activateTab(target) {
    document.querySelectorAll('.tab-btn').forEach((button) => {
      button.classList.toggle('active', button.dataset.target === target);
    });

    document.querySelectorAll('.tab-content').forEach((content) => {
      content.classList.toggle('active', content.id === target);
    });
  }

  // 1. Tab 切换逻辑
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const target = e.currentTarget.dataset.target;
      activateTab(target);
      // 记忆 tab:切回时刷新列表;modal 只在 idle 时才隐藏(running/done 保持显示)
      if (target === 'memory') {
        const modal = document.getElementById('rethinkModal');
        if (modal && _rethinkState === 'idle') modal.style.display = 'none';
        loadMemoryPanel().catch((err) => console.warn('加载记忆面板失败', err));
      }
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
  // 发送分流：自动化开关开(或 /browser-operation 前缀)→ 启动 agent；否则走轻量直连聊天。
  async function handleSend() {
    if (_sendingLock) return;
    if (agentState.active) return;
    const input = document.getElementById('chatInput');
    const text = (input.value || '').trim();
    if (!text) return;
    if (text.length > MAX_PROMPT_LENGTH) {
      alert(`单次发送不能超过 ${MAX_PROMPT_LENGTH} 字。`);
      return;
    }

    _sendingLock = true;
    const sendBtn = document.getElementById('sendBtn');
    if (sendBtn) sendBtn.disabled = true;
    try {
      const image = attachedImage ? attachedImage.dataUrl : '';
      if (shouldUseAgent(text)) {
        // 自动化模式：去掉命令前缀,启动 agent,图片作视觉输入
        const task = text.startsWith(AGENT_COMMAND.trim())
          ? text.slice(AGENT_COMMAND.trim().length).trim()
          : text;
        input.value = '';
        if (attachedImage) clearAttachedImage();
        await runAgentTask(task, image);
      } else {
        input.value = '';
        if (attachedImage) clearAttachedImage();
        // 搜索模式开启时,把输入文本作为 search_query 强制搜索;发送后自动关闭搜索模式
        const searchQuery = isSearchMode() ? text : '';
        if (searchQuery) {
          const st = document.getElementById('webSearchToggle');
          const sb = document.getElementById('webSearchBtn');
          if (st) st.checked = false;
          if (sb) sb.classList.remove('is-active');
        }
        await runPlainChat(text, image, searchQuery);
      }
    } finally {
      _sendingLock = false;
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  // 轻量直连聊天：不点自动化按钮时,消息直接发给用户配置的 OpenAI 兼容接口,流式返回。
  // search_query 非空时附带搜索参数(手动搜索)。
  async function runPlainChat(text, image, search_query = '') {
    let apiKey, modelName, safeApiUrl;
    try {
      ({ apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig());
    } catch (error) {
      alert(error.message || 'API 配置无效');
      return;
    }
    if (!(await ensurePrivacyNoticeAccepted())) return;

    const safeModelName = String(modelName || '').trim() || 'gpt-4o';

    // 用户气泡
    const userBubble = createMessageNode('user');
    const userTextNode = document.createElement('div');
    userTextNode.textContent = text;
    userBubble.appendChild(userTextNode);
    if (image) {
      const previewImage = document.createElement('img');
      previewImage.className = 'user-upload-preview';
      previewImage.src = image;
      previewImage.alt = '上传图片';
      userBubble.appendChild(previewImage);
    }
    scrollToBottom();

    // AI 气泡
    const aiBubble = createMessageNode('ai');
    showTypingIndicator(aiBubble);
    scrollToBottom();

    const userContent = image
      ? [{ type: 'text', text: text || '请分析这张图片' }, { type: 'image_url', image_url: { url: image } }]
      : text;
    // 带上多轮历史 + chat_id:后端据此做上下文续写与记忆抽取(chat_id 让"攒 N 轮"去抖生效)。
    const chatId = await getOrCreateCurrentChatId();
    const requestBody = {
      model: safeModelName,
      messages: [...chatMessages, { role: 'user', content: userContent }],
      stream: true,
      chat_id: chatId,
      search_query: search_query || '',
    };
    const requestHeaders = { 'Content-Type': 'application/json' };
    if (String(apiKey || '').trim()) {
      requestHeaders.Authorization = `Bearer ${String(apiKey).trim()}`;
    }

    const msgId = createMessageId();
    let fullReply = '';
    let done = false;
    let _searchSources = [];  // 联网搜索结果元数据
    const streamer = createMarkdownStreamer(aiBubble);

    await new Promise((resolve) => {
      const finalize = () => {
        if (done) return;
        done = true;
        if (!fullReply) aiBubble.textContent = '响应为空。';
        else {
          streamer.finalize(fullReply);
          // 联网搜索:渲染引用 [1][2] 为可点击链接 + 来源面板
          if (_searchSources.length) {
            renderSearchCitations(aiBubble, _searchSources);
          }
          chatMessages.push({ role: 'user', content: image ? (text || '[图片]') : text });
          chatMessages.push({ role: 'assistant', content: fullReply });
        }
        chrome.runtime.onMessage.removeListener(listener);
        resolve();
      };
      const listener = (msg) => {
        if (msg.msgId !== msgId) return;
        if (msg.type === 'LLM_CHUNK') {
          fullReply += msg.chunk;
          streamer.update(fullReply);
          scrollToBottom();
        } else if (msg.type === 'LLM_SEARCH_RESULTS') {
          _searchSources = msg.search_results || [];
        } else if (msg.type === 'LLM_DONE') {
          finalize();
        } else if (msg.type === 'LLM_ERROR') {
          streamer.cancel();
          aiBubble.textContent = '';
          const errorSpan = document.createElement('span');
          errorSpan.className = 'error-text';
          errorSpan.textContent = `⚠️ 错误: ${msg.error}`;
          aiBubble.appendChild(errorSpan);
          done = true;
          chrome.runtime.onMessage.removeListener(listener);
          resolve();
        }
      };
      chrome.runtime.onMessage.addListener(listener);
      chrome.runtime.sendMessage({
        type: 'CALL_LLM_STREAM',
        msgId,
        url: `${safeApiUrl}/chat/completions`,
        options: { method: 'POST', headers: requestHeaders, body: JSON.stringify(requestBody) }
      });
      setTimeout(finalize, 120000);
    });
  }

  // ── 会话历史(抽屉:列表 / 续谈 / 重命名 / 删除)──

  async function backendBase() {
    // 取当前后端 base url(localhost:8000/v1 之类);失败抛错由调用方降级
    const { safeApiUrl } = await resolveApiRequestConfig();
    return safeApiUrl;
  }

  function openDrawer() {
    document.getElementById('sessionDrawer')?.classList.add('open');
    document.getElementById('drawerMask')?.classList.remove('hidden');
    loadSessionList().catch((e) => console.warn('加载会话列表失败', e));
  }

  function closeDrawer() {
    document.getElementById('sessionDrawer')?.classList.remove('open');
    document.getElementById('drawerMask')?.classList.add('hidden');
  }

  async function loadSessionList() {
    const listEl = document.getElementById('sessionList');
    if (!listEl) return;
    listEl.replaceChildren();
    let base;
    try { base = await backendBase(); } catch { listEl.innerHTML = '<div class="session-empty">需先在设置里配置后端地址</div>'; return; }
    let data;
    try {
      data = await callBackendApi(buildBackendEndpointUrl(base, '/v1/sessions/list'), 'GET');
    } catch (e) {
      listEl.innerHTML = '<div class="session-empty">会话历史不可用</div>';
      return;
    }
    const sessions = data?.sessions || [];
    if (!sessions.length) {
      listEl.innerHTML = '<div class="session-empty">还没有历史会话</div>';
      return;
    }
    for (const s of sessions) {
      const item = document.createElement('div');
      item.className = 'session-item';
      if (s.chat_id === currentChatId) item.classList.add('active');

      const titleEl = document.createElement('div');
      titleEl.className = 'session-item-title';
      titleEl.textContent = s.title || '(未命名会话)';
      titleEl.addEventListener('click', () => resumeSession(s.chat_id));
      item.appendChild(titleEl);

      const actions = document.createElement('div');
      actions.className = 'session-item-actions';
      const renameBtn = document.createElement('button');
      renameBtn.textContent = '✏️';
      renameBtn.title = '重命名';
      renameBtn.addEventListener('click', (ev) => { ev.stopPropagation(); renameSession(s.chat_id, s.title); });
      const delBtn = document.createElement('button');
      delBtn.textContent = '🗑️';
      delBtn.title = '删除';
      delBtn.addEventListener('click', (ev) => { ev.stopPropagation(); deleteSession(s.chat_id, s.title); });
      actions.appendChild(renameBtn);
      actions.appendChild(delBtn);
      item.appendChild(actions);

      listEl.appendChild(item);
    }
  }

  async function resumeSession(chatId) {
    let base;
    try { base = await backendBase(); } catch { return; }
    let data;
    try {
      data = await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/messages`), 'GET');
    } catch (e) {
      alert('载入会话失败: ' + (e?.message || ''));
      return;
    }
    const messages = data?.messages || [];
    const summary = data?.summary || '';
    const summaryMsgCount = data?.summary_msg_count || 0;
    // 切到该会话:重建内存历史 + DOM 气泡
    currentChatId = chatId;
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: chatId });
    // chatMessages 用于发给 LLM:摘要 + tail 原文
    chatMessages = [];
    if (summary) {
      chatMessages.push({ role: 'system', content: '## 本会话此前摘要\n' + summary });
    }
    const tail = summaryMsgCount > 0 ? messages.slice(summaryMsgCount) : messages;
    for (const m of tail) {
      chatMessages.push({ role: m.role, content: m.content });
    }
    // DOM 全量渲染(用户看得到完整历史)
    const historyEl = document.getElementById('chatHistory');
    historyEl.replaceChildren();
    for (const m of messages) {
      const bubble = createMessageNode(m.role === 'user' ? 'user' : 'ai');
      if (m.role === 'user') {
        const node = document.createElement('div');
        node.textContent = m.content;
        bubble.appendChild(node);
      } else {
        renderMarkdownInto(bubble, m.content, { streaming: false });
      }
    }
    scrollToBottom();
    closeDrawer();
  }

  async function renameSession(chatId, oldTitle) {
    const title = prompt('重命名会话:', oldTitle || '');
    if (title == null) return;
    try {
      const base = await backendBase();
      await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}`), 'PATCH', { title: title.trim() });
      await loadSessionList();
    } catch (e) {
      alert('重命名失败: ' + (e?.message || ''));
    }
  }

  async function deleteSession(chatId, title) {
    if (!confirm(`删除会话「${title || '未命名'}」?`)) return;
    try {
      const base = await backendBase();
      await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}`), 'DELETE');
      // 若删的是当前会话,顺带清空并开新会话
      if (chatId === currentChatId) {
        document.getElementById('chatHistory').replaceChildren();
        chatMessages = [];
        await resetCurrentChatId();
      }
      await loadSessionList();
    } catch (e) {
      alert('删除失败: ' + (e?.message || ''));
    }
  }

  // ── 长期记忆管理(设置内:列出 / 删 / 改 / 加)──

  const MEMORY_TYPE_LABELS = { core: '画像', episodic: '事件' };

  // 并发保护:同时多次点刷新/多入口触发时,只保留最后一次结果,防止重复渲染
  let _memoryLoadSeq = 0;

  async function loadMemoryPanel() {
    const listEl = document.getElementById('memoryList');
    if (!listEl) return;
    const seq = ++_memoryLoadSeq;
    // 立即显示"加载中"提示(不等 await)——防用户以为无反应
    // 只有本次是最新的 seq 时才占位;并发的旧 call 不覆盖
    listEl.replaceChildren();
    listEl.innerHTML = '<div class="memory-empty">加载中...</div>';
    let base;
    try { base = await backendBase(); }
    catch {
      if (seq !== _memoryLoadSeq) return;   // 有更新的请求发出,丢弃本次
      listEl.replaceChildren();
      listEl.innerHTML = '<div class="memory-empty">需先在设置里配置后端地址</div>';
      return;
    }
    let data;
    try {
      data = await callBackendApi(buildBackendEndpointUrl(base, '/v1/memory/list'), 'GET');
    } catch (e) {
      if (seq !== _memoryLoadSeq) return;
      listEl.replaceChildren();
      listEl.innerHTML = '<div class="memory-empty">记忆库不可用</div>';
      return;
    }
    // 关键:所有 await 完后再检查 seq,只有"最新"那次真正渲染
    if (seq !== _memoryLoadSeq) return;
    listEl.replaceChildren();   // 清空必须在渲染前(而非请求前)
    const memories = data?.memories || [];
    if (!memories.length) {
      listEl.innerHTML = '<div class="memory-empty">还没有长期记忆</div>';
      return;
    }
    // 按类型分组:core → episodic
    const order = ['core', 'episodic'];
    const grouped = {};
    for (const m of memories) (grouped[m.memory_type] = grouped[m.memory_type] || []).push(m);
    for (const type of order) {
      const items = grouped[type];
      if (!items || !items.length) continue;
      const label = document.createElement('div');
      label.className = 'memory-group-label';
      label.textContent = MEMORY_TYPE_LABELS[type] || type;
      listEl.appendChild(label);
      for (const m of items) listEl.appendChild(buildMemoryItem(m));
    }
  }

  function buildMemoryItem(m) {
    const item = document.createElement('div');
    item.className = 'memory-item';
    // 已被 rethink 替代的条目灰化(P3)
    if (m.superseded_by) item.classList.add('superseded');

    const content = document.createElement('div');
    content.className = 'memory-item-content';
    // subject badge(P3):有 subject 时前缀显示,视觉分组
    if (m.subject) {
      const badge = document.createElement('span');
      badge.className = 'memory-item-subject';
      badge.textContent = m.subject;
      badge.title = `主题:${m.subject}`;
      content.appendChild(badge);
    }
    const textNode = document.createElement('span');
    textNode.textContent = m.content;
    content.appendChild(textNode);
    // expires_at badge(P3):有明确时限时后缀显示
    if (m.expires_at) {
      const exp = document.createElement('span');
      exp.className = 'memory-item-expires';
      // 转成本地日期展示
      try {
        const d = new Date(m.expires_at);
        exp.textContent = '⏰ ' + d.toLocaleDateString();
      } catch {
        exp.textContent = '⏰ ' + m.expires_at.slice(0, 10);
      }
      exp.title = `到期时间:${m.expires_at}`;
      content.appendChild(exp);
    }
    item.appendChild(content);

    const actions = document.createElement('div');
    actions.className = 'memory-item-actions';
    const editBtn = document.createElement('button');
    editBtn.textContent = '✏️';
    editBtn.title = '编辑';
    editBtn.addEventListener('click', () => editMemory(m.memory_id, m.content));
    const delBtn = document.createElement('button');
    delBtn.textContent = '🗑️';
    delBtn.title = '删除';
    delBtn.addEventListener('click', () => deleteMemory(m.memory_id, m.content));
    actions.appendChild(editBtn);
    actions.appendChild(delBtn);
    item.appendChild(actions);
    return item;
  }

  async function editMemory(memoryId, oldContent) {
    const content = prompt('编辑记忆:', oldContent || '');
    if (content == null || !content.trim()) return;
    try {
      const base = await backendBase();
      await callBackendApi(buildBackendEndpointUrl(base, `/v1/memory/${encodeURIComponent(memoryId)}`), 'PATCH', { content: content.trim() });
      await loadMemoryPanel();
    } catch (e) {
      alert('编辑失败: ' + (e?.message || ''));
    }
  }

  async function deleteMemory(memoryId, content) {
    if (!confirm(`删除这条记忆?\n\n${content}`)) return;
    try {
      const base = await backendBase();
      await callBackendApi(buildBackendEndpointUrl(base, `/v1/memory/${encodeURIComponent(memoryId)}`), 'DELETE');
      await loadMemoryPanel();
    } catch (e) {
      alert('删除失败: ' + (e?.message || ''));
    }
  }

  async function addMemory() {
    const input = document.getElementById('memoryAddInput');
    const subjectInput = document.getElementById('memoryAddSubject');
    const content = String(input?.value || '').trim();
    if (!content) return;
    const subject = String(subjectInput?.value || '').trim();
    const addBtn = document.getElementById('memoryAddBtn');
    try {
      if (addBtn) { addBtn.disabled = true; addBtn.textContent = '添加中...'; }
      const base = await backendBase();
      await callBackendApi(buildBackendEndpointUrl(base, '/v1/memory'), 'POST',
        { content, memory_type: 'core', subject });
      input.value = '';
      if (subjectInput) subjectInput.value = '';
      if (addBtn) { addBtn.textContent = '✓ 已添加'; }
      setTimeout(() => { if (addBtn) { addBtn.disabled = false; addBtn.textContent = '添加'; } }, 1500);
      await loadMemoryPanel();
    } catch (e) {
      if (addBtn) { addBtn.disabled = false; addBtn.textContent = '添加'; }
      alert('添加失败: ' + (e?.message || ''));
    }
  }

  // ── 一键整理 SSE(批次 E · P3)──
  // POST /v1/memory/rethink 返回 SSE 流,实时显示进度。
  // 事件序列:start / scanning / llm_call / applied(kind=conflicts/expired/merges) / done / error
  // 进行中拒绝重入:后端返 error {code:"in_progress"} + [DONE]
  // _rethinkState: 'idle' | 'running' | 'done'
  // idle=没在整理(modal 该隐藏),running=进行中,done=完成但用户还没关闭
  let _rethinkState = 'idle';

  async function rethinkMemories() {
    const modal = document.getElementById('rethinkModal');
    const statusEl = document.getElementById('rethinkModalStatus');
    const logEl = document.getElementById('rethinkModalLog');
    const closeBtn = document.getElementById('rethinkModalCloseBtn');
    if (!modal || !statusEl || !logEl || !closeBtn) return;

    _rethinkState = 'running';
    modal.style.display = 'flex';
    statusEl.textContent = '连接后端...';
    logEl.replaceChildren();
    closeBtn.style.display = 'none';

    const appendLog = (cls, text) => {
      const line = document.createElement('div');
      line.className = 'rethink-log-line ' + cls;
      line.textContent = text;
      logEl.appendChild(line);
      logEl.scrollTop = logEl.scrollHeight;
    };

    let base;
    try { base = await backendBase(); }
    catch {
      statusEl.textContent = '未配置后端地址';
      closeBtn.style.display = 'inline-flex';
      return;
    }

    let resp;
    try {
      resp = await fetch(buildBackendEndpointUrl(base, '/v1/memory/rethink'), {
        method: 'POST',
        headers: { 'Accept': 'text/event-stream' },
      });
    } catch (e) {
      statusEl.textContent = '连接失败: ' + (e?.message || '');
      closeBtn.style.display = 'inline-flex';
      return;
    }
    if (!resp.ok || !resp.body) {
      statusEl.textContent = `请求失败:HTTP ${resp.status}`;
      closeBtn.style.display = 'inline-flex';
      return;
    }

    // 手动解析 SSE(fetch + ReadableStream,兼容 POST + SSE)
    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let currentEvent = 'message';
    let counts = { conflicts: 0, expired: 0, merges: 0 };

    const handleEvent = (name, dataStr) => {
      let data = {};
      try { data = JSON.parse(dataStr); } catch { /* 保留空对象 */ }
      switch (name) {
        case 'start':
          statusEl.textContent = `扫描到 ${data.total_core || 0} 条 core 记忆`;
          appendLog('rethink-log-info', `开始整理:${data.total_core} 条 core`);
          break;
        case 'scanning':
          statusEl.textContent = data.progress || '扫描中...';
          break;
        case 'llm_call':
          statusEl.textContent = data.progress || 'LLM 分析中...';
          appendLog('rethink-log-info', '⏳ ' + (data.progress || 'LLM 分析中...'));
          break;
        case 'applied': {
          const kind = data.kind || '';
          if (kind === 'conflicts') {
            counts.conflicts++;
            const inv = Array.isArray(data.invalidate) ? data.invalidate.length : 0;
            appendLog('rethink-log-conflict',
              `⚔ 冲突:保留 "${(data.keep_content || '').slice(0, 40)}",失效 ${inv} 条`);
          } else if (kind === 'expired') {
            counts.expired++;
            appendLog('rethink-log-expired',
              `⏰ 过期:"${(data.content || '').slice(0, 40)}"`);
          } else if (kind === 'merges') {
            counts.merges++;
            const mem = Array.isArray(data.members) ? data.members.length : 0;
            appendLog('rethink-log-merge',
              `⊕ 合并 ${mem} 条 → "${(data.merged_content || '').slice(0, 40)}"`);
          }
          statusEl.textContent = `已处理:冲突 ${counts.conflicts} · 过期 ${counts.expired} · 合并 ${counts.merges}`;
          break;
        }
        case 'done': {
          const parts = [];
          if (data.conflicts) parts.push(`${data.conflicts} 组冲突`);
          if (data.expired) parts.push(`${data.expired} 条过期`);
          if (data.merges) parts.push(`${data.merges} 组合并`);
          if (data.skipped === 'not_enough_core') {
            statusEl.textContent = `core 少于 3 条,无需整理`;
          } else if (!parts.length) {
            statusEl.textContent = `整理完成:未发现需处理的条目`;
          } else {
            statusEl.textContent = `整理完成:${parts.join(' · ')} · 耗时 ${Math.round((data.elapsed_ms || 0) / 100) / 10}s`;
          }
          appendLog('rethink-log-info', `✓ 完成`);
          break;
        }
        case 'error': {
          if (data.code === 'in_progress') {
            statusEl.textContent = data.message || '整理已在进行中,请稍候';
            appendLog('rethink-log-info',
              `⚠ 已有整理在跑,已耗时 ${Math.round((data.elapsed_ms || 0) / 1000)} 秒`);
          } else {
            statusEl.textContent = `失败:${data.message || data.code || '未知错误'}`;
            appendLog('rethink-log-error', `✗ ${data.code}: ${data.message || ''}`);
          }
          break;
        }
      }
    };

    // 读 SSE 流(整块包 try/finally:任何异常都保证关闭按钮可见,不残留卡死态)
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // 按 \n\n 切分事件块
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const block of parts) {
          if (!block.trim()) continue;
          let evName = 'message';
          let dataStr = '';
          for (const line of block.split('\n')) {
            if (line.startsWith('event: ')) evName = line.slice(7).trim();
            else if (line.startsWith('data: ')) dataStr = line.slice(6);
          }
          if (dataStr === '[DONE]') { /* 流结束,不用处理 */ continue; }
          handleEvent(evName, dataStr);
        }
      }
    } catch (e) {
      statusEl.textContent = `连接中断: ${e?.message || e}`;
      appendLog('rethink-log-error', `✗ 连接异常: ${e?.message || e}`);
    } finally {
      _rethinkState = 'done';
      closeBtn.style.display = 'inline-flex';
    }
    // 完成后刷新记忆列表
    try { await loadMemoryPanel(); } catch { /* 静默 */ }
  }

  // ── 联网搜索:引用渲染 + 来源面板 ──

  function renderSearchCitations(bubbleEl, sources) {
    if (!sources || !sources.length) return;
    // 把回答里的 [1] [2] 渲染为可点击引用链接
    const contentEl = bubbleEl.querySelector('.markdown-body') || bubbleEl;
    if (contentEl.innerHTML) {
      contentEl.innerHTML = contentEl.innerHTML.replace(/\[(\d+)\]/g, (match, num) => {
        const idx = parseInt(num) - 1;
        const src = sources[idx];
        if (!src) return match;
        const safeTitle = (src.title || '').replace(/"/g, '&quot;');
        return `<a class="search-citation" href="${src.url}" target="_blank" rel="noopener" title="${safeTitle}">[${num}]</a>`;
      });
    }
    // 来源面板
    const panel = document.createElement('div');
    panel.className = 'search-sources-panel';
    const toggle = document.createElement('div');
    toggle.className = 'search-sources-toggle';
    toggle.textContent = `📎 来源 (${sources.length})`;
    const list = document.createElement('div');
    list.className = 'search-sources-list';
    list.style.display = 'none';
    for (let i = 0; i < sources.length; i++) {
      const s = sources[i];
      const a = document.createElement('a');
      a.className = 'search-source-item';
      a.href = s.url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.innerHTML = `<span class="search-source-num">[${i + 1}]</span> ${(s.title || '').replace(/</g, '&lt;')}`;
      list.appendChild(a);
    }
    toggle.addEventListener('click', () => {
      list.style.display = list.style.display === 'none' ? 'block' : 'none';
    });
    panel.appendChild(toggle);
    panel.appendChild(list);
    bubbleEl.appendChild(panel);
  }

  // 5. 绑定各种交互事件
  getOrCreateCurrentChatId().catch(console.error);

  document.getElementById('sendBtn').addEventListener('click', handleSend);
  document.getElementById('chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  });

  // 联网搜索切换按钮(toggle,和自动化按钮同模式)
  const _searchBtn = document.getElementById('webSearchBtn');
  const _searchToggle = document.getElementById('webSearchToggle');
  if (_searchBtn && _searchToggle) {
    _searchBtn.addEventListener('click', () => {
      _searchToggle.checked = !_searchToggle.checked;
      _searchBtn.classList.toggle('is-active', _searchToggle.checked);
    });
  }
  function isSearchMode() {
    const t = document.getElementById('webSearchToggle');
    return t && t.checked;
  }

  // 会话抽屉:开/关/遮罩/新建
  document.getElementById('openSessionsBtn')?.addEventListener('click', openDrawer);
  document.getElementById('closeSessionsBtn')?.addEventListener('click', closeDrawer);
  document.getElementById('drawerMask')?.addEventListener('click', closeDrawer);
  document.getElementById('newSessionBtn')?.addEventListener('click', async () => {
    document.getElementById('chatHistory').replaceChildren();
    chatMessages = [];
    await resetCurrentChatId();
    closeDrawer();
  });

  // 记忆面板:刷新 / 添加(添加支持 Enter)/ 一键整理(P3)
  document.getElementById('refreshMemoryBtn')?.addEventListener('click', () => loadMemoryPanel());
  document.getElementById('memoryAddBtn')?.addEventListener('click', addMemory);
  document.getElementById('memoryAddInput')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addMemory(); }
  });
  document.getElementById('rethinkMemoryBtn')?.addEventListener('click', () => rethinkMemories());
  document.getElementById('rethinkModalCloseBtn')?.addEventListener('click', () => {
    const m = document.getElementById('rethinkModal');
    if (m) m.style.display = 'none';
    _rethinkState = 'idle';
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

  // 清空只开启新执行记录。
  document.getElementById('clearChatBtn')?.addEventListener('click', async () => {
    document.getElementById('chatHistory').replaceChildren();
    document.getElementById('chatInput').value = '';
    clearAttachedImage();
    chatMessages = [];   // 清空多轮历史,新会话从零开始
    await resetCurrentChatId();
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

  // ═══════════════════════════════════════════════════════════════════
  // Agent 页面自动化模块
  // ═══════════════════════════════════════════════════════════════════

  const SLASH_COMMANDS = [
    { name: '/browser-operation', description: '浏览器页面自动化操作（点击、输入、滚动等）', handler: 'agent' },
  ];
  const AGENT_COMMAND = '/browser-operation ';
  const AGENT_SETTLE_TIMEOUT_MS = 3000;
  const AGENT_ACTION_TIMEOUT_MS = 10000;
  const AGENT_STEP_TIMEOUT_MS = 120000;   // 单个 step（执行+等稳+观察）总超时；对齐 browser-use step_timeout（它 180s），防非 LLM 环节卡死拖到整轮墙钟
  const AGENT_TOTAL_TIMEOUT_MS = 3600000;   // 1小时：整轮墙钟仅作"防真死"极粗兜底（browser-use 无整轮墙钟，只靠步数+单步超时）；到点走 force_done 收尾。配合 max_steps=200 长任务放宽

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

  // ═══════════════════════════════════════════════════════════════════════════
  // Set-of-Marks 截图标注（对齐 browser-use python_highlights.py）
  // 在裸截图上叠加"元素编号框"，让 LLM 看到的视觉元素与它能填的 index 一一对应。
  // 没有这层标注，多模态反而有害：模型看得见按钮却猜不准编号，会反复点错。
  // ═══════════════════════════════════════════════════════════════════════════

  // 按 tag 分色（同 browser-use ELEMENT_COLORS）
  const SOM_ELEMENT_COLORS = {
    button: '#FF6B6B', input: '#4ECDC4', select: '#45B7D1',
    a: '#96CEB4', textarea: '#FF8C42', default: '#DDA0DD',
  };

  function somElementColor(tag, type) {
    if (tag === 'input' && (type === 'button' || type === 'submit')) return SOM_ELEMENT_COLORS.button;
    return SOM_ELEMENT_COLORS[tag] || SOM_ELEMENT_COLORS.default;
  }

  // 画一个元素的虚线框 + 编号方块（对齐 draw_enhanced_bounding_box_with_text）
  function drawSomBox(ctx, box, color, label, imgW, imgH, dpr) {
    // CSS 视口坐标 → 设备像素（截图是设备像素图；漏掉这步框会整体偏移）
    const x1 = Math.max(0, Math.min(Math.round(box.x * dpr), imgW));
    const y1 = Math.max(0, Math.min(Math.round(box.y * dpr), imgH));
    const x2 = Math.max(x1, Math.min(Math.round((box.x + box.width) * dpr), imgW));
    const y2 = Math.max(y1, Math.min(Math.round((box.y + box.height) * dpr), imgH));
    if (x2 - x1 < 2 || y2 - y1 < 2) return;

    // 虚线框：dash=4 gap=8 width=2
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 8]);
    ctx.strokeRect(x1 + 1, y1 + 1, x2 - x1 - 2, y2 - y1 - 2);
    ctx.restore();

    if (!label) return;

    // 编号方块：字号随图宽缩放(10~20)，元素色底 + 白边 + 白字
    const fontSize = Math.max(10, Math.min(20, Math.round(imgW * 0.01)));
    const padding = Math.max(4, Math.min(10, Math.round(imgW * 0.005)));
    ctx.save();
    ctx.font = `bold ${fontSize}px Arial, sans-serif`;
    ctx.textBaseline = 'top';
    const textW = Math.ceil(ctx.measureText(label).width);
    const textH = fontSize;
    const cw = textW + padding * 2;
    const ch = textH + padding * 2;
    const elW = x2 - x1, elH = y2 - y1;

    let bx = x1 + Math.floor((elW - cw) / 2);
    // 小元素：编号放框上方，避免遮住图标内容；大元素：放框内顶部
    let by = (elW < 60 || elH < 30) ? Math.max(0, y1 - ch - 5) : y1 + 2;
    // 夹取到图像边界内
    if (bx < 0) bx = 0;
    if (by < 0) by = 0;
    if (bx + cw > imgW) bx = imgW - cw;
    if (by + ch > imgH) by = imgH - ch;

    ctx.fillStyle = color;
    ctx.strokeStyle = 'white';
    ctx.lineWidth = 2;
    ctx.setLineDash([]);
    ctx.fillRect(bx, by, cw, ch);
    ctx.strokeRect(bx, by, cw, ch);
    ctx.fillStyle = 'white';
    ctx.fillText(label, bx + padding, by + padding);
    ctx.restore();
  }

  // 在裸截图上叠加所有可见元素的 SoM 标注，返回新 dataURL（失败回退原图）
  async function annotateScreenshotSoM(dataUrl, elements, viewport) {
    try {
      if (!dataUrl || !elements?.length) return dataUrl;
      const img = await new Promise((resolve, reject) => {
        const im = new Image();
        im.onload = () => resolve(im);
        im.onerror = reject;
        im.src = dataUrl;
      });
      const imgW = img.naturalWidth, imgH = img.naturalHeight;
      // DPR = 截图设备像素宽 / CSS 视口宽（captureVisibleTab 截的是设备像素图）
      const vpW = viewport?.width || imgW;
      const dpr = vpW > 0 ? imgW / vpW : 1;

      const canvas = document.createElement('canvas');
      canvas.width = imgW; canvas.height = imgH;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);

      for (const el of elements) {
        const box = el.bounding_box;
        if (!box) continue;
        // 只标视口内可见元素（框在截图范围内）
        if (box.x + box.width <= 0 || box.x >= vpW) continue;
        if (box.y + box.height <= 0 || box.y >= (viewport?.height || imgH)) continue;
        // filter_highlight_ids：有足够文字的元素不画编号(文字本身可定位)，只画框
        const meaningful = (el.text || el.aria_label || el.placeholder || '').trim();
        const label = meaningful.length >= 3 ? '' : String(el.id);
        const color = somElementColor(el.tag, el.type);
        drawSomBox(ctx, box, color, label, imgW, imgH, dpr);
      }
      return canvas.toDataURL('image/jpeg', 0.6);
    } catch (e) {
      console.warn('[SoM] 标注失败，回退裸图:', e);
      return dataUrl;
    }
  }

  async function observePageState() {
    const tab = await getActiveBrowserTab();
    if (!tab?.id) throw new Error('无法获取当前标签页');

    // CDP 三源观察：background 融合返回 pageState，本地补截图 + SoM 标注（SW 无 DOM/Canvas）。
    const resp = await chrome.runtime.sendMessage({ type: 'AGENT_OBSERVE', tabId: tab.id });
    if (!resp || !resp.ok) throw new Error(resp?.error || 'CDP 观察失败');
    const pageState = resp.pageState;
    try {
      const raw = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 60 });
      pageState.screenshot = await annotateScreenshotSoM(
        raw, pageState.interactive_elements, pageState.viewport
      );
    } catch (e) {
      pageState.screenshot = '';
    }
    return pageState;
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

    // CDP 执行：元素动作（click/type/scroll/...）下沉 background（navigate/wait 已在上方本地处理）。
    const tab = await getActiveBrowserTab();
    if (!tab?.id) throw new Error('无法获取当前标签页');
    const resp = await chrome.runtime.sendMessage({ type: 'AGENT_EXECUTE', tabId: tab.id, action });
    if (!resp || !resp.ok) return { success: false, action_type: action.type, error: resp?.error || 'CDP 执行失败', timestamp: Date.now() };
    return { ...resp.result, timestamp: Date.now() };
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
      if (action.index != null) actionDesc += ` → [${action.index}]`;
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
    const label = document.createElement('span');
    label.textContent = success !== false ? '✅ 任务完成: ' : '⚠️ 任务未能完成: ';
    completeEl.appendChild(label);
    const body = document.createElement('div');
    const summaryText = success !== false
      ? (summary || '已完成所有操作')
      : (summary || '');
    renderMarkdownInto(body, summaryText, { streaming: false });
    completeEl.appendChild(body);
    bubble.appendChild(completeEl);
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
      if (action.index != null) desc += ` → [${action.index}]`;
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

  // 跨轮 diff：检测上一步操作是否"就地展开了一组选项/复选框"（checkbox/radio/inline下拉）。
  // 采集端的 findVisiblePopups 只认容器型/ARIA弹层，就地展开的组（已被采集但未被判为弹层）靠这里兜底。
  // 判定：本轮相对上轮新增元素成簇（同 parent_sig ≥2 个）且新增不是页面大改（占比<30% 且 ≤15 个）。
  //
  // ⚠️ 边界（重要）：本函数只对"已进入 interactive_elements 的元素"做前后 diff，
  //    它工作在【采集之后】，不是采集层。若某元素因 class 不匹配任何选择器而【采集阶段就漏了】，
  //    它既不在 prevEls 也不在 newEls，diff 永远发现不了它——这不是 bug，是设计边界。
  //    采集漏掉的元素只能靠"结构/cursor 通用抽取"（不依赖 class 的第2道防线）在采集层捞回，
  //    不能指望这里兜。参见 POPUP_EXTRA_SELECTORS / POPUP_TRUSTED_SELECTORS / POPUP_FUZZY_SELECTORS 的 class 匹配。
  function detectInlineGroup(prevEls, newEls) {
    if (!Array.isArray(prevEls) || !Array.isArray(newEls) || !newEls.length) return null;
    const sig = (e) => `${e.tag}|${e.role}|${(e.text || '').slice(0, 20)}|${e.name}`;
    const prevSet = new Set(prevEls.map(sig));
    const added = newEls.filter(e => !prevSet.has(sig(e)));
    if (added.length < 2) return null;
    // 大面积变化视为翻页/重渲染，不算展开组
    if (added.length > 15) return null;
    if (added.length / Math.max(newEls.length, 1) >= 0.3) return null;
    // 按 parent_sig 聚簇，取最大的一簇
    const byParent = {};
    for (const e of added) {
      const k = e.parent_sig || '';
      if (!k) continue;
      (byParent[k] ||= []).push(e);
    }
    let best = null;
    for (const k of Object.keys(byParent)) {
      if (!best || byParent[k].length > best.length) best = byParent[k];
    }
    if (!best || best.length < 2) return null;
    return {
      type: 'inline_group',
      header_text: '',
      selector: '',
      synthesized: true,
      member_count: best.length
    };
  }

  async function runAgentTask(task, taskImage = '') {
    const sessionId = `agent_${createMessageId()}`;
    agentState.active = true;
    agentState.sessionId = sessionId;
    agentState.task = task;
    agentState.status = 'running';
    agentState.currentStep = 0;

    // Port keepalive：任务全程持有，活跃 Port 在则 background service worker 不被 terminate，
    // 覆盖单步 settle + LLM 90s 的长间隙（MV3 SW 空闲 30s 会被杀）。finally 里断开。
    let keepalivePort = null;
    try {
      keepalivePort = chrome.runtime.connect({ name: 'agent-keepalive' });
      keepalivePort.onDisconnect.addListener(() => { /* SW 重启会断开，无需动作 */ });
    } catch { keepalivePort = null; }

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
    if (taskImage) {
      const previewImage = document.createElement('img');
      previewImage.className = 'user-upload-preview';
      previewImage.src = taskImage;
      previewImage.alt = '任务参考图';
      userBubble.appendChild(previewImage);
    }

    const aiBubble = createMessageNode('ai');
    showTypingIndicator(aiBubble);
    scrollToBottom();

    let cancelBtn = null;

    try {
      let pageState = await observePageState();
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
        require_confirmation: [],
        task_image: taskImage || ''
      }, apiKey);

      const agentStartTime = Date.now();
      while (response.status === 'action_required' || response.status === 'confirm_required') {
        if (!agentState.active) break;
        if (Date.now() - agentStartTime > AGENT_TOTAL_TIMEOUT_MS) {
          // 整轮超时：不硬杀，让后端 force_done 逼 LLM 出 task_complete 给交代（对齐 browser-use 收尾）
          try {
            const freshState = await observePageState().catch(() => pageState);
            const finalResp = await callAgentApi(safeApiUrl, '/v1/agent/step', {
              session_id: sessionId,
              action_result: { success: true, action_type: 'timeout_finish', details: '整轮超时，强制收尾' },
              page_state: freshState,
              force_done: true
            }, apiKey);
            if (finalResp.status === 'completed') {
              renderAgentComplete(aiBubble, finalResp.summary, false);
            } else {
              renderAgentError(aiBubble, '执行超时，未能生成收尾总结');
            }
          } catch (e) {
            renderAgentError(aiBubble, '执行超时');
          }
          break;
        }

        agentState.currentStep = response.step;
        const action = response.action;
        if (!action) break;  // 无动作但非终态，异常，退出

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

        const actionResult = await Promise.race([
          executePageAction(action),
          new Promise((resolve) => setTimeout(
            () => resolve({ success: false, action_type: action.type, details: 'step 超时（执行未在时限内返回）', _stepTimeout: true }),
            AGENT_STEP_TIMEOUT_MS))
        ]);
        renderAgentStepInBubble(aiBubble, response.step, null, null, actionResult);

        // 编号失效（stale）：页面已重渲染，重新观察后让 LLM 用新编号，不计失败
        if (actionResult.stale) {
          const freshState = await observePageState();
          pageState = freshState;
          response = await callAgentApi(safeApiUrl, '/v1/agent/step', {
            session_id: sessionId,
            action_result: { success: false, stale: true, action_type: action.type, details: actionResult.error || '编号失效' },
            page_state: freshState
          }, apiKey);
          continue;
        }

        // step 超时：执行卡住未在时限内返回，重新观察后把失败反馈给 LLM 换策略，不拖到整轮墙钟
        if (actionResult._stepTimeout) {
          const freshState = await observePageState().catch(() => pageState);
          pageState = freshState;
          response = await callAgentApi(safeApiUrl, '/v1/agent/step', {
            session_id: sessionId,
            action_result: { success: false, action_type: action.type, details: '本步超时，请换一种方式' },
            page_state: freshState
          }, apiKey);
          continue;
        }

        // 智能等待页面稳定（导航期间 executeScript 可能永远不返回,外层超时兜底）
        const activeTab = await getActiveBrowserTab().catch(() => null);
        if (activeTab?.id) {
          await Promise.race([
            waitForPageSettle(activeTab.id),
            new Promise(r => setTimeout(r, 5000))   // 5s 硬上限,防导航中 executeScript 挂死
          ]);
        }

        // 导航后移鼠标到中性位收残留浮层，再观察
        const preObserveTab = await getActiveBrowserTab().catch(() => null);
        if (preObserveTab?.id) {
          try {
            const curUrl = await chrome.scripting.executeScript({
              target: { tabId: preObserveTab.id, allFrames: false },
              func: () => location.href
            }).then(r => r?.[0]?.result).catch(() => null);
            if (curUrl && curUrl !== preUrl) {
              await chrome.runtime.sendMessage({ type: 'DEBUGGER_HOVER', tabId: preObserveTab.id, x: 2, y: 2 });
              await new Promise(r => setTimeout(r, 250));
            }
          } catch (e) { /* ignore */ }
        }

        const newPageState = await Promise.race([
          observePageState(),
          new Promise((_, reject) => setTimeout(() => reject(new Error('observe 超时')), 30000))
        ]).catch(() => pageState);  // 观察超时时沿用上一步状态,不卡死循环

        // inline 组兜底：成簇新增 → 合成 active_popup，让 popup_appeared 成立、格式化置顶
        if (!newPageState.active_popup) {
          const inlineGroup = detectInlineGroup(
            pageState?.interactive_elements || [],
            newPageState.interactive_elements || []
          );
          if (inlineGroup) {
            newPageState.active_popup = inlineGroup;
            const sig = (e) => `${e.tag}|${e.role}|${(e.text || '').slice(0, 20)}|${e.name}`;
            const prevSet = new Set((pageState?.interactive_elements || []).map(sig));
            for (const e of newPageState.interactive_elements) {
              if (!prevSet.has(sig(e))) e.in_popup = true;
            }
          }
        }

        actionResult.state_changes = {
          url_changed: newPageState.url !== preUrl,
          popup_appeared: !prePopup && !!newPageState.active_popup,
          popup_disappeared: !!prePopup && !newPageState.active_popup,
          element_count_delta: newPageState.interactive_elements.length - preElementCount
        };
        pageState = newPageState;

        // 单次调用：传上一步结果 + 新观察 → 下一个动作
        response = await callAgentApi(safeApiUrl, '/v1/agent/step', {
          session_id: sessionId,
          action_result: actionResult,
          page_state: newPageState
        }, apiKey);
      }

      if (response.status === 'completed') {
        renderAgentComplete(aiBubble, response.summary, response.success !== false);
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
      // 断开 keepalive：任务结束后允许 SW 正常回收
      if (keepalivePort) { try { keepalivePort.disconnect(); } catch { /* noop */ } }
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

    // 侧边栏已收窄为 agent 为主体：发送统一由 handleSend → runAgentTask 处理，
    // 不再需要旧的捕获阶段拦截（否则会与 handleSend 重复触发）。
  })();

  // ═══════════════════════════════════════════════════════════════════
  // 操作录制模块（存入知识库）
  // ═══════════════════════════════════════════════════════════════════
});

