document.addEventListener('DOMContentLoaded', async () => {
  let attachedImage = null;
  let currentChatId = '';
  // 仅兼容直连模式使用前端历史；本项目后端由服务端管理上下文。
  let chatMessages = [];
  const serverContextBases = new Set();
  const sessionSequences = new Map();
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
    // 例外:`[` 后紧跟换行(块公式裸括号形式,由 preprocessLaTeX 处理),不动。
    if (/\[[^\n\]]*\]\([^)]*$/.test(text) || /\[[^\n\]]*$/.test(text)) {
      text = text.replace(/\[([^\n\]]*)\]\([^)]*$/, '$1').replace(/\[([^\n\]]*)$/, '$1');
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

  // ═══════════════════════════════════════════════════════════════════════════
  // preprocessLaTeX:模型输出的数学定界符归一,骨架抄自 LibreChat/NextChat。
  // 目的:让 marked 看到 $$...$$ / $...$,同时保护 \/_ 等字符不被 markdown 吞。
  // 顺序至关重要,不能颠倒:
  //   1. 抽出代码块和行内 code(math 不能出现在代码里)
  //   2. 抽出已闭合的 $$...$$、\[...\]、\(...\)、$...$(用占位符,marked 看不见)
  //   3. 保守裸括号兜底:整行 [ 到 整行 ] 且含 LaTeX 特征 → 转 $$...$$
  //      (裸行内 (...) 不动 —— 与 \left( 和散文括号无法可靠区分)
  //   4. 货币兜底:$100 之类的 $ 前置反斜杠转义,不当行内公式定界
  //   5. 送 marked / DOMPurify
  //   6. 换回占位符(原始 math + code 源码回到 HTML,再交 KaTeX)
  // ═══════════════════════════════════════════════════════════════════════════

  // 用 U+E000 私有区字符做占位符,不出现在正常文本里,零冲突。
  const PH_CODE = '';
  const PH_MATH = '';

  // 步骤 0(兜底):fence 前后补空行,marked 要求 fence 独占段落。
  // 模型常直接接在段落后 "text\n```",或 fence 后无空行 "```\ntext",导致不识别。
  function fixFenceSpacing(text) {
    if (!text) return '';
    // fence 前非空行 → 补空行
    let out = text.replace(/([^\n])\n(```)/g, '$1\n\n$2');
    // fence 后非空行 → 补空行(fence 行可带语言标识,如 ```python)
    out = out.replace(/(```[^\n]*)\n([^\n])/g, '$1\n\n$2');
    return out;
  }

  // 步骤 1+2:抽出代码和已闭合的 math 段。返回 { text, code, math }。
  function extractProtected(text) {
    const code = [];
    const math = [];
    // 只抽行内 code(fence 留给 marked 原生处理,抽走会破坏段落结构)
    // 行内 code 可能和 $...$ 冲突,必须先抽
    let out = text.replace(/`[^`\n]+`/g, (m) => {
      code.push(m); return `${PH_CODE}${code.length - 1}${PH_CODE}`;
    });
    // 已闭合的 math:$$...$$、\[...\]、\(...\)、$...$ 按此顺序抽,长的优先
    const pushMath = (raw, display, body) => {
      math.push({ display, body }); return `${PH_MATH}${math.length - 1}${PH_MATH}`;
    };
    out = out.replace(/\$\$([\s\S]+?)\$\$/g, (m, b) => pushMath(m, true, b));
    out = out.replace(/\\\[([\s\S]+?)\\\]/g, (m, b) => pushMath(m, true, b));
    out = out.replace(/\\\(([\s\S]+?)\\\)/g, (m, b) => pushMath(m, false, b));
    // 单 $:同行内、非空、且 $ 前不是数字/字母/反斜杠(避免 $100、a$b、\$)
    out = out.replace(/(^|[^\\\w])\$([^\n$]+?)\$(?!\d)/g, (m, pre, b) => pre + pushMath(m, false, b));
    return { text: out, code, math };
  }

  // 步骤 3:裸 [ ... ] 兜底(仅当独占行 + 含 LaTeX 特征)
  function convertBareBrackets(text, math) {
    // 匹配"^ [ $"独占一行,到"^ ] $"独占一行,中间任意
    return text.replace(
      /^[ \t]*\[[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*\][ \t]*$/gm,
      (m, body) => {
        // LaTeX 特征:反斜杠命令 / 上下标 / 花括号 / \frac \sum 等
        if (!/\\|[\^_{}]/.test(body)) return m;
        math.push({ display: true, body });
        return `${PH_MATH}${math.length - 1}${PH_MATH}`;
      }
    );
  }

  // 步骤 4:货币兜底 —— $ 后紧跟数字的独立 $,转义避免误当定界符残留
  function escapeCurrency(text) {
    return text.replace(/\$(?=\d)/g, '\\$');
  }

  // 步骤 6:把占位符换回。math 换回带定界符,让 KaTeX auto-render 再扫。
  function restoreProtected(html, code, math) {
    let out = html;
    if (math.length) {
      const re = new RegExp(`${PH_MATH}(\\d+)${PH_MATH}`, 'g');
      out = out.replace(re, (_m, i) => {
        const seg = math[Number(i)];
        if (!seg) return '';
        return seg.display ? `$$${seg.body}$$` : `$${seg.body}$`;
      });
    }
    if (code.length) {
      const re = new RegExp(`${PH_CODE}(\\d+)${PH_CODE}`, 'g');
      out = out.replace(re, (_m, i) => code[Number(i)] || '');
    }
    return out;
  }

  function preprocessLaTeX(text) {
    if (!text) return { text: '', code: [], math: [] };
    const t0 = fixFenceSpacing(text);
    const { text: t1, code, math } = extractProtected(t0);
    const t2 = convertBareBrackets(t1, math);
    const t3 = escapeCurrency(t2);
    return { text: t3, code, math };
  }

  // 流式期间截断未闭合的公式尾巴,避免字面 [ 或 $$ 闪现。
  // 找 buffer 里最后一个"开了但没关"的 math 开定界符,截到它之前。
  // 判定:$$ 数量奇 / \[ 数量 > \] 数量 / \( 数量 > \) 数量 → 尾部有未闭合
  function truncateUnclosedMath(text) {
    if (!text) return '';
    // 简单方式:扫一遍找最后一个未闭合 opener 的位置
    const openers = [];
    // $$ 优先(会吞 \[)
    let i = 0;
    while (i < text.length) {
      if (text[i] === '\\' && (text[i + 1] === '[' || text[i + 1] === '(')) {
        const close = text[i + 1] === '[' ? '\\]' : '\\)';
        const end = text.indexOf(close, i + 2);
        if (end === -1) { openers.push(i); break; }
        i = end + 2;
      } else if (text[i] === '$' && text[i + 1] === '$') {
        const end = text.indexOf('$$', i + 2);
        if (end === -1) { openers.push(i); break; }
        i = end + 2;
      } else {
        i++;
      }
    }
    if (openers.length === 0) return text;
    return text.slice(0, openers[0]);
  }

  // marked(解析) → 补全未闭合行内标记 → DOMPurify → innerHTML → KaTeX。
  // 流式:先截未闭合公式,再补 markdown 悬空标记;非流式:完整跑。
  function renderMarkdownInto(el, text, opts) {
    const streaming = opts && opts.streaming;
    let src = text || '';
    if (streaming) src = truncateUnclosedMath(src);
    if (streaming) src = closeOpenMarks(src);
    const pre = preprocessLaTeX(src);
    let html;
    if (window.marked && typeof window.marked.parse === 'function') {
      html = window.marked.parse(pre.text);
      if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
        html = window.DOMPurify.sanitize(html);
      }
      html = restoreProtected(html, pre.code, pre.math);
      el.innerHTML = html;
      addCodeCopyButtons(el);
      if (typeof window.renderMathInElement === 'function') {
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
            reject(Object.assign(new Error(response?.error || '后台请求失败'), { status: response?.status }));
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
          if (!response?.ok) {
            const error = new Error(response?.error || '后台请求失败');
            error.status = response?.status;
            reject(error); return;
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
      const target = e.currentTarget.dataset.target;
      activateTab(target);
      // 记忆 tab:切回时刷新列表;modal 只在 idle 时才隐藏(running/done 保持显示)
      if (target === 'memory') {
        const modal = document.getElementById('rethinkModal');
        if (modal && _rethinkState === 'idle') modal.style.display = 'none';
        renderAddMemoryBtn();   // 恢复添加按钮状态(添加中/已添加/idle)
        loadMemoryPanel().catch((err) => console.warn('加载记忆面板失败', err));
      }
    });
  });

  // 2. 加载与保存设置
  const config = await chrome.storage.local.get(['apiUrl', 'modelName', 'agentLlmParams']);
  const storedCredential = await getStoredApiCredential();
  const apiUrlInput = document.getElementById('apiUrl');
  const apiKeyInput = document.getElementById('apiKey');
  const modelNameInput = document.getElementById('modelName');
  const agentLlmParamsInput = document.getElementById('agentLlmParams');
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
  agentLlmParamsInput.value = config.agentLlmParams || '';
  updateApiKeyStatus(Boolean(storedCredential.apiKey), storedCredential.apiKeyApiUrl);

  // 自定义 LLM 参数的黑名单键：由 harness 独占,用户不能覆盖。
  const AGENT_LLM_PARAMS_FORBIDDEN = new Set(['model', 'messages', 'stream', 'api_key', 'apiKey', 'timeout', 'n']);

  // 解析并校验用户填的 JSON。空串合法(返回 null)。返回 [parsed | null, errorMsg | ''].
  function parseAgentLlmParams(raw) {
    const text = (raw || '').trim();
    if (!text) return [null, ''];
    let obj;
    try { obj = JSON.parse(text); } catch (e) { return [null, `JSON 语法错误: ${e.message}`]; }
    if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) return [null, 'JSON 必须是对象(非数组/非字符串)'];
    for (const k of Object.keys(obj)) {
      if (AGENT_LLM_PARAMS_FORBIDDEN.has(k)) return [null, `禁止字段: ${k}(由系统管理)`];
    }
    if ('extra_body' in obj) {
      const eb = obj.extra_body;
      if (eb === null || typeof eb !== 'object' || Array.isArray(eb)) return [null, 'extra_body 必须是对象'];
    }
    return [obj, ''];
  }

  document.getElementById('saveSettingsBtn').addEventListener('click', async () => {
    const apiUrl = apiUrlInput.value.replace(/\/$/, '');
    const enteredApiKey = apiKeyInput.value.trim();
    const modelName = modelNameInput.value.trim() || 'gpt-3.5-turbo';
    const [parsedLlmParams, llmParamsErr] = parseAgentLlmParams(agentLlmParamsInput.value);
    if (llmParamsErr) { showSettingsMessage(`自动化 LLM 参数无效: ${llmParamsErr}`, 'error'); return; }
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

    await chrome.storage.local.set({ apiUrl: safeApiUrl, modelName, agentLlmParams: agentLlmParamsInput.value.trim() });
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

    if (await supportsServerContext(safeApiUrl)) {
      await runServerChat(text, image, search_query, { apiKey, modelName, safeApiUrl });
      return;
    }

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
      kb_id: window._kbBoundId || '',
    };
    const requestHeaders = { 'Content-Type': 'application/json' };
    if (String(apiKey || '').trim()) {
      requestHeaders.Authorization = `Bearer ${String(apiKey).trim()}`;
    }

    const msgId = createMessageId();
    let fullReply = '';
    let done = false;
    let _searchSources = [];  // 联网搜索结果元数据
    // aiBubble 内部结构分离：增强卡片区 + markdown 内容区
    // streamer 只渲染 markdown 内容区，不影响增强卡片
    aiBubble.textContent = '';
    const mdBody = document.createElement('div');
    mdBody.className = 'markdown-body';
    aiBubble.appendChild(mdBody);
    const streamer = createMarkdownStreamer(mdBody);

    await new Promise((resolve) => {
      const finalize = () => {
        if (done) return;
        done = true;
        if (!fullReply) mdBody.textContent = '响应为空。';
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
        } else if (msg.type === 'LLM_ENHANCEMENT_STEP') {
          // 增强步骤事件：更新增强卡片
          updateEnhancementCard(aiBubble, msg.step);
          // 工具调用完成时收集搜索来源(供 finalize 渲染引用面板)
          if (msg.step.status === 'done' && msg.step.sources && msg.step.sources.length) {
            _searchSources = _searchSources.concat(msg.step.sources);
          }
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

  async function supportsServerContext(base) {
    if (serverContextBases.has(base)) return true;
    try {
      const data = await callBackendApi(buildBackendEndpointUrl(base, '/v1/sessions/capabilities'));
      if (data?.server_context === true && data.protocol_version === 1) {
        serverContextBases.add(base);
        return true;
      }
    } catch {}
    return false;
  }

  function addRequestRecovery(container, base, chatId, requestId, originalBody = null) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'tool-btn';
    button.textContent = '检查状态 / 重试';
    button.addEventListener('click', async () => {
      if (_sendingLock) return;
      if (currentChatId !== chatId) { alert('请先打开此请求所属的会话。'); return; }
      _sendingLock = true;
      button.disabled = true;
      try {
        let state;
        try {
          state = await callBackendApi(buildBackendEndpointUrl(base,
            `/v1/sessions/${encodeURIComponent(chatId)}/requests/${encodeURIComponent(requestId)}`));
        } catch (error) {
          if (error.status !== 404 || !originalBody) throw error;
          state = { can_retry: true, last_seq: originalBody.expected_last_seq };
        }
        sessionSequences.set(`${base}|${chatId}`, state.last_seq);
        if (state.status === 'completed') {
          await resumeSession(chatId);
        } else if (state.status === 'running') {
          alert('后端仍在处理此请求，暂不重复提交，请稍后检查。');
        } else if (state.can_retry) {
          const body = originalBody || state.retry_request;
          if (!body) { alert('此请求包含未保存的图片，请重新提供图片后发送。'); return; }
          const settings = await resolveApiRequestConfig();
          if (settings.safeApiUrl !== base) { alert('后端地址已改变，请切回原地址后重试。'); return; }
          if (currentChatId !== chatId) { alert('当前会话已改变，请回到原会话后重试。'); return; }
          await runServerChat('', null, '', settings, body);
        } else {
          alert('此失败请求后已有其他消息，请作为新问题重新发送。');
        }
      } catch (error) {
        alert('无法确认请求状态: ' + (error.message || error));
      } finally { button.disabled = false; _sendingLock = false; }
    });
    container.appendChild(button);
  }

  async function runServerChat(text, image, searchQuery, settings, retryBody = null) {
    const { apiKey, modelName, safeApiUrl: base } = settings;
    const chatId = retryBody?.chat_id || await getOrCreateCurrentChatId();
    const key = `${base}|${chatId}`;
    let body;
    try {
      if (!sessionSequences.has(key)) {
        const state = await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/messages?limit=1`));
        sessionSequences.set(key, state.last_seq);
      }
      body = retryBody || {
        context_mode: 'server', chat_id: chatId, request_id: createMessageId(),
        expected_last_seq: sessionSequences.get(key), model: String(modelName).trim(), stream: true,
        kb_id: window._kbBoundId || '', search_query: searchQuery,
        messages: [{ role: 'user', content: image ? [
          { type: 'text', text: text || '请分析这张图片' }, { type: 'image_url', image_url: { url: image } }
        ] : text }],
      };
    } catch (error) {
      document.getElementById('chatInput').value = text;
      if (image) applyAttachedImage({ dataUrl: image, name: '待发送图片' });
      alert('无法同步会话，尚未提交: ' + error.message);
      return;
    }
    if (!retryBody) {
      const user = createMessageNode('user');
      user.dataset.requestId = body.request_id;
      const label = document.createElement('div'); label.textContent = text;
      user.appendChild(label);
      if (image) {
        const preview = document.createElement('img'); preview.className = 'user-upload-preview';
        preview.src = image; preview.alt = '本轮图片'; user.appendChild(preview);
      }
    }
    const bubble = createMessageNode('ai');
    bubble.dataset.requestId = body.request_id;
    const content = document.createElement('div'); content.className = 'markdown-body'; bubble.appendChild(content);
    const streamer = createMarkdownStreamer(content);
    const msgId = createMessageId();
    let reply = ''; let meta = null; let sources = []; let done = false;
    await new Promise(resolve => {
      const finish = (error = '') => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        chrome.runtime.onMessage.removeListener(listener);
        if (reply) streamer.finalize(reply); else streamer.cancel();
        if (meta?.persisted && !error) {
          sessionSequences.set(key, meta.last_seq);
          if (sources.length) renderSearchCitations(bubble, sources);
        } else {
          const status = document.createElement('div'); status.className = 'error-text';
          status.textContent = `未确认保存：${error || '连接已结束，但没有收到保存确认'}`;
          bubble.appendChild(status);
          addRequestRecovery(bubble, base, chatId, body.request_id, body);
        }
        resolve();
      };
      const listener = msg => {
        if (msg.msgId !== msgId) return;
        if (msg.type === 'LLM_CHUNK') { reply += msg.chunk; streamer.update(reply); scrollToBottom(); }
        else if (msg.type === 'LLM_SESSION_META') meta = msg.session_meta;
        else if (msg.type === 'LLM_ENHANCEMENT_STEP') {
          updateEnhancementCard(bubble, msg.step);
          if (msg.step.status === 'done') sources.push(...(msg.step.sources || []));
        } else if (msg.type === 'LLM_ERROR') finish(msg.error);
        else if (msg.type === 'LLM_DONE') finish();
      };
      const timer = setTimeout(() => finish('等待超时，请检查后端状态'), 605000);
      chrome.runtime.onMessage.addListener(listener);
      const headers = { 'Content-Type': 'application/json' };
      if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
      chrome.runtime.sendMessage({ type: 'CALL_LLM_STREAM', msgId, url: `${base}/chat/completions`,
        options: { method: 'POST', headers, body: JSON.stringify(body) } }, () => {
          if (chrome.runtime.lastError) finish(chrome.runtime.lastError.message);
        });
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

  async function resumeSession(chatId, beforeSeq = null) {
    let base;
    try { base = await backendBase(); } catch { return; }
    let data;
    try {
      const managed = await supportsServerContext(base);
      const query = managed ? `?limit=100${beforeSeq ? `&before_seq=${beforeSeq}` : ''}` : '';
      data = await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/messages${query}`), 'GET');
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
    const managed = serverContextBases.has(base);
    if (managed) sessionSequences.set(`${base}|${chatId}`, data.last_seq);
    if (summary && !managed) {
      chatMessages.push({ role: 'system', content: '## 本会话此前摘要\n' + summary });
    }
    const tail = managed ? [] : summaryMsgCount > 0 ? messages.slice(summaryMsgCount) : messages;
    for (const m of tail) {
      chatMessages.push({ role: m.role, content: m.content });
    }
    // DOM 全量渲染(用户看得到完整历史)
    const historyEl = document.getElementById('chatHistory');
    const oldNodes = beforeSeq ? Array.from(historyEl.children).filter(node => node.id !== 'loadOlderMessages') : [];
    historyEl.replaceChildren();
    if (data.has_more) {
      const more = document.createElement('button'); more.id = 'loadOlderMessages'; more.type = 'button';
      more.className = 'tool-btn'; more.textContent = '加载更早消息';
      more.addEventListener('click', () => resumeSession(chatId, data.before_seq));
      historyEl.appendChild(more);
    }
    for (const m of messages) {
      const bubble = createMessageNode(m.role === 'user' ? 'user' : 'ai');
      if (m.request_id) bubble.dataset.requestId = m.request_id;
      if (m.role === 'user') {
        const node = document.createElement('div');
        node.textContent = m.content;
        bubble.appendChild(node);
        if (managed && m.request_id && m.status !== 'completed') {
          const state = document.createElement('div'); state.textContent = `请求状态：${m.status}`;
          bubble.appendChild(state);
          addRequestRecovery(bubble, base, chatId, m.request_id);
        }
      } else {
        // AI 消息:先渲染工具调用步骤(如果有),再渲染正文 + 引用面板
        let toolSteps = [];
        if (m.tools) {
          try { toolSteps = typeof m.tools === 'string' ? JSON.parse(m.tools) : m.tools; } catch {}
        }
        if (toolSteps.length) {
          for (const step of toolSteps) {
            updateEnhancementCard(bubble, step);
          }
        }
        const mdBody = document.createElement('div');
        mdBody.className = 'markdown-body';
        bubble.appendChild(mdBody);
        renderMarkdownInto(mdBody, m.content, { streaming: false });
        // 从工具步骤提取引用来源
        const allSources = [];
        for (const step of toolSteps) {
          if (step.sources && step.sources.length) {
            for (const s of step.sources) allSources.push(s);
          }
        }
        if (allSources.length) {
          renderSearchCitations(bubble, allSources);
        }
      }
    }
    for (const node of oldNodes) historyEl.appendChild(node);
    if (!beforeSeq) scrollToBottom();
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

  // 添加记忆按钮三态:idle=可添加 / adding=添加中(不可点)/ done=✓ 已添加(1.5s 自动回 idle)
  // 参照 rethink 模式,切 tab 后回来能正确恢复按钮显示。
  let _addMemoryState = 'idle';
  let _addMemoryDoneTimer = null;

  function renderAddMemoryBtn() {
    const btn = document.getElementById('memoryAddBtn');
    if (!btn) return;
    if (_addMemoryState === 'adding') {
      btn.disabled = true;
      btn.textContent = '添加中...';
    } else if (_addMemoryState === 'done') {
      btn.disabled = true;
      btn.textContent = '✓ 已添加';
    } else {
      btn.disabled = false;
      btn.textContent = '添加';
    }
  }

  async function addMemory() {
    if (_addMemoryState === 'adding') return;   // 防重复点击
    const input = document.getElementById('memoryAddInput');
    const subjectInput = document.getElementById('memoryAddSubject');
    const content = String(input?.value || '').trim();
    if (!content) return;
    const subject = String(subjectInput?.value || '').trim();
    _addMemoryState = 'adding';
    renderAddMemoryBtn();
    try {
      const base = await backendBase();
      await callBackendApi(buildBackendEndpointUrl(base, '/v1/memory'), 'POST',
        { content, memory_type: 'core', subject });
      input.value = '';
      if (subjectInput) subjectInput.value = '';
      _addMemoryState = 'done';
      renderAddMemoryBtn();
      // 1.5s 后回 idle;切 tab 期间不受影响,回来时 renderAddMemoryBtn 会按状态渲染
      if (_addMemoryDoneTimer) clearTimeout(_addMemoryDoneTimer);
      _addMemoryDoneTimer = setTimeout(() => {
        _addMemoryState = 'idle';
        renderAddMemoryBtn();
      }, 1500);
      await loadMemoryPanel();
    } catch (e) {
      _addMemoryState = 'idle';
      renderAddMemoryBtn();
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

  // ─── 增强卡片相关函数 ─────────────────────────────────────────
  // ChatGPT/Codex 风格：无边框，灰色小字，一行一个步骤

  function getOrCreateEnhancementCard(bubbleEl) {
    let card = bubbleEl.querySelector('.enhancement-card');
    if (!card) {
      card = document.createElement('div');
      card.className = 'enhancement-card';
      // 步骤直接平铺，无 header/边框
      const markdownBody = bubbleEl.querySelector('.markdown-body');
      if (markdownBody) {
        bubbleEl.insertBefore(card, markdownBody);
      } else {
        bubbleEl.appendChild(card);
      }
    }
    return card;
  }

  function updateEnhancementCard(bubbleEl, step) {
    const card = getOrCreateEnhancementCard(bubbleEl);
    const esc = (t) => String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    const stepId = `${step.type}_${step.query || ''}`;
    let stepEl = card.querySelector(`[data-step-id="${CSS.escape(stepId)}"]`);

    const icon = step.type === 'web_search' ? '🔍' : '📚';

    if (step.status === 'running') {
      if (!stepEl) {
        stepEl = document.createElement('div');
        stepEl.className = 'enhancement-step running';
        stepEl.setAttribute('data-step-id', stepId);
        card.appendChild(stepEl);
      }
      const label = step.type === 'web_search' ? '正在搜索' : '正在检索知识库';
      stepEl.innerHTML = `<span class="step-icon">${icon}</span><span class="step-text">${label} “${esc(step.query)}”</span>`;
    } else if (step.status === 'done') {
      if (!stepEl) {
        stepEl = document.createElement('div');
        stepEl.setAttribute('data-step-id', stepId);
        card.appendChild(stepEl);
      }
      stepEl.className = 'enhancement-step done';
      const label = step.type === 'web_search' ? '已搜索' : '已检索知识库';
      const count = step.result_count || 0;
      const countText = count > 0 ? ` · ${count} 项` : '';
      stepEl.innerHTML = `<span class="step-icon">${icon}</span><span class="step-text">${label} “${esc(step.query)}”${countText}</span>`;
    }
  }

  // ─── 引用相关函数 ─────────────────────────────────────────

  function renderSearchCitations(bubbleEl, sources) {
    if (!sources || !sources.length) return;
    // 把回答里的 [1] [2] 渲染为可点击引用链接
    const contentEl = bubbleEl.querySelector('.markdown-body') || bubbleEl;
    if (contentEl.innerHTML) {
      contentEl.innerHTML = contentEl.innerHTML.replace(/\[(\d+)\]/g, (match, num) => {
        const idx = parseInt(num) - 1;
        const src = sources[idx];
        if (!src) return match;
        const isKb = src.url && src.url.startsWith('kb://');
        const safeTitle = (src.title || '').replace(/"/g, '&quot;');
        if (isKb) {
          // KB 引用：不跳转，仅 tooltip 提示
          return `<span class="search-citation kb-citation-inline" title="${safeTitle}">[${num}]</span>`;
        }
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
      const isKb = s.url && s.url.startsWith('kb://');
      const item = document.createElement('div');
      item.className = 'search-source-item' + (isKb ? ' kb-source-item' : '');

      if (isKb) {
        // KB 来源：不可点击，显示文档名 + snippet
        item.innerHTML = `
          <span class="search-source-num">[${i + 1}]</span>
          <span class="kb-source-icon">📚</span>
          <div class="kb-source-detail">
            <div class="kb-source-title">${(s.title || '').replace(/</g, '&lt;')}</div>
            ${s.snippet ? `<div class="kb-source-snippet">${s.snippet.replace(/</g, '&lt;')}</div>` : ''}
          </div>`;
      } else {
        // Web 来源：可点击链接
        const a = document.createElement('a');
        a.className = 'search-source-link';
        a.href = s.url;
        a.target = '_blank';
        a.rel = 'noopener';
        a.innerHTML = `<span class="search-source-num">[${i + 1}]</span> ${(s.title || '').replace(/</g, '&lt;')}`;
        item.appendChild(a);
      }
      list.appendChild(item);
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
    Object.assign(agentState, { active: true, sessionId, task, status: 'running', currentStep: 0 });
    const ownsUI = () => agentState.sessionId === sessionId;

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
    const header = document.createElement('div');
    header.className = 'agent-header';
    const statusText = document.createElement('span');
    statusText.textContent = '🤖 正在准备自动化任务…';
    header.appendChild(statusText);
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'agent-btn-cancel';
    cancelBtn.textContent = '⏹ 停止';
    header.appendChild(cancelBtn);
    aiBubble.appendChild(header);
    scrollToBottom();

    let run = null;
    let keepalivePort = null;
    let stopRequested = false;
    let backendTerminal = false;
    cancelBtn.onclick = () => {
      stopRequested = true;
      cancelBtn.disabled = true;
      cancelBtn.textContent = '正在停止…';
      statusText.textContent = '正在停止后续动作并释放输入…';
      if (ownsUI()) agentState.status = 'stopping';
      if (run) run.stop().catch(() => {});
    };

    try {
      keepalivePort = chrome.runtime.connect({ name: 'agent-keepalive' });
      keepalivePort.onDisconnect.addListener(() => {});
      const tab = await getActiveBrowserTab();
      if (!tab?.id) throw new Error('无法获取当前标签页');
      const { apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig();
      let llmParams = {};
      try {
        const stored = await chrome.storage.local.get(['agentLlmParams']);
        const parsed = JSON.parse(stored.agentLlmParams || '{}');
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) llmParams = parsed;
      } catch { /* 保持已有默认参数 */ }

      run = new AgentRunner.Run(sessionId, tab.id, {
        message: message => chrome.runtime.sendMessage(message),
        api: (path, body) => callAgentApi(safeApiUrl, path, body, apiKey),
        annotate: page => AgentObservation.annotate(page),
        status: (state, text) => {
          if (!ownsUI()) return;
          agentState.status = state;
          statusText.textContent = text;
        }
      });
      if (stopRequested) { await run.stop(); run.check(); }
      await run.start();
      let pageState = await run.observe();
      let response = await run.decide('/v1/agent/execute', {
        task, page_state: pageState, model: modelName || 'gpt-4o',
        require_confirmation: [], task_image: taskImage || '', llm_params: llmParams
      });

      while (response.status === 'action_required' || response.status === 'confirm_required') {
        run.check();
        if (!ownsUI()) throw new Error('任务已被替换');
        agentState.currentStep = response.step;
        const action = response.action;
        if (!action?.action_id || action.observation_id !== pageState.observation_id) {
          throw new Error('动作与当前观察版本不匹配，未执行');
        }
        if (response.status === 'confirm_required') {
          const confirmed = await run.wait(showAgentConfirmDialog(aiBubble, action, response.thought), run.deadline - Date.now());
          if (!confirmed) { await run.stop(); run.check(); }
        }
        renderAgentStepInBubble(aiBubble, response.step, response.thought, action, null);
        const result = await run.execute(action);
        run.check();
        renderAgentStepInBubble(aiBubble, response.step, null, null, result);

        // 等稳仅帮助减少瞬时变化；只有后续成功的新观察才允许推进。
        await run.wait(waitForPageSettle(tab.id), 5000).catch(error => {
          if (error.code !== 'timeout') throw error;
        });
        run.check();
        const nextPage = await run.observe();
        if (!nextPage.active_popup) {
          const inlineGroup = detectInlineGroup(pageState.interactive_elements || [], nextPage.interactive_elements || []);
          if (inlineGroup) {
            nextPage.active_popup = inlineGroup;
            const sig = e => `${e.tag}|${e.role}|${(e.text || '').slice(0, 20)}|${e.name}`;
            const previous = new Set((pageState.interactive_elements || []).map(sig));
            for (const element of nextPage.interactive_elements) {
              if (!previous.has(sig(element))) element.in_popup = true;
            }
          }
        }
        result.state_changes = {
          url_changed: nextPage.url !== pageState.url,
          popup_appeared: !pageState.active_popup && !!nextPage.active_popup,
          popup_disappeared: !!pageState.active_popup && !nextPage.active_popup,
          element_count_delta: (nextPage.interactive_elements || []).length - (pageState.interactive_elements || []).length
        };
        pageState = nextPage;
        response = await run.decide('/v1/agent/step', { page_state: pageState, action_result: result });
      }
      run.check();
      backendTerminal = ['completed', 'cancelled', 'error'].includes(response.status);
      if (response.status === 'completed') {
        statusText.textContent = response.success === false ? '任务未完成' : '任务完成';
        renderAgentComplete(aiBubble, response.summary, response.success !== false);
      } else if (response.status === 'cancelled') {
        statusText.textContent = '任务已取消';
      } else {
        throw new Error(response.error || '后端未返回有效动作或终态');
      }
    } catch (error) {
      if (error.code === 'cancelled' || stopRequested) {
        statusText.textContent = '正在确认停止状态…';
      } else {
        statusText.textContent = '任务未完成';
        renderAgentError(aiBubble, error.message || '执行失败');
      }
    } finally {
      if (run && !backendTerminal) await run.stop();
      const cleanup = run ? await run.finish() : { safe: true };
      if (cleanup.safe === false) {
        statusText.textContent = '旧动作状态未确认，已阻止后续输入';
        renderAgentError(aiBubble, '请检查页面；当前标签页仍被保护锁定，不会自动重放动作。');
      } else if (stopRequested || run?.abort.signal.aborted && !backendTerminal) {
        statusText.textContent = stopRequested ? '已停止；已发出的操作不会回滚' : '任务已结束';
      }
      if (ownsUI()) Object.assign(agentState, { active: false, status: 'idle' });
      aiBubble.querySelectorAll('.agent-confirm').forEach(element => element.remove());
      cancelBtn.remove();
      if (keepalivePort) { try { keepalivePort.disconnect(); } catch { /* 已断开 */ } }
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


// ═══════════════════════════════════════════════════════════════════
// KB 绑定 chip + 选择器(聊天 tab 内)
// ═══════════════════════════════════════════════════════════════════
(function initKbChip() {
  const API_BASE = 'http://localhost:8000';
  let _kbListCache = [];

  window._kbBoundId = sessionStorage.getItem('kb_id') || '';

  async function _fetchKbList() {
    try {
      const res = await fetch(`${API_BASE}/v1/kb`);
      _kbListCache = await res.json();
    } catch { _kbListCache = []; }
    return _kbListCache;
  }

  function _escHtml(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

  async function renderKbChip() {
    const bar = document.getElementById('kb-chip-bar');
    const nameSpan = document.getElementById('kb-chip-name');
    if (!bar || !nameSpan) return;

    if (!window._kbBoundId) {
      bar.style.display = 'none';
      return;
    }

    if (!_kbListCache.length) await _fetchKbList();
    const kb = _kbListCache.find(k => k.kb_id === window._kbBoundId);
    if (kb) {
      nameSpan.textContent = kb.name;
      bar.style.display = 'flex';
    } else {
      window._kbBoundId = '';
      sessionStorage.removeItem('kb_id');
      bar.style.display = 'none';
    }
  }

  async function showKbSelector() {
    const selector = document.getElementById('kbSelector');
    const list = document.getElementById('kbSelectorList');
    if (!selector || !list) return;

    await _fetchKbList();
    if (!_kbListCache.length) {
      list.innerHTML = '<div class="kb-selector-empty">暂无知识库，请先在 📚 知识库 tab 创建</div>';
    } else {
      list.innerHTML = _kbListCache.map(kb => `
        <div class="kb-selector-item ${kb.kb_id === window._kbBoundId ? 'active' : ''}" data-kb-id="${kb.kb_id}">
          <div class="kb-selector-item-name">${_escHtml(kb.name)}</div>
          <div class="kb-selector-item-desc">${_escHtml(kb.description || '')}</div>
        </div>
      `).join('');
    }
    selector.style.display = 'flex';
  }

  function hideKbSelector() {
    const s = document.getElementById('kbSelector');
    if (s) s.style.display = 'none';
  }

  document.getElementById('kbSelectBtn')?.addEventListener('click', showKbSelector);
  document.getElementById('kbSelectorCancel')?.addEventListener('click', hideKbSelector);

  document.getElementById('kbSelectorList')?.addEventListener('click', (e) => {
    const item = e.target.closest('.kb-selector-item');
    if (!item) return;
    window._kbBoundId = item.dataset.kbId;
    sessionStorage.setItem('kb_id', window._kbBoundId);
    hideKbSelector();
    renderKbChip();
  });

  document.getElementById('kb-chip-remove')?.addEventListener('click', () => {
    window._kbBoundId = '';
    sessionStorage.removeItem('kb_id');
    renderKbChip();
  });

  window.addEventListener('kb-changed', (e) => {
    window._kbBoundId = e.detail.kb_id || '';
    if (window._kbBoundId) {
      sessionStorage.setItem('kb_id', window._kbBoundId);
    } else {
      sessionStorage.removeItem('kb_id');
    }
    renderKbChip();
  });

  renderKbChip();
})();

// ═══════════════════════════════════════════════════════════════════
// 知识库管理(批次 G)
// ═══════════════════════════════════════════════════════════════════
(function initKB() {
  let currentKbId = sessionStorage.getItem('kb_id') || '';
  let kbs = [];
  let docs = [];
  let viewMode = 'active';
  let trashKbs = [];
  const _busy = new Map();  // key → {text, done}  操作中状态追踪

  const API_BASE = 'http://localhost:8000';

  async function kbRequest(path, options) {
    const response = await fetch(`${API_BASE}${path}`, options);
    const data = await response.json();
    if (!response.ok || data?.ok === false) {
      throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    }
    return data;
  }

  async function loadKBs() {
    try {
      const res = await fetch(`${API_BASE}/v1/kb`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      kbs = await res.json();
      renderKBList();
      return true;
    } catch (err) {
      console.error('加载知识库失败:', err);
      return false;
    }
  }

  function renderKBList() {
    const container = document.getElementById('kbList');
    if (!container) return;
    container.innerHTML = kbs.map(kb => {
      const b = _busy.get('kb-del-' + kb.kb_id);
      const btnText = b ? b.text : '删除';
      const btnDis = b ? 'disabled' : '';
      return `
      <div class="kb-card ${kb.kb_id === currentKbId ? 'selected' : ''}" data-kb-id="${kb.kb_id}">
        <div class="kb-card-name">${escapeHtml(kb.name)}</div>
        <div class="kb-card-desc">${escapeHtml(kb.description || '')}</div>
        <button class="kb-card-delete" data-kb-id="${kb.kb_id}" ${btnDis}>${btnText}</button>
      </div>`;
    }).join('');
  }

  document.getElementById('kbRefreshBtn')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = '刷新中…';
    try {
      const loaded = viewMode === 'trash' ? await loadTrash() : await loadKBs();
      btn.textContent = loaded ? '↻ 刷新' : '刷新失败，重试';
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById('createKbBtn')?.addEventListener('click', async () => {
    const name = prompt('知识库名称:');
    if (!name || !name.trim()) return;
    const description = prompt('描述(可选):') || '';
    const btn = document.getElementById('createKbBtn');
    btn.disabled = true; btn.textContent = '创建中...';
    try {
      await kbRequest('/v1/kb', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name.trim(), description: description.trim()})
      });
      btn.textContent = '✓ 已创建';
      setTimeout(() => { btn.disabled = false; btn.textContent = '+ 新建知识库'; loadKBs(); }, 800);
    } catch (err) {
      btn.textContent = '创建失败';
      setTimeout(() => { btn.disabled = false; btn.textContent = '+ 新建知识库'; }, 1500);
    }
  });

  document.getElementById('kbList')?.addEventListener('click', async (e) => {
    const card = e.target.closest('.kb-card');
    const delBtn = e.target.closest('.kb-card-delete');
    
    if (delBtn) {
      e.stopPropagation();
      const kb_id = delBtn.dataset.kbId;
      if (!confirm('确定删除此知识库及所有文档?')) return;
      _busy.set('kb-del-' + kb_id, {text: '删除中...'});
      renderKBList();
      try {
        const result = await kbRequest(`/v1/kb/${kb_id}`, {method: 'DELETE'});
        if (currentKbId === kb_id) {
          currentKbId = '';
          sessionStorage.removeItem('kb_id');
          document.getElementById('kbDocsSection').style.display = 'none';
        }
        _busy.set('kb-del-' + kb_id, {text: result.sync_pending ? '已删除，待同步' : '✓ 已删除'});
        renderKBList();
        setTimeout(() => { _busy.delete('kb-del-' + kb_id); loadKBs(); }, 800);
      } catch (err) {
        _busy.set('kb-del-' + kb_id, {text: '删除失败'});
        renderKBList();
        setTimeout(() => { _busy.delete('kb-del-' + kb_id); renderKBList(); }, 1500);
      }
      return;
    }

    if (card) {
      currentKbId = card.dataset.kbId;
      sessionStorage.setItem('kb_id', currentKbId);
      window.dispatchEvent(new CustomEvent('kb-changed', {detail: {kb_id: currentKbId}}));
      renderKBList();
      await loadDocs(currentKbId);
      document.getElementById('kbDocsSection').style.display = 'block';
      const kb = kbs.find(k => k.kb_id === currentKbId);
      document.getElementById('currentKbName').textContent = kb ? kb.name : '';
    }
  });

  document.getElementById('uploadDocBtn')?.addEventListener('click', () => {
    document.getElementById('fileInput').click();
  });

  document.getElementById('fileInput')?.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) {
      alert('文件过大,上限 50MB');
      return;
    }
    const formData = new FormData();
    formData.append('file', file);
    const uploadKbId = currentKbId;
    if (!uploadKbId) return;
    try {
      const data = await kbRequest(`/v1/kb/${uploadKbId}/docs`, {
        method: 'POST',
        body: formData
      });
      e.target.value = '';
      await loadDocs(uploadKbId);
      pollDocStatus(uploadKbId, data.doc_id);
    } catch (err) {
      alert('上传失败: ' + err.message);
    }
  });

  async function loadDocs(kb_id) {
    try {
      const loaded = await kbRequest(`/v1/kb/${kb_id}/docs`);
      if (currentKbId !== kb_id) return;
      docs = loaded;
      renderDocList();
    } catch (err) {
      console.error('加载文档失败:', err);
    }
  }

  function renderDocList() {
    const container = document.getElementById('docList');
    if (!container) return;
    container.innerHTML = docs.map(doc => {
      const b = _busy.get('doc-del-' + doc.doc_id);
      const btnText = b ? b.text : '删除';
      const btnDis = b ? 'disabled' : '';
      return `
      <div class="doc-item" data-doc-id="${doc.doc_id}">
        <span class="doc-name">${escapeHtml(doc.filename)}</span>
        <span class="doc-status ${doc.status}">${doc.status === 'pending' ? '⏳ 索引中' : doc.sync_pending ? (doc.status === 'failed' ? '❌ 索引失败，待清理' : '待同步') : statusIcon(doc.status)}</span>
        <span class="doc-error">${escapeHtml(doc.sync_error || doc.error_msg || '')}</span>
        <span class="doc-size">${(doc.file_bytes / 1024).toFixed(1)} KB</span>
        <button class="doc-delete" data-doc-id="${doc.doc_id}" ${btnDis}>${btnText}</button>
      </div>`;
    }).join('');
  }

  function statusIcon(status) {
    if (status === 'pending') return '⏳';
    if (status === 'indexed') return '✅';
    if (status === 'failed') return '❌';
    return status;
  }

  async function pollDocStatus(kb_id, doc_id) {
    const maxRetries = 60;
    for (let i = 0; i < maxRetries; i++) {
      await new Promise(r => setTimeout(r, 2000));
      try {
        const data = await kbRequest(`/v1/kb/${kb_id}/docs/${doc_id}/status`);
        if (data.status === 'not_found') {
          await loadDocs(kb_id);
          break;
        }
        if (currentKbId === kb_id) {
          docs = docs.map(doc => doc.doc_id === doc_id ? {...doc, ...data} : doc);
          renderDocList();
        }
        if ((data.status === 'indexed' || data.status === 'failed') && !data.sync_pending) {
          await loadDocs(kb_id);
          break;
        }
      } catch (err) {
        console.error('轮询状态失败:', err);
        break;
      }
    }
  }

  document.getElementById('docList')?.addEventListener('click', async (e) => {
    const delBtn = e.target.closest('.doc-delete');
    if (delBtn) {
      e.stopPropagation();
      const doc_id = delBtn.dataset.docId;
      const kb_id = currentKbId;
      if (!confirm('确定删除此文档?')) return;
      _busy.set('doc-del-' + doc_id, {text: '删除中...'});
      renderDocList();
      try {
        const result = await kbRequest(`/v1/kb/${kb_id}/docs/${doc_id}`, {method: 'DELETE'});
        _busy.set('doc-del-' + doc_id, {text: result.sync_pending ? '已删除，待同步' : '✓ 已删除'});
        renderDocList();
        setTimeout(() => { _busy.delete('doc-del-' + doc_id); loadDocs(kb_id); }, 800);
      } catch (err) {
        _busy.set('doc-del-' + doc_id, {text: '删除失败'});
        renderDocList();
        setTimeout(() => { _busy.delete('doc-del-' + doc_id); renderDocList(); }, 1500);
      }
    }
  });

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // ─── 回收站 ─────────────────────────────────────────────────────

  async function loadTrash() {
    try {
      const res = await fetch(`${API_BASE}/v1/kb/trash`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      trashKbs = await res.json();
      renderTrashList();
      return true;
    } catch (err) {
      console.error('加载回收站失败:', err);
      return false;
    }
  }

  function renderTrashList() {
    const container = document.getElementById('kbTrashList');
    const empty = document.getElementById('kbTrashEmpty');
    if (!container) return;
    if (!trashKbs.length) {
      container.innerHTML = '';
      container.style.display = 'none';
      if (empty) empty.style.display = 'block';
      return;
    }
    if (empty) empty.style.display = 'none';
    container.style.display = '';
    container.innerHTML = trashKbs.map(kb => {
      const br = _busy.get('kb-restore-' + kb.kb_id);
      const bh = _busy.get('kb-hard-' + kb.kb_id);
      const restText = br ? br.text : '还原';
      const restDis = br ? 'disabled' : '';
      const hardText = bh ? bh.text : '彻底删除';
      const hardDis = bh ? 'disabled' : '';
      return `
      <div class="kb-card trash" data-kb-id="${kb.kb_id}">
        <div class="kb-card-name">${escapeHtml(kb.name)}</div>
        <div class="kb-card-desc">${escapeHtml(kb.description || '')}</div>
        <div class="kb-trash-meta">${kb.doc_count} 篇文档 · 删除于 ${kb.deleted_at.slice(0, 10)}</div>
        <div class="kb-sync-state">${escapeHtml(kb.sync_action ? '待同步：' + kb.sync_action : '')} ${escapeHtml(kb.sync_error || '')}</div>
        <div class="kb-card-actions">
          <button class="kb-card-restore" data-kb-id="${kb.kb_id}" data-kb-name="${escapeHtml(kb.name)}" ${restDis}>${restText}</button>
          <button class="kb-card-hard-delete" data-kb-id="${kb.kb_id}" data-kb-name="${escapeHtml(kb.name)}" ${hardDis}>${hardText}</button>
        </div>
      </div>`;
    }).join('');
  }

  function enterTrash() {
    viewMode = 'trash';
    document.getElementById('kbList').style.display = 'none';
    document.getElementById('kbDocsSection').style.display = 'none';
    document.getElementById('kbHeaderTitle').textContent = '回收站';
    document.getElementById('createKbBtn').style.display = 'none';
    document.getElementById('kbTrashBtn').textContent = '← 返回';
    loadTrash();
  }

  function exitTrash() {
    viewMode = 'active';
    document.getElementById('kbTrashList').style.display = 'none';
    const empty = document.getElementById('kbTrashEmpty');
    if (empty) empty.style.display = 'none';
    document.getElementById('kbList').style.display = '';
    document.getElementById('kbHeaderTitle').textContent = '我的知识库';
    document.getElementById('createKbBtn').style.display = '';
    document.getElementById('kbTrashBtn').textContent = '🗑 回收站';
    loadKBs();
  }

  document.getElementById('kbTrashBtn')?.addEventListener('click', () => {
    viewMode === 'active' ? enterTrash() : exitTrash();
  });

  document.getElementById('kbTrashList')?.addEventListener('click', async (e) => {
    const restoreBtn = e.target.closest('.kb-card-restore');
    const hardDelBtn = e.target.closest('.kb-card-hard-delete');

    if (restoreBtn) {
      e.stopPropagation();
      const kb_id = restoreBtn.dataset.kbId;
      if (!confirm('还原此知识库及其文档?')) return;
      _busy.set('kb-restore-' + kb_id, {text: '还原中...'});
      renderTrashList();
      try {
        const result = await kbRequest(`/v1/kb/${kb_id}/restore`, {method: 'POST'});
        if (result.sync_pending) throw new Error('恢复仍在同步中');
        _busy.set('kb-restore-' + kb_id, {text: '✓ 已还原'});
        renderTrashList();
        setTimeout(() => { _busy.delete('kb-restore-' + kb_id); loadTrash(); }, 800);
      } catch (err) {
        _busy.set('kb-restore-' + kb_id, {text: '还原未完成，重试'});
        renderTrashList();
        setTimeout(() => { _busy.delete('kb-restore-' + kb_id); loadTrash(); }, 1500);
      }
      return;
    }

    if (hardDelBtn) {
      e.stopPropagation();
      const kb_id = hardDelBtn.dataset.kbId;
      const kb_name = hardDelBtn.dataset.kbName;
      if (!confirm('⚠️ 此操作不可逆，将永久删除该知识库及所有文档和向量数据！')) return;
      const input = prompt(`请输入知识库名字「${kb_name}」以确认删除:`);
      if (input !== kb_name) {
        alert('名字不匹配,取消删除');
        return;
      }
      _busy.set('kb-hard-' + kb_id, {text: '删除中...'});
      renderTrashList();
      try {
        await kbRequest(`/v1/kb/${kb_id}/hard`, {method: 'DELETE'});
        _busy.set('kb-hard-' + kb_id, {text: '✓ 已删除'});
        renderTrashList();
        setTimeout(() => { _busy.delete('kb-hard-' + kb_id); loadTrash(); }, 800);
      } catch (err) {
        _busy.set('kb-hard-' + kb_id, {text: '删除失败'});
        renderTrashList();
        setTimeout(() => { _busy.delete('kb-hard-' + kb_id); renderTrashList(); }, 1500);
      }
      return;
    }
  });

  loadKBs();

  window.getCurrentKbId = () => currentKbId;
})();
