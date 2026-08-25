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
        await runPlainChat(text, image);
      }
    } finally {
      _sendingLock = false;
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  // 轻量直连聊天：不点自动化按钮时,消息直接发给用户配置的 OpenAI 兼容接口,流式返回。
  async function runPlainChat(text, image) {
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
      chat_id: chatId
    };
    const requestHeaders = { 'Content-Type': 'application/json' };
    if (String(apiKey || '').trim()) {
      requestHeaders.Authorization = `Bearer ${String(apiKey).trim()}`;
    }

    const msgId = createMessageId();
    let fullReply = '';
    let done = false;

    await new Promise((resolve) => {
      const finalize = () => {
        if (done) return;
        done = true;
        if (!fullReply) aiBubble.textContent = '响应为空。';
        else {
          // 存这轮到多轮历史(仅文本;图片轮用占位符,不把 base64 塞进历史)。
          chatMessages.push({ role: 'user', content: image ? (text || '[图片]') : text });
          chatMessages.push({ role: 'assistant', content: fullReply });
          if (chatMessages.length > MAX_CHAT_HISTORY_MESSAGES) {
            chatMessages = chatMessages.slice(-MAX_CHAT_HISTORY_MESSAGES);
          }
        }
        chrome.runtime.onMessage.removeListener(listener);
        resolve();
      };
      const listener = (msg) => {
        if (msg.msgId !== msgId) return;
        if (msg.type === 'LLM_CHUNK') {
          fullReply += msg.chunk;
          aiBubble.textContent = fullReply;
          scrollToBottom();
        } else if (msg.type === 'LLM_DONE') {
          finalize();
        } else if (msg.type === 'LLM_ERROR') {
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

  // 5. 绑定各种交互事件
  getOrCreateCurrentChatId().catch(console.error);

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
          '[class*="select-item"]',
          '[class*="select-list"] > *',
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
        // 可信选择器：标准 ARIA role + 组件库精确 class，命中即视为弹层，无需二次验证。
        const POPUP_TRUSTED_SELECTORS = [
          '[role="dialog"]', '[role="listbox"]', '[role="menu"]',
          '.jmtd-dropdown-panel', '.jmtd-dropdown-list',
          '.jmtd-popup', '.jmtd-modal',
          '.jmtd-date-picker-panel', '.jmtd-select-dropdown',
          '.ant-modal-content', '.ant-dropdown',
          '.ant-picker-panel', '.ant-picker-dropdown',
          '.ant-select-dropdown', '.ant-popover-inner',
          '.el-dialog', '.el-dropdown-menu',
          '.el-picker-panel', '.el-select-dropdown', '.el-popover',
          '.modal[style*="display: block"]', '.modal.show'
        ];
        // 宽泛选择器：仅靠 class 子串匹配，容易误伤常驻侧栏/助手浮层（如"码小伴"）。
        // 命中后必须再过 looksLikeFloatingLayer() 才算弹层，避免 active_popup 被常驻容器长期占用。
        const POPUP_FUZZY_SELECTORS = [
          '[class*="popup"]:not([style*="display: none"])',
          '[class*="dropdown-list"]', '[class*="picker-panel"]',
          '[class*="search__dropdown"]', '[class*="autocomplete"]',
          '[class*="popover__content"]', '[class*="popper"]',
          '[class*="dropdownWrap"]', '[class*="dropdown__"]',
          '[class*="select-branch"]',
          '[class*="select-list"]', '[class*="select-dropdown"]'
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

        // Shadow DOM 穿透：收集 root 下所有 open shadowRoot（含嵌套），供逐个 querySelector。
        // querySelectorAll 无法跨 shadow 边界，Web Component 内的元素只能进 shadowRoot 才能采到。
        const MAX_SHADOW_ROOTS = 400;   // 性能保护：超大页面限制递归数量
        function collectShadowRoots(root, acc) {
          if (acc.length >= MAX_SHADOW_ROOTS) return acc;
          let hosts;
          try { hosts = root.querySelectorAll('*'); } catch (e) { return acc; }
          for (const el of hosts) {
            const sr = el.shadowRoot;
            if (sr && sr.mode === 'open') {
              acc.push(sr);
              collectShadowRoots(sr, acc);
              if (acc.length >= MAX_SHADOW_ROOTS) break;
            }
          }
          return acc;
        }

        // 在 root 及其所有 open shadowRoot 内查询选择器，合并结果（穿透 shadow DOM）
        function queryAllDeep(root, selector) {
          const out = [];
          const roots = [root, ...collectShadowRoots(root, [])];
          for (const r of roots) {
            let hits;
            try { hits = r.querySelectorAll(selector); } catch (e) { continue; }
            for (const el of hits) out.push(el);
          }
          return out;
        }

        // 遮挡命中测试：多点采样（中心+4内角），全部被挡才算遮挡；含 label/input 关联救援。
        // 对齐 browser-use elementFromPoint + 包含判断 + label 关联，比单点中心更准（角落可点、半透明遮罩不误判）。
        function isOccluded(el, rect) {
          try {
            const ok = (px, py) => {
              if (px < 0 || py < 0 || px > window.innerWidth || py > window.innerHeight) return null; // 点在视口外，忽略
              let top = document.elementFromPoint(px, py);
              while (top && top.shadowRoot) {
                const inner = top.shadowRoot.elementFromPoint(px, py);
                if (!inner || inner === top) break;
                top = inner;
              }
              if (!top) return null;
              // 命中自己/后代/祖先 → 该点不遮挡
              if (top === el || el.contains(top) || top.contains(el)) return true;
              // label/input 关联救援：命中的是与目标关联的 label，或目标 label 内的控件
              if (top.tagName === 'LABEL') {
                const forId = top.getAttribute('for');
                if (forId && el.id && forId === el.id) return true;
                if (top.contains(el)) return true;
              }
              return false;
            };
            const insetX = Math.min(rect.width / 4, 8);
            const insetY = Math.min(rect.height / 4, 8);
            const pts = [
              [rect.left + rect.width / 2, rect.top + rect.height / 2],   // 中心
              [rect.left + insetX, rect.top + insetY],                     // 左上内角
              [rect.right - insetX, rect.top + insetY],                    // 右上
              [rect.left + insetX, rect.bottom - insetY],                  // 左下
              [rect.right - insetX, rect.bottom - insetY],                 // 右下
            ];
            let anyClickable = false, anyInViewport = false;
            for (const [px, py] of pts) {
              const r = ok(px, py);
              if (r === null) continue;      // 视口外
              anyInViewport = true;
              if (r) { anyClickable = true; break; }
            }
            if (!anyInViewport) return false;  // 所有采样点都在视口外 → 不判遮挡（交给 scroll）
            return !anyClickable;              // 没有一个采样点可点 → 遮挡
          } catch (e) {
            return false;
          }
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
          // iframe 偏移累加 → 顶层视口绝对坐标（与点击路径 hover/click 同一套逻辑）。
          // 采集在各 frame 自身上下文执行，此刻 window.frameElement 可用；合并阶段（主 frame）拿不到子 window 做不了。
          // 坐标"出厂即绝对"后，SoM 画框 / 后端 _split_by_viewport / 点击裁剪三处消费者的坐标系才统一。
          // 跨源 iframe：frameElement 访问抛 SecurityError → catch break，偏移停在边界层（与点击路径同限制，非新增缺陷）。
          let offX = 0, offY = 0, w = window;
          while (w !== w.parent) {
            try {
              const f = w.frameElement;
              if (f) { const fr = f.getBoundingClientRect(); offX += fr.left; offY += fr.top; }
              w = w.parent;
            } catch (e) { break; }
          }
          // 父节点轻量指纹：用于跨轮 diff 时判断"新增元素是否成组"（同父=同一簇）
          let parentSig = '';
          const p = el.parentElement;
          if (p) {
            const pr = p.getBoundingClientRect();
            parentSig = `${p.tagName}:${Math.round(pr.top)}:${Math.round(pr.left)}:${p.children.length}`;
          }
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
            bounding_box: { x: Math.round(rect.x + offX), y: Math.round(rect.y + offY), width: Math.round(rect.width), height: Math.round(rect.height) },
            visible: true,
            occluded: isOccluded(el, rect),
            enabled: !el.disabled && !el.classList.contains('jmtd-date-picker-cell-disabled'),
            checked: el.checked !== undefined ? el.checked : null,
            contenteditable: el.isContentEditable || false,
            parent_sig: parentSig,
            in_popup: inPopup
          };
        }

        // 检测可见弹出层（取最顶层的）
        function findVisiblePopups() {
          const popups = [];
          const pushUnique = (el) => {
            if (!el || !isVisible(el)) return;
            if (popups.some(p => p === el || p.contains(el))) return;
            for (let i = popups.length - 1; i >= 0; i--) {
              if (el.contains(popups[i])) popups.splice(i, 1);
            }
            popups.push(el);
          };
          // 判断一个（仅靠宽泛 class 命中的）容器是否真是"临时浮层"，而非常驻侧栏/助手面板。
          // 弹层特征取其一即可：① 模态（有遮罩 / aria-modal / role=dialog）；② 定位脱离文档流
          // （fixed/absolute）且未贴满整条视口边（常驻侧栏通常 fixed 且高度≈满屏、紧贴左右边）。
          const looksLikeFloatingLayer = (el) => {
            if (el.getAttribute('aria-modal') === 'true') return true;
            const role = el.getAttribute('role');
            if (role === 'dialog' || role === 'listbox' || role === 'menu') return true;
            // 存在可见的遮罩层 → 模态
            if (document.querySelector(
              '.jmtd-modal-mask, .ant-modal-mask, .el-overlay, .el-dialog__wrapper, ' +
              '[class*="modal-mask"], [class*="overlay"], [class*="mask"][style*="display"]'
            )) return true;
            const style = window.getComputedStyle(el);
            if (style.position !== 'fixed' && style.position !== 'absolute') return false;
            const rect = el.getBoundingClientRect();
            const vw = window.innerWidth, vh = window.innerHeight;
            // 贴边常驻栏：几乎满高(>=85%视口) 且 紧贴左或右边 → 判为常驻，不算弹层
            const nearlyFullHeight = rect.height >= vh * 0.85;
            const pinnedToSide = rect.left <= 2 || rect.right >= vw - 2;
            if (nearlyFullHeight && pinnedToSide) return false;
            // 几乎铺满整个视口的容器（页面级包裹层）也不算弹层
            if (rect.width >= vw * 0.95 && rect.height >= vh * 0.95) return false;
            return true;
          };
          const consider = (el, needsVerify) => {
            if (!isVisible(el)) return;
            if (needsVerify && !looksLikeFloatingLayer(el)) return;
            const dominated = popups.some(p => p.contains(el));
            if (dominated) return;
            for (let i = popups.length - 1; i >= 0; i--) {
              if (el.contains(popups[i])) popups.splice(i, 1);
            }
            popups.push(el);
          };
          for (const sel of POPUP_TRUSTED_SELECTORS) {
            for (const el of document.querySelectorAll(sel)) consider(el, false);
          }
          for (const sel of POPUP_FUZZY_SELECTORS) {
            for (const el of document.querySelectorAll(sel)) consider(el, true);
          }
          // ARIA 兜底：只认 W3C 标准的「浮层型」信号（不碰厂商 class，也不认常驻的 group/tree，
          // 否则常驻容器会让 active_popup 永远非空，压制真正弹层的 popup_appeared 信号）
          // 1) aria-expanded=true 的触发器所控制的组（aria-controls 或紧邻兄弟）—— 明确"刚展开"
          for (const trigger of document.querySelectorAll('[aria-expanded="true"]')) {
            if (!isVisible(trigger)) continue;
            const controlsId = trigger.getAttribute('aria-controls');
            let group = controlsId ? document.getElementById(controlsId) : null;
            if (!group && trigger.nextElementSibling) group = trigger.nextElementSibling;
            pushUnique(group);
          }
          // 2) 可见的 listbox / menu —— 语义上就是弹出选择层
          for (const el of document.querySelectorAll('[role="listbox"],[role="menu"]')) {
            pushUnique(el);
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
            const candidates = queryAllDeep(root, selector);
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

        // 清除旧标记（穿透 shadow DOM）
        queryAllDeep(document, '[data-agent-id]').forEach(el => el.removeAttribute('data-agent-id'));

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
          menu_texts: (() => {
            const raw = Array.from(document.querySelectorAll(
              'nav a, [class*="menu"] a, [class*="nav"] a, [role="menuitem"]'
            )).map(el => (el.textContent || '').trim());
            const seen = new Set();
            const result = [];
            for (const t of raw) {
              if (!t || t.length > 8) continue;
              if (/[\d()（）]/.test(t)) continue;
              if (seen.has(t)) continue;
              seen.add(t);
              result.push(t);
              if (result.length >= 12) break;
            }
            return result;
          })(),
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
          text_content_summary: (() => {
            // 去导航化正文提取：剔除全局导航/页头/侧栏，取主内容，避免每页重复的菜单噪音挤占注入额度
            const NAV_SEL = 'nav, header, aside, footer, [role="navigation"], [role="banner"],'
              + '[class*="sidebar"], [class*="side-bar"], [class*="navbar"], [class*="nav-bar"],'
              + '[class*="menu"]:not([class*="content"]), [class*="header"]:not([class*="content"]),'
              + '[class*="breadcrumb"], [class*="topbar"], [class*="top-bar"], [class*="footer"]';
            const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
            // 1) 优先标准主内容容器
            let main = document.querySelector('main, [role="main"], article, .main-content, [class*="main-content"]');
            if (main && clean(main.textContent).length >= 80) {
              return clean(main.textContent).slice(0, 3500);
            }
            // 2) 启发式：clone body，删掉导航类节点，取剩余文本
            try {
              const clone = document.body.cloneNode(true);
              clone.querySelectorAll(NAV_SEL + ', script, style, noscript').forEach(n => n.remove());
              const txt = clean(clone.textContent);
              if (txt.length >= 80) return txt.slice(0, 3500);
            } catch (e) { /* 回退 */ }
            // 3) 兜底：全 body
            return clean(document.body?.textContent).slice(0, 3500);
          })(),
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
      // 更新 iframe 内的 data-agent-id 属性以匹配 offset 后的值。
      // 关键：offset 后的新 id 区间(N+1..N+M)会与子 frame 内仍存活的原 id 区间(1..M)重叠，
      // 若边查边改会让 querySelector 命中刚改过的节点，产生重复 id → resolveByIndex 点错元素。
      // 因此先一次性把所有原 id 解析成节点引用（此时 DOM 未改），再统一写入。
      chrome.scripting.executeScript({
        target: { tabId: tab.id, frameIds: [results[i].frameId || i] },
        func: (mapping) => {
          const pairs = mapping
            .map(({ original, newId }) => ({
              el: document.querySelector(`[data-agent-id="${original}"]`),
              newId
            }))
            .filter(p => p.el);
          for (const { el, newId } of pairs) el.setAttribute('data-agent-id', String(newId));
        },
        args: [idMapping]
      }).catch(() => {});
      if (!mainResult.element_count_truncated && frameResult.element_count_truncated) {
        mainResult.element_count_truncated = true;
      }
    }

    // 多模态：截当前可见区 + Set-of-Marks 编号标注，作为 LLM 自评的 ground truth（失败不阻断）
    // 关键：必须叠加编号框，否则模型看得见元素却猜不准 index，会反复点错（对齐 browser-use）
    try {
      const raw = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 60 });
      mainResult.screenshot = await annotateScreenshotSoM(
        raw, mainResult.interactive_elements, mainResult.viewport
      );
    } catch (e) {
      mainResult.screenshot = '';
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

    const tab = await getActiveBrowserTab();
    if (!tab?.id) throw new Error('无法获取当前标签页');

    const execPromise = chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: async (actionData) => {
        // 索引直连：按观察时打标的 data-agent-id 直取节点（穿透 open shadow DOM）
        function resolveByIndex(index) {
          if (index === undefined || index === null) return null;
          const sel = `[data-agent-id="${index}"]`;
          let el = document.querySelector(sel);
          if (el) return el;
          // 穿透 shadow：递归所有 open shadowRoot 查找
          const stack = [document];
          let guard = 0;
          while (stack.length && guard < 500) {
            guard++;
            const root = stack.pop();
            let hosts;
            try { hosts = root.querySelectorAll('*'); } catch (e) { continue; }
            for (const h of hosts) {
              if (h.shadowRoot && h.shadowRoot.mode === 'open') {
                const hit = h.shadowRoot.querySelector(sel);
                if (hit) return hit;
                stack.push(h.shadowRoot);
              }
            }
          }
          return null;
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

        const { type, index, params = {} } = actionData;
        // 索引直连：按 data-agent-id 直取；编号失效（页面已重渲染）→ stale，交由外层重新观察
        const needsEl = (type !== 'scroll' && type !== 'press_key' && type !== 'navigate' && type !== 'wait');
        let element = needsEl ? resolveByIndex(index) : (index != null ? resolveByIndex(index) : null);

        if (needsEl && !element) {
          return { success: false, stale: true, error: `编号 ${index} 已失效（页面已更新）`, action_type: type };
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
              // Phase 1: 先滚进视口（对齐 browser-use scrollIntoViewIfNeeded）
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              await new Promise(r => setTimeout(r, 300));
              const rect = element.getBoundingClientRect();
              const cx = rect.left + rect.width / 2;
              const cy = rect.top + rect.height / 2;

              // Phase 2: 遮挡检测——中心点最上层是不是目标本身/其后代/关联
              let occluded = false;
              try {
                let top = document.elementFromPoint(cx, cy);
                while (top && top.shadowRoot) {
                  const inner = top.shadowRoot.elementFromPoint(cx, cy);
                  if (!inner || inner === top) break;
                  top = inner;
                }
                if (top) {
                  occluded = !(top === element || element.contains(top) || top.contains(element));
                }
              } catch (e) { /* 检测失败当未遮挡 */ }

              // Phase 3b: 遮挡 → 直接 element.click() 绕过遮挡层（DOM 原生，不受视觉遮挡影响）
              if (occluded) {
                try {
                  const opts = { bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy };
                  element.dispatchEvent(new PointerEvent('pointerdown', opts));
                  element.dispatchEvent(new MouseEvent('mousedown', opts));
                  element.dispatchEvent(new PointerEvent('pointerup', opts));
                  element.dispatchEvent(new MouseEvent('mouseup', opts));
                  element.dispatchEvent(new MouseEvent('click', opts));
                  element.click();
                } catch (e) { /* ignore */ }
                return {
                  success: true,
                  details: `点击了 [${index}]（遮挡，已用DOM点击绕过）`,
                  action_type: type
                  // 不返回 _clickCoords → 外层跳过 debugger 坐标点击
                };
              }

              // Phase 3a: 未遮挡 → 计算顶层视口绝对坐标，交外层 debugger 派发真实鼠标事件
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
                details: `点击了 [${index}]`,
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
              return { success: true, details: `清空了 [${index}]`, action_type: type };
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
                // 在下拉弹出层中查找选项。仅用精确文本相等（去空白、小写），
                // 不用子串匹配：substring 会让 option_text="1" 命中 "10"/"100"，
                // 正是 CLAUDE.md 明令删除的模糊匹配 wrong-click 模式。找不到则明确失败，
                // 让 LLM 重新决策，而不是猜一个错的。
                const optionText = (params.option_text || params.value || '').toLowerCase().trim();
                const dropdownItems = document.querySelectorAll(
                  '[role="option"], [role="listbox"] li, .ant-select-item, .el-select-dropdown__item, ' +
                  '[class*="option"], [class*="menu-item"], [class*="dropdown"] li'
                );
                let targetOption = null;
                for (const item of dropdownItems) {
                  const t = (item.textContent || '').trim().toLowerCase();
                  if (t === optionText) {
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
              return { success: true, details: `选择了 ${params.option_text || params.value || '?'}`, action_type: type };
            }

            case 'scroll': {
              const dir = params.direction || 'down';
              const amount = params.amount || 300;
              const yDelta = (dir === 'down') ? amount : (dir === 'up' ? -amount : 0);
              const xDelta = (dir === 'right') ? amount : (dir === 'left' ? -amount : 0);
              let scrollTarget = (index != null) ? resolveByIndex(index) : null;
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
                  ? `已滚动到 [${index}]，元素现在在视口内`
                  : `已尝试滚动到 [${index}]`,
                action_type: type
              };
            }

            case 'hover': {
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              await new Promise(r => setTimeout(r, 200));
              const rect = element.getBoundingClientRect();
              const cx = rect.left + rect.width / 2;
              const cy = rect.top + rect.height / 2;
              // 计算顶层视口绝对坐标（处理 iframe 偏移）
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
                } catch (e) { break; }
              }
              // 同时派发合成事件（对 JS mouseenter 浮层兜底），坐标交外层 debugger 真实移动
              const evtOpts = { bubbles: true, clientX: cx, clientY: cy };
              element.dispatchEvent(new PointerEvent('pointerenter', evtOpts));
              element.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
              element.dispatchEvent(new MouseEvent('mouseenter', evtOpts));
              return {
                success: true,
                details: `悬停在 [${index}]`,
                action_type: type,
                _hoverCoords: { x: cx + offsetX, y: cy + offsetY }
              };
            }

            case 'focus':
              element.scrollIntoView({ block: 'center', behavior: 'smooth' });
              element.focus();
              return { success: true, details: `聚焦到 [${index}]`, action_type: type };

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
        // 回退：合成事件，按 data-agent-id 直取目标。
        // 注意：必须穿透 open shadow DOM 解析节点——裸 document.querySelector 找不到
        // shadow root 内的元素，会静默不派发任何事件却让外层仍报 success:true。
        await chrome.scripting.executeScript({
          target: { tabId: tab.id, allFrames: true },
          func: (idx) => {
            if (idx === undefined || idx === null) return;
            const sel = `[data-agent-id="${idx}"]`;
            let el = document.querySelector(sel);
            if (!el) {
              // 递归穿透所有 open shadowRoot（对齐 resolveByIndex）
              const stack = [document];
              let guard = 0;
              while (stack.length && guard < 500 && !el) {
                guard++;
                const root = stack.pop();
                let hosts;
                try { hosts = root.querySelectorAll('*'); } catch (e) { continue; }
                for (const h of hosts) {
                  if (h.shadowRoot && h.shadowRoot.mode === 'open') {
                    const hit = h.shadowRoot.querySelector(sel);
                    if (hit) { el = hit; break; }
                    stack.push(h.shadowRoot);
                  }
                }
              }
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
          args: [action.index ?? null]
        }).catch(() => {});
      }
      delete result._clickCoords;
    }

    // hover 操作：用 debugger 真实移动鼠标触发悬浮浮层（CSS/JS hover 都生效）
    if (result.success && result._hoverCoords && action.type === 'hover') {
      const { x, y } = result._hoverCoords;
      try {
        await chrome.runtime.sendMessage({
          type: 'DEBUGGER_HOVER', tabId: tab.id, x: Math.round(x), y: Math.round(y)
        });
        // 等浮层展开动画完成；鼠标停留在触发器上，下一步采集能抓到浮层内元素
        await new Promise(r => setTimeout(r, 500));
      } catch (e) { /* debugger 不可用，已派发合成事件兜底 */ }
      delete result._hoverCoords;
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
    completeEl.textContent = success !== false
      ? `✅ 任务完成: ${summary || '已完成所有操作'}`
      : `⚠️ 任务未能完成: ${summary || ''}`;
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

        // 智能等待页面稳定
        const activeTab = await getActiveBrowserTab().catch(() => null);
        if (activeTab?.id) await waitForPageSettle(activeTab.id);

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

        const newPageState = await observePageState();

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

