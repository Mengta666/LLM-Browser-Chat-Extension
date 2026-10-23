document.addEventListener('DOMContentLoaded', /** 侧边栏文档就绪后初始化聊天、设置、记忆和自动化界面，并恢复已保存会话。 */ async () => {
  let attachedImage = null;
  let chatImage = null;
  let chatImageProcessing = false;
  let chatAttachmentEpoch = 0;
  let chatUpload = null;
  const chatImageViews = new Set();
  let currentChatId = '';
  const sessionSequences = new Map();
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
  const MAX_IMAGE_PIXELS = 20_000_000;
  const MAX_URL_LENGTH = 2048;
  const PRIVACY_NOTICE_KEY = 'privacyNoticeAccepted';
  const CURRENT_CHAT_ID_KEY = 'currentChatId';
  const PAGE_REFRESH_ENDPOINT_PATH = '/api/pages/refresh_snapshot';
  const ALLOWED_IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  const DEFAULT_API_BASE_URLS = new Set([
    DEFAULT_BACKEND_API_URL
  ]);
  const DEFAULT_API_URL = DEFAULT_BACKEND_API_URL;

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
        const tab = Array.isArray(tabs) ? tabs.find(/** 从查询结果中选择具有有效标识的标签页。 */ (candidate) => candidate?.id) : null;
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
    window.DOMPurify.addHook('afterSanitizeAttributes', /** 为清理后的外部链接设置新窗口打开及来源隔离属性。 */ (node) => {
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
    el.querySelectorAll('pre').forEach(/** 为尚未包装的代码块创建复制按钮。 */ (pre) => {
      if (pre.parentElement && pre.parentElement.classList.contains('code-block')) return;
      const wrap = document.createElement('div');
      wrap.className = 'code-block';
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      const btn = document.createElement('button');
      btn.className = 'code-copy-btn';
      btn.type = 'button';
      btn.textContent = '复制';
      btn.addEventListener('click', /** 响应复制操作，将代码正文写入剪贴板。 */ () => {
        const code = pre.innerText;
        navigator.clipboard.writeText(code).then(/** 显示复制成功状态，并安排恢复按钮文本。 */ () => {
          btn.textContent = '已复制';
          setTimeout(/** 复制成功提示结束后恢复按钮文本。 */ () => { btn.textContent = '复制'; }, 1500);
        }).catch(/** 在剪贴板写入失败时显示失败提示。 */ () => { btn.textContent = '复制失败'; });
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
    let out = text.replace(/`[^`\n]+`/g, /** 保存行内代码并用占位符保护其内容。 */ (m) => {
      code.push(m); return `${PH_CODE}${code.length - 1}${PH_CODE}`;
    });
    // 已闭合的 math:$$...$$、\[...\]、\(...\)、$...$ 按此顺序抽,长的优先
    /** 保存公式正文与显示模式，返回对应占位符以避免 Markdown 改写公式。 */
    const pushMath = (raw, display, body) => {
      math.push({ display, body }); return `${PH_MATH}${math.length - 1}${PH_MATH}`;
    };
    out = out.replace(/\$\$([\s\S]+?)\$\$/g, /** 保护双美元符号包围的块级公式。 */ (m, b) => pushMath(m, true, b));
    out = out.replace(/\\\[([\s\S]+?)\\\]/g, /** 保护方括号命令包围的块级公式。 */ (m, b) => pushMath(m, true, b));
    out = out.replace(/\\\(([\s\S]+?)\\\)/g, /** 保护圆括号命令包围的行内公式。 */ (m, b) => pushMath(m, false, b));
    // 单 $:同行内、非空、且 $ 前不是数字/字母/反斜杠(避免 $100、a$b、\$)
    out = out.replace(/(^|[^\\\w])\$([^\n$]+?)\$(?!\d)/g, /** 保护单美元公式，并保留前面的非公式字符。 */ (m, pre, b) => pre + pushMath(m, false, b));
    return { text: out, code, math };
  }

  // 步骤 3:裸 [ ... ] 兜底(仅当独占行 + 含 LaTeX 特征)
  function convertBareBrackets(text, math) {
    // 匹配"^ [ $"独占一行,到"^ ] $"独占一行,中间任意
    return text.replace(
      /^[ \t]*\[[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*\][ \t]*$/gm,
      /** 仅将含 LaTeX 特征的独立方括号段转换为块级公式。 */ (m, body) => {
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
      out = out.replace(re, /** 将公式占位符恢复为对应显示模式的分隔符与正文。 */ (_m, i) => {
        const seg = math[Number(i)];
        if (!seg) return '';
        return seg.display ? `$$${seg.body}$$` : `$${seg.body}$`;
      });
    }
    if (code.length) {
      const re = new RegExp(`${PH_CODE}(\\d+)${PH_CODE}`, 'g');
      out = out.replace(re, /** 把行内代码占位符恢复为已转义的 code 元素。 */ (_m, i) => {
        const raw = code[Number(i)];
        if (!raw) return '';
        const node = document.createElement('code');
        node.textContent = raw.slice(1, -1);
        return node.outerHTML;
      });
    }
    return out;
  }

  /** 保护代码和公式片段、修正规范外的公式分隔，再转义货币符号，供 Markdown 渲染使用。 */
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
    /** 在动画帧中渲染最新待展示文本，并清除当前帧标识。 */
    const flush = () => {
      frame = 0;
      renderMarkdownInto(el, pending, { streaming: true });
    };
    return {
      /** 保存最新流式文本；尚未预约渲染时安排下一动画帧更新。 */
      update(text) {
        pending = text;
        if (!frame) frame = requestAnimationFrame(flush);
      },
      /** 取消待执行的流式渲染，用完整正文完成最终 Markdown 渲染。 */
      finalize(text) {
        if (frame) { cancelAnimationFrame(frame); frame = 0; }
        renderMarkdownInto(el, text, { streaming: false });
      },
      /** 取消尚未执行的渲染帧，保留当前已展示内容。 */
      cancel() {
        if (frame) { cancelAnimationFrame(frame); frame = 0; }
      }
    };
  }

  /** 异步解码图片，成功时返回图像元素，失败时报告解码错误。 */
  function loadImageElement(src) {
    return new Promise(/** 绑定图片解码的完成和失败事件，再设置图片来源。 */ (resolve, reject) => {
      const image = new Image();
      /** 图片解码完成后返回可绘制的图像元素。 */
      image.onload = () => resolve(image);
      /** 将图片加载错误转换为异步解码失败。 */
      image.onerror = () => reject(new Error('图片解码失败'));
      image.src = src;
    });
  }

  /** 统一图片 MIME 类型的大小写和 JPEG 别名。 */
  function normalizeImageMimeType(type) {
    const value = String(type || '').trim().toLowerCase();
    if (value === 'image/jpg') return 'image/jpeg';
    return value;
  }

  /** 判断规范化后的图片类型是否在允许列表中。 */
  function isAllowedImageMime(type) {
    return ALLOWED_IMAGE_MIME_TYPES.has(normalizeImageMimeType(type));
  }

  /** 校验图片尺寸有效且总像素数未超过限制。 */
  function assertImageDimensionsSafe(width, height) {
    if (!width || !height) {
      throw new Error('无法识别图片尺寸');
    }

    if (width * height > MAX_IMAGE_PIXELS) {
      throw new Error('图片分辨率过大');
    }
  }

  /** 校验图片类型、大小和尺寸后转为 PNG，并再次检查转换后的大小。 */
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

  /** 把字节数格式化为适合界面展示的 B、KB 或 MB。 */
  function formatFileSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  /** 根据 Base64 文本长度估算图片数据的字节数。 */
  function estimateDataUrlBytes(dataUrl) {
    const base64 = String(dataUrl || '').split(',')[1] || '';
    return Math.floor(base64.length * 0.75);
  }

  /** 优先使用随机 UUID，否则生成随机字节形式的消息标识。 */
  function createMessageId() {
    if (crypto?.randomUUID) return crypto.randomUUID();

    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, /** 将随机字节转换为两位十六进制标识片段。 */ (byte) => byte.toString(16).padStart(2, '0')).join('');
  }

  /** 生成带聊天前缀的随机会话标识。 */
  function createChatId() {
    return `chat_${createMessageId()}`;
  }

  /** 复用当前或会话存储中的聊天标识；不存在时创建并保存新标识。 */
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

  /** 清理当前附件状态，创建并保存新的聊天标识。 */
  async function resetCurrentChatId() {
    invalidateChatAttachments();
    currentChatId = createChatId();
    updateSendButton();
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: currentChatId });
    return currentChatId;
  }

  /** 将指定接口路径拼接到规范化后的后端根地址。 */
  function buildBackendEndpointUrl(apiBaseUrl, endpointPath) {
    const normalizedApiBaseUrl = normalizeApiBaseUrl(apiBaseUrl);
    const parsedUrl = new URL(normalizedApiBaseUrl);
    let basePath = parsedUrl.pathname.replace(/\/$/, '');
    if (basePath.endsWith('/v1')) {
      basePath = basePath.slice(0, -3);
    }
    return `${parsedUrl.origin}${basePath}${endpointPath}`;
  }

  /** 合并默认与用户已授权的 API 基址，规范化并过滤无效项。 */
  async function getAllowedApiBaseUrls() {
    const { [CUSTOM_API_BASE_URLS_KEY]: customUrls = [] } = await chrome.storage.local.get([CUSTOM_API_BASE_URLS_KEY]);
    return new Set([
      ...DEFAULT_API_BASE_URLS,
      ...customUrls
        .filter(/** 过滤自定义地址配置中的非字符串项。 */ (url) => typeof url === 'string')
        .map(/** 规范化自定义后端基址，无效配置返回空值。 */ (url) => {
          try {
            return normalizeApiBaseUrl(url);
          } catch {
            return '';
          }
        })
        .filter(Boolean)
    ]);
  }

  /** 将 API 地址转换为浏览器站点访问权限匹配模式。 */
  function getApiHostPermissionPattern(apiUrl) {
    const parsedUrl = new URL(apiUrl);
    return `${parsedUrl.protocol}//${parsedUrl.hostname}/*`;
  }

  /** 为自定义 API 地址申请浏览器站点权限，拒绝授权时报告错误。 */
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

  /** 让用户确认自定义后端的数据接收范围，取得站点权限后保存白名单。 */
  async function ensureCustomApiBaseUrlAllowed(apiUrl) {
    if (DEFAULT_API_BASE_URLS.has(apiUrl)) return;

    const allowedUrls = await getAllowedApiBaseUrls();
    if (allowedUrls.has(apiUrl)) return;

    const hostname = new URL(apiUrl).hostname;
    const ok = confirm([
      `确认添加自定义 API 地址：${apiUrl}`,
      '',
      `之后对话内容、图片和你输入的 API Key 会发送到 ${hostname}。`,
      '请确认这是你信任的 Browser Agent 后端，不是上游模型接口。'
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

  /** 校验后端基址已获授权，并要求远程后端具有访问凭证。 */
  async function validateBackendApiConfig(apiUrl, apiKey) {
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

    return normalizedApiUrl;
  }

  /** 加载模型和后端配置，校验凭证与后端地址的绑定后返回请求配置。 */
  async function resolveApiRequestConfig() {
    const { apiUrl, modelName } = await chrome.storage.local.get(['apiUrl', 'modelName']);
    const { apiKey, apiKeyApiUrl } = await getStoredApiCredential();
    const safeApiUrl = await validateBackendApiConfig(apiUrl || DEFAULT_API_URL, apiKey);
    const shouldEnforceApiKeyBinding = Boolean(String(apiKey || '').trim());

    if (shouldEnforceApiKeyBinding && apiKeyApiUrl && apiKeyApiUrl !== safeApiUrl) {
      throw new Error('当前 API Key 与 API 地址不匹配，请在设置中重新保存配置');
    }

    return {
      apiKey,
      modelName,
      safeApiUrl
    };
  }

  /** 读取会话凭证，并将旧本地凭证迁移到会话存储，保留原服务绑定。 */
  async function getStoredApiCredential() {
    const [{ apiKey: sessionApiKey, apiKeyApiUrl }, { apiKey: localApiKey }] = await Promise.all([
      chrome.storage.session.get(['apiKey', 'apiKeyApiUrl']),
      chrome.storage.local.get(['apiKey'])
    ]);

    if (localApiKey) {
      if (!sessionApiKey) {
        // 旧凭证保留原服务绑定，不能随默认后端变更而转发到新地址。
        await chrome.storage.session.set({ apiKey: localApiKey, apiKeyApiUrl: 'https://api.openai.com/v1' });
      }
      await chrome.storage.local.remove(['apiKey']);
      return {
        apiKey: sessionApiKey || localApiKey,
        apiKeyApiUrl: apiKeyApiUrl || 'https://api.openai.com/v1'
      };
    }

    return {
      apiKey: sessionApiKey || '',
      apiKeyApiUrl: apiKeyApiUrl || ''
    };
  }

  /** 通过扩展后台发送受限 JSON 请求，并将通信或接口错误传回调用方。 */
  function callApiJson(url, options) {
    return new Promise(/** 将扩展后台 JSON 消息请求包装为异步结果。 */ (resolve, reject) => {
      chrome.runtime.sendMessage(
        {
          type: 'CALL_API_JSON',
          url,
          options
        },
        /** 检查扩展通信与后端响应状态，再返回 JSON 正文。 */ (response) => {
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
  async function callBackendApi(url, method = 'GET', body = null) {
    const { apiKey, safeApiUrl } = await resolveApiRequestConfig();
    if (!url.startsWith(buildBackendEndpointUrl(safeApiUrl, '/'))) {
      throw new Error('后端地址已改变，请切回原后端后操作');
    }
    const options = { method, headers: { 'Content-Type': 'application/json' } };
    if (apiKey) options.headers.Authorization = `Bearer ${apiKey}`;
    if (body != null) options.body = JSON.stringify(body);
    return new Promise(/** 将通用后端消息请求包装为异步结果。 */ (resolve, reject) => {
      chrome.runtime.sendMessage(
        { type: 'CALL_BACKEND_API', url, options },
        /** 将通用后端响应转换为正文或带状态码的异常。 */ (response) => {
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

  /** 校验图片 data URL 的格式、类型及估算字节数。 */
  function isAllowedDataImageUrl(value) {
    const text = String(value || '').trim();
    const match = text.match(/^data:(image\/(?:png|jpe?g|webp|gif));base64,([A-Za-z0-9+/=]+)$/i);
    if (!match) return false;
    if (!isAllowedImageMime(match[1])) return false;

    const estimatedBytes = Math.floor(match[2].length * 0.75);
    return estimatedBytes <= MAX_IMAGE_BYTES;
  }

  /** 解析长度受限且不含内嵌凭证的 HTTP 或 HTTPS 图片地址。 */
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

  /** 判断图片地址是否通过 HTTP 地址格式校验。 */
  function isAllowedImageHttpUrl(value) {
    return Boolean(parseImageHttpUrl(value));
  }

  /** 将外部资源地址转换为浏览器站点权限匹配模式。 */
  function getHostPermissionPattern(value) {
    const parsed = new URL(String(value || '').trim());
    return `${parsed.protocol}//${parsed.hostname}/*`;
  }

  /** 申请访问图片所在站点的运行时权限。 */
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

  /** 校验图片地址；对 HTTP、本地或内网来源显示确认提示。 */
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

  /** 检查并按需申请读取网页所需的站点权限，返回授权结果。 */
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

  /** 首次使用时展示数据发送说明，并保存用户确认结果。 */
  async function ensurePrivacyNoticeAccepted() {
    const { [PRIVACY_NOTICE_KEY]: accepted } = await chrome.storage.local.get([PRIVACY_NOTICE_KEY]);
    if (accepted) return true;

    const ok = confirm([
      '首次使用前请确认：',
      '',
      '1. 你的输入、主动选择的网页文本和上传图片会发送到本项目后端，由后端调用模型服务。',
      '2. API Key 仅保存在当前浏览器会话中，重启浏览器后可能需要重新输入。',
      '3. 扩展不会在后台持续读取网页内容，也不会自动发送网页内容。'
    ].join('\n'));

    if (ok) {
      await chrome.storage.local.set({ [PRIVACY_NOTICE_KEY]: true });
    }
    return ok;
  }

  /** 使用 FileReader 异步将二进制图片读取为 data URL。 */
  function readBlobAsDataUrl(blob) {
    return new Promise(/** 绑定文件读取回调后启动图片 data URL 读取。 */ (resolve, reject) => {
      const reader = new FileReader();
      /** 读取完成后返回字符串形式的图片 data URL。 */
      reader.onload = () => resolve(String(reader.result || ''));
      /** 把文件读取失败传递给等待图片数据的调用方。 */
      reader.onerror = () => reject(reader.error || new Error('读取图片失败'));
      reader.readAsDataURL(blob);
    });
  }

  /** 从图片来源 URL 的末尾路径推导名称，无法推导时使用默认名称。 */
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

  /** 校验导入图片的 data URL 并转为 PNG，返回可附加的图片描述。 */
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

  /** 更新自动化任务的待发送图片，并刷新输入区预览。 */
  function applyAttachedImage(image) {
    attachedImage = image;
    renderComposerImage();
  }

  /** 按聊天或自动化模式展示对应附件，更新尺寸、上传状态和清除按钮。 */
  function renderComposerImage() {
    const useAgent = shouldUseAgent(document.getElementById('chatInput').value || '');
    const image = useAgent ? attachedImage : chatImage;
    const imagePreview = document.getElementById('imagePreview');
    const imagePreviewImg = document.getElementById('imagePreviewImg');
    const imagePreviewName = document.getElementById('imagePreviewName');
    const imagePreviewSize = document.getElementById('imagePreviewSize');
    const clearImageBtn = document.getElementById('clearImageBtn');

    if (!image) {
      imagePreview.classList.add('hidden');
      imagePreviewImg.removeAttribute('src');
      clearImageBtn.hidden = true;
      return;
    }
    imagePreviewImg.src = useAgent ? image.dataUrl : image.previewUrl;
    imagePreviewName.textContent = image.name || 'image';
    imagePreviewSize.textContent = useAgent ? `${image.type || 'image'} · ${formatFileSize(image.size || 0)}`
      : `${image.mime} · ${ChatImages.format(image.size)} · ${image.width} × ${image.height}${image.first_frame ? ' · 仅发送第一帧' : ''}${chatUpload ? ' · 上传中…' : chatImageProcessing ? ' · 处理中…' : ''}`;
    imagePreview.classList.remove('hidden');
    clearImageBtn.hidden = false;
  }

  /** 清除自动化附件及文件输入，刷新当前模式的预览。 */
  function clearAttachedImage() {
    attachedImage = null;
    const imageInput = document.getElementById('imageInput');
    const imagePreview = document.getElementById('imagePreview');
    const clearImageBtn = document.getElementById('clearImageBtn');

    imageInput.value = '';
    imagePreview.classList.add('hidden');
    clearImageBtn.hidden = true;
    renderComposerImage();
  }

  /** 取消当前聊天图片的上传请求。 */
  function cancelChatUpload() {
    if (chatUpload) chatUpload.controller.abort();
  }

  /** 使旧图片处理结果失效，取消上传并释放预览地址，再更新输入区状态。 */
  function clearChatImage() {
    chatAttachmentEpoch++;
    chatImageProcessing = false;
    cancelChatUpload();
    if (chatImage?.previewUrl) URL.revokeObjectURL(chatImage.previewUrl);
    chatImage = null;
    document.getElementById('imageInput').value = '';
    renderComposerImage();
    updateSendButton();
  }

  /** 取消历史图片加载、解除可见性观察，并释放预览地址和放大弹窗。 */
  function clearChatImageViews() {
    for (const view of chatImageViews) {
      view.controller.abort();
      view.observer?.disconnect();
      if (view.url) URL.revokeObjectURL(view.url);
    }
    chatImageViews.clear();
    document.querySelectorAll('.chat-image-dialog').forEach(/** 关闭所有仍打开的聊天图片弹窗。 */ dialog => dialog.close());
  }

  /** 清理输入区与历史消息中的所有聊天附件展示资源。 */
  function invalidateChatAttachments() {
    clearChatImage();
    clearChatImageViews();
  }

  /** 准备单张聊天图片，并用附件版本号防止迟到结果覆盖新选择。 */
  async function selectChatImage(files) {
    if (files.length !== 1) throw new Error('第一批每条消息只支持一张图片，请一次选择一张。');
    const epoch = ++chatAttachmentEpoch;
    cancelChatUpload();
    chatImageProcessing = true;
    renderComposerImage();
    updateSendButton();
    try {
      const image = await ChatImages.prepare(files[0]);
      if (epoch !== chatAttachmentEpoch) { URL.revokeObjectURL(image.previewUrl); return; }
      if (chatImage?.previewUrl) URL.revokeObjectURL(chatImage.previewUrl);
      chatImage = image;
    } finally {
      if (epoch === chatAttachmentEpoch) {
        chatImageProcessing = false;
        renderComposerImage();
        updateSendButton();
      }
    }
  }

  /** 创建聊天附件卡片，支持按需加载、放大以及原会话内再次引用。 */
  function renderChatAttachment(container, attachment, base, chatId, localImage = null) {
    const card = document.createElement('div'); card.className = 'chat-attachment-card';
    const img = document.createElement('img'); img.alt = attachment.name || '聊天图片'; img.hidden = true;
    const meta = document.createElement('div'); meta.className = 'chat-attachment-meta';
    meta.textContent = `${attachment.name || '图片'} · ${ChatImages.format(attachment.size)} · ${attachment.width} × ${attachment.height}${attachment.first_frame ? ' · 仅发送第一帧' : ''}`;
    const status = document.createElement('span'); status.textContent = '图片待加载';
    const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '加载图片';
    const reuse = document.createElement('button'); reuse.type = 'button'; reuse.textContent = '再次引用';
    card.append(img, meta, status, retry, reuse); container.appendChild(card);
    const view = { controller: new AbortController(), url: '', observer: null };
    chatImageViews.add(view);
    /** 确认后端与会话未切换后加载附件预览，丢弃已失效卡片的加载结果。 */
    const load = async () => {
      retry.disabled = true; status.textContent = '图片加载中…';
      try {
        const settings = await resolveApiRequestConfig();
        if (settings.safeApiUrl !== base || currentChatId !== chatId) throw new Error('请在原后端和原会话查看图片');
        const url = localImage?.blob ? URL.createObjectURL(localImage.blob)
          : await ChatImages.preview(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/attachments/${attachment.attachment_id}/access`),
              settings.apiKey, /** 将后端返回的受限附件路径拼接到原后端地址。 */ path => buildBackendEndpointUrl(base, path), view.controller.signal);
        if (!card.isConnected || view.controller.signal.aborted) { URL.revokeObjectURL(url); return; }
        if (view.url) URL.revokeObjectURL(view.url);
        view.url = url; img.src = url; img.hidden = false; status.textContent = ''; retry.hidden = true;
      } catch (error) {
        if (!view.controller.signal.aborted) { status.textContent = error.message || '图片不可用'; retry.textContent = '重试加载'; }
      } finally { retry.disabled = false; }
    };
    retry.addEventListener('click', load);
    img.addEventListener('click', /** 打开当前附件的大图预览。 */ () => ChatImages.enlarge(view.url));
    reuse.addEventListener('click', /** 在空闲聊天状态下重新引用原会话附件，并丢弃切换后的迟到结果。 */ async () => {
      if (chatUpload || _sendingLock || agentState.active || shouldUseAgent(document.getElementById('chatInput').value)) {
        alert('请在空闲的 Chat 模式下再次引用图片。'); return;
      }
      const epoch = ++chatAttachmentEpoch;
      try {
        const settings = await resolveApiRequestConfig();
        if (settings.safeApiUrl !== base || currentChatId !== chatId) throw new Error('只能在原后端、原会话再次引用');
        const previewUrl = await ChatImages.preview(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/attachments/${attachment.attachment_id}/access`),
          settings.apiKey, /** 为再次引用图片构造原后端的内容下载地址。 */ path => buildBackendEndpointUrl(base, path), view.controller.signal);
        if (epoch !== chatAttachmentEpoch || currentChatId !== chatId) { URL.revokeObjectURL(previewUrl); return; }
        if (chatImage?.previewUrl) URL.revokeObjectURL(chatImage.previewUrl);
        chatImage = { ...attachment, previewUrl, uploaded: attachment, base, chatId };
        renderComposerImage();
      } catch (error) { if (!view.controller.signal.aborted) alert(error.message); }
    });
    view.observer = new IntersectionObserver(/** 附件卡片进入可见区域后停止观察并加载图片。 */ entries => {
      if (entries.some(/** 判断是否有卡片进入观察区域。 */ entry => entry.isIntersecting)) { view.observer.disconnect(); load(); }
    }, { root: document.getElementById('chatHistory'), rootMargin: '200px' });
    view.observer.observe(card);
  }

  /** 校验用户选择的图片文件，转换为 PNG 后附加到自动化输入区。 */
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

  /** 导入并规范化来源图片，同时更新自动化附件预览。 */
  async function attachImageFromSource(image) {
    const loadedImage = await loadImageFromSource(image);
    applyAttachedImage(loadedImage);
    return loadedImage;
  }

  /** 同步侧边栏标签按钮与对应内容区域的选中状态。 */
  function activateTab(target) {
    document.querySelectorAll('.tab-btn').forEach(/** 切换标签按钮的选中样式。 */ (button) => {
      button.classList.toggle('active', button.dataset.target === target);
    });

    document.querySelectorAll('.tab-content').forEach(/** 切换内容面板的显示状态。 */ (content) => {
      content.classList.toggle('active', content.id === target);
    });
  }

  // 1. Tab 切换逻辑
  document.querySelectorAll('.tab-btn').forEach(/** 为每个侧边栏标签按钮绑定切换行为。 */ btn => {
    btn.addEventListener('click', /** 切换内容面板，进入记忆页时恢复按钮状态并刷新列表。 */ (e) => {
      const target = e.currentTarget.dataset.target;
      activateTab(target);
      // 记忆 tab:切回时刷新列表;modal 只在 idle 时才隐藏(running/done 保持显示)
      if (target === 'memory') {
        const modal = document.getElementById('rethinkModal');
        if (modal && _rethinkState === 'idle') modal.style.display = 'none';
        renderAddMemoryBtn();   // 恢复添加按钮状态(添加中/已添加/idle)
        loadMemoryPanel().catch(/** 记录记忆面板加载失败。 */ (err) => console.warn('加载记忆面板失败', err));
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

  /** 显示凭证是否已保存及其绑定地址，清空输入框以避免展示明文。 */
  function updateApiKeyStatus(hasKey, apiKeyUrl = '') {
    apiKeyInput.value = '';
    apiKeyInput.placeholder = hasKey ? '已保存 API Key，留空则保留' : '本机 / 内网后端可留空';
    if (apiKeyStatus) {
      apiKeyStatus.textContent = hasKey
        ? `已保存 API Key；页面不会显示明文。适用地址：${apiKeyUrl || '当前 API 地址'}`
        : '尚未保存 API Key。';
    }
  }

  /** 显示设置操作结果，并在短暂延迟后清除提示。 */
  function showSettingsMessage(text, variant) {
    saveMsg.textContent = text;
    saveMsg.className = variant === 'error' ? 'settings-message-error' : 'settings-message-ok';
    setTimeout(/** 在设置提示展示结束后清空内容和样式。 */ () => {
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

  document.getElementById('saveSettingsBtn').addEventListener('click', /** 校验后端地址、模型参数和凭证绑定，再保存设置并更新状态提示。 */ async () => {
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
      if (!enteredApiKey && effectiveApiKey && savedCredential.apiKeyApiUrl && savedCredential.apiKeyApiUrl !== safeApiUrl) {
        throw new Error('切换后端地址时请重新输入访问凭证，或先清除旧凭证');
      }
      if (!isLocalApi && !DEFAULT_API_BASE_URLS.has(safeApiUrl) && !enteredApiKey && savedCredential.apiKeyApiUrl !== safeApiUrl) {
        throw new Error('添加自定义 API 地址时请同时输入该服务对应的 API Key');
      }
      await ensureCustomApiBaseUrlAllowed(safeApiUrl);
      safeApiUrl = await validateBackendApiConfig(safeApiUrl, effectiveApiKey);
    } catch (error) {
      showSettingsMessage(error.message || '配置无效', 'error');
      return;
    }

    if (enteredApiKey) {
      await chrome.storage.session.set({ apiKey: enteredApiKey, apiKeyApiUrl: safeApiUrl });
    }

    invalidateChatAttachments();
    await chrome.storage.local.set({ apiUrl: safeApiUrl, modelName, agentLlmParams: agentLlmParamsInput.value.trim() });
    apiUrlInput.value = safeApiUrl;
    updateApiKeyStatus(Boolean(enteredApiKey || effectiveApiKey), safeApiUrl);
    showSettingsMessage('保存成功！', 'ok');
  });

  document.getElementById('clearApiKeyBtn')?.addEventListener('click', /** 确认后清除会话及旧本地存储中的凭证，并刷新界面。 */ async () => {
    if (!confirm('确认清除已保存的 API Key？')) return;
    await Promise.all([
      chrome.storage.session.remove(['apiKey', 'apiKeyApiUrl']),
      chrome.storage.local.remove(['apiKey'])
    ]);
    updateApiKeyStatus(false);
    showSettingsMessage('API Key 已清除', 'ok');
  });

  // 3. UI 辅助函数
  /** 创建指定角色的消息行，追加到聊天历史并返回正文气泡容器。 */
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

  /** 按指定滚动方式将聊天历史移动到末尾。 */
  function scrollToBottom(behavior = 'smooth') {
    const chatHistory = document.getElementById('chatHistory');
    chatHistory.scrollTo({ top: chatHistory.scrollHeight, behavior });
  }

  // 4. 发送与流式接收核心逻辑
  let _sendingLock = false;
  let _chatRequest = null;

  /** 根据自动化、上传和聊天请求状态切换发送或停止按钮及可用状态。 */
  function updateSendButton() {
    const button = document.getElementById('sendBtn');
    if (!button) return;
    if (agentState.active) {
      button.textContent = agentState.status === 'stopping' ? '正在停止…' : '■ 停止';
      button.title = '停止当前自动化；已发出的操作不会回滚';
      button.disabled = agentState.status === 'stopping';
      return;
    }
    if (chatUpload?.chatId === currentChatId) {
      button.textContent = chatUpload.controller.signal.aborted ? '正在停止…' : '■ 停止';
      button.title = '停止图片上传，不提交聊天';
      button.disabled = chatUpload.controller.signal.aborted;
      return;
    }
    const request = _chatRequest?.chatId === currentChatId ? _chatRequest : null;
    const active = request && (request.status === 'running' || request.stopping);
    button.textContent = active ? (request.stopping ? '正在停止…' : '■ 停止') : '发送';
    button.title = active ? '停止当前提问，不删除会话；已完成的压缩进度会保留' : '发送消息';
    button.disabled = active ? request.stopping : (_sendingLock || agentState.active || chatImageProcessing);
  }

  /** 登记或复用当前聊天请求的界面状态，避免替换仍活动或正在停止的请求。 */
  function bindChatRequest(base, chatId, requestId, onSettled, live = false) {
    if (_chatRequest?.base === base && _chatRequest.chatId === chatId && _chatRequest.requestId === requestId) {
      return _chatRequest;
    }
    if (_chatRequest?.live || _chatRequest?.stopping) return null;
    _chatRequest = { base, chatId, requestId, onSettled, live, status: 'running', stopping: false };
    updateSendButton();
    return _chatRequest;
  }

  /** 记录请求结束状态并解除界面占用，刷新发送按钮。 */
  function releaseChatRequest(request, status) {
    if (!request) return;
    request.status = status;
    if (_chatRequest === request && !request.stopping) _chatRequest = null;
    updateSendButton();
  }

  /** 请求后端取消指定提问，响应丢失时查询终态，并更新会话序号。 */
  async function cancelChatRequest(base, chatId, requestId) {
    const endpoint = buildBackendEndpointUrl(base,
      `/v1/sessions/${encodeURIComponent(chatId)}/requests/${encodeURIComponent(requestId)}`);
    let state;
    try {
      state = await callBackendApi(`${endpoint}/cancel`, 'POST', {});
    } catch (error) {
      // 取消可能已生效但响应丢失，也可能正常完成先于取消到达。
      try { state = await callBackendApi(endpoint); } catch { throw error; }
      if (!['cancelled', 'completed', 'partial'].includes(state.status)) throw error;
    }
    if (!['cancelled', 'completed', 'partial'].includes(state.status)) throw new Error('后端尚未确认停止');
    sessionSequences.set(`${base}|${chatId}`, state.last_seq);
    return state;
  }

  /** 停止当前会话的图片上传或聊天请求，确认后端终态后刷新界面。 */
  async function stopChatRequest() {
    if (chatUpload?.chatId === currentChatId) { cancelChatUpload(); updateSendButton(); return; }
    const request = _chatRequest;
    if (!request || request.chatId !== currentChatId || request.stopping || request.status !== 'running') return;
    request.stopping = true;
    updateSendButton();
    try {
      const state = await cancelChatRequest(request.base, request.chatId, request.requestId);
      request.status = state.status;
      await request.onSettled(state);
    } catch (error) {
      alert('停止未确认，请稍后重试：' + (error.message || error));
    } finally {
      request.stopping = false;
      if (request.status !== 'running') releaseChatRequest(request, request.status);
      else updateSendButton();
    }
  }

  // 发送分流：自动化开关开(或 /browser-operation 前缀)→ 启动 agent；否则走服务端聊天。
  async function handleSend() {
    if (chatImageProcessing || chatUpload) return;
    if (_sendingLock || (_chatRequest?.chatId === currentChatId &&
        (_chatRequest.status === 'running' || _chatRequest.stopping))) return;
    if (agentState.active) return;
    const input = document.getElementById('chatInput');
    const text = (input.value || '').trim();
    const useAgent = shouldUseAgent(text);
    if (!text && (useAgent || !chatImage)) return;
    if (useAgent && text.length > MAX_AGENT_TASK_LENGTH) {
      alert(`自动化指令不能超过 ${MAX_AGENT_TASK_LENGTH} 字。`);
      return;
    }

    _sendingLock = true;
    updateSendButton();
    try {
      const image = useAgent ? (attachedImage ? attachedImage.dataUrl : '') : chatImage;
      if (useAgent) {
        // 自动化模式：去掉命令前缀,启动 agent,图片作视觉输入
        const task = text.startsWith(AGENT_COMMAND.trim())
          ? text.slice(AGENT_COMMAND.trim().length).trim()
          : text;
        input.value = '';
        if (attachedImage) clearAttachedImage();
        await runAgentTask(task, image);
      } else {
        // search_query 非空表示本轮要求联网；后端结合会话生成实际搜索词。
        const searchQuery = isSearchMode() ? text : '';
        await runChat(text, image, searchQuery);
      }
    } finally {
      _sendingLock = false;
      updateSendButton();
    }
  }

  /** 固定会话与附件版本，校验后端及隐私确认后启动服务端聊天。 */
  async function runChat(text, image, searchQuery = '') {
    const epoch = chatAttachmentEpoch;
    const chatId = await getOrCreateCurrentChatId();
    let settings;
    try {
      settings = await resolveApiRequestConfig();
    } catch (error) {
      alert(error.message || '后端配置无效');
      return;
    }
    if (!(await ensurePrivacyNoticeAccepted())) return;
    if (epoch !== chatAttachmentEpoch || currentChatId !== chatId) return;
    await runServerChat(text, image, searchQuery, settings);
  }

  /** 探测后端会话、后台任务及事件流能力，拒绝不兼容的接口。 */
  async function requireServerContext(base) {
    let data;
    try {
      data = await callBackendApi(buildBackendEndpointUrl(base, '/v1/sessions/capabilities'));
    } catch (error) {
      throw new Error(error.status === 404
        ? '当前地址未提供 Browser Agent 会话接口，请在设置中填写本项目后端地址，而不是模型接口。'
        : '无法连接或验证 Browser Agent 后端，请检查后端是否启动、地址和访问凭证是否正确。');
    }
    if (data?.server_context !== true || data.protocol_version !== 1) {
      throw new Error('后端会话协议不兼容，请更新本项目后端；不再支持直接连接模型接口。');
    }
    if (data.chat_background_tasks_version !== 1 || data.chat_event_stream_version !== 1) {
      throw new Error('请重启已更新的后端，以启用聊天断线续接。');
    }
    return data;
  }

  /** 将历史压缩阶段、进度和错误码转换为用户可读的状态说明。 */
  function compactionLabel(progress) {
    const count = `${progress.next_batch}/${progress.total_batches}`;
    if (progress.phase === 'generating') return '历史上下文整理完成，正在回答原问题…';
    if (progress.status === 'completed') return '历史上下文整理完成';
    if (progress.status === 'cancelled') return '本轮已取消，历史和压缩检查点仍保留';
    const errors = {
      compaction_output_truncated: '状态提取被输出上限截断，请检查辅助模型的输出配置',
      compaction_configuration_error: '摘要模型或预算配置不可用，请检查配置后恢复',
      compaction_timeout: '压缩阶段超时',
      compaction_summary_too_large: '摘要精简后仍超过目标预算',
      compaction_insufficient_space: '摘要完成后空间仍不足',
      compaction_input_budget_exceeded: '摘要请求超过输入预算，请检查配置',
      compaction_patch_invalid: '状态变更格式或更新规则未通过检查，可恢复原问题重新提取',
      compaction_source_invalid: '状态变更的原文来源未通过检查，可恢复原问题重新提取',
      compaction_state_invalid: '已保存的状态格式异常，需要检查历史存储',
      compaction_state_budget_exceeded: '必须保留的任务或约束超过状态预算，请调整预算后恢复',
      compaction_replan_exhausted: '本次拆分尝试已用尽，失败批次已保留，请检查输出配置后恢复',
      compaction_legacy_history_missing: '旧摘要对应的原文不完整，未切换会话状态格式',
      backend_restarted: '后端已重启',
    };
    if (progress.status !== 'running') {
      return `历史压缩未完成（${count} 批），进度已保留。${errors[progress.error_code] || '压缩已中断或失败'}；当前问题尚未继续回答。`;
    }
    const phase = { extract: '提取有来源的状态变更', repair: '修复状态变更格式或来源',
      replan: '重新划分失败批次', render: '整理预算内的会话状态', validate: '检查完整上下文预算', reduce: '精简旧摘要' }[progress.phase];
    return `正在整理历史上下文：已完成 ${count} 批${phase ? `，正在${phase}` : ''}。完成后自动继续回答。`;
  }

  /** 展示全文分析状态与已处理原文范围，区分辅助分析和主模型阅读。 */
  function documentAnalysisLabel(progress) {
    const end = progress?.processed_range?.[1] || 0;
    const total = progress?.saved_chars || 0;
    const status = { running: '正在分片分析', completed: '全文处理完成（结论仍需核实）',
      paused: '本次分析额度已用尽，请明确回复“继续全文分析”', interrupted: '全文分析已中断，不会自动继续',
      continued: '此任务已由后续提问继续，以下为任务最新处理范围',
      failed: '全文分析失败，可检查后明确继续' }[progress?.status] || '全文分析状态待确认';
    return `${status}：原文 ${progress?.source_ref || ''} 已保存 ${total} 字符，辅助分析已处理 [0,${end})；调用 ${progress?.calls || 0}/16。不是主模型逐字已读。`;
  }

  /** 为保留的请求添加取消按钮，并在后端确认后通知调用方。 */
  function addRequestCancel(container, base, chatId, requestId, onCancelled) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'tool-btn'; button.textContent = '结束本次提问';
    button.addEventListener('click', /** 请求取消当前提问，确认成功后通知调用者，失败时恢复按钮。 */ async () => {
      button.disabled = true;
      try {
        const state = await cancelChatRequest(base, chatId, requestId);
        await onCancelled?.(state);
      } catch (error) {
        button.disabled = false;
        alert('取消未确认，请检查请求状态：' + (error.message || error));
      }
    });
    container.appendChild(button);
    return button;
  }

  /** 为中断请求建立状态查询、事件续接、取消和显式重试入口。 */
  function addRequestRecovery(container, base, chatId, requestId, originalBody = null, initialMessage = '') {
    const label = document.createElement('div'); container.appendChild(label);
    label.className = 'error-text'; label.textContent = initialMessage;
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'tool-btn';
    button.textContent = '检查处理状态';
    const cancel = addRequestCancel(container, base, chatId, requestId, /** 将取消后的请求快照交给恢复界面更新。 */ state => update(state));
    cancel.hidden = true;
    let observedRequest = null;
    let recovering = null;
    let recoveredBubble = null;
    let recoveredText = '';
    let recoveredBody = null;
    /** 应用请求快照并更新恢复界面，防止迟到的运行状态覆盖已确认终态。 */
    const update = state => {
      // 状态查询与事件订阅可能乱序返回，迟到的运行中快照不能把已确认终态改回处理中。
      if (observedRequest && ['cancelled', 'completed', 'partial'].includes(observedRequest.status)
          && state.status === 'running') return;
      const progress = state.context_compaction;
      const pending = progress && ['running', 'failed', 'interrupted'].includes(progress.status);
      const labels = { running: '正在处理当前提问…', cancelled: '本轮已停止，问题记录和已完成的压缩进度仍保留。',
        completed: '回答已保存。', partial: '已有回答已保存，可继续生成。', failed: '本轮处理失败。', interrupted: '本轮处理已中断。' };
      const finalFailures = {
        final_reasoning_budget_exhausted: '模型收尾耗尽输出额度：只有推理，没有正文。',
        final_output_budget_exhausted: '模型收尾耗尽输出额度，未返回正文。',
        final_empty_answer: '模型已停止生成，但没有返回正文。',
        final_tool_call_rejected: '模型收尾仍提出工具调用；这些调用未执行。',
        final_output_truncated: '模型正文被截断，未标记为完整回答。',
        final_generation_timeout: '最终回答生成超时。',
        final_connection_error: '最终回答阶段模型连接失败。',
        final_policy_unsupported: '上游拒绝了收尾参数，请检查模型服务与代理配置。',
        final_model_error: '最终回答阶段模型服务出错。',
        final_response_incomplete: '模型响应流未完整结束。',
        final_answer_unavailable: '模型未生成可用的最终回答。'
      };
      if (finalFailures[state.error_code]) labels.failed = finalFailures[state.error_code] + ' 已读资料和工具过程已保留，重试前会检查来源是否仍可访问。';
      if (['backend_restarted', 'backend_shutdown'].includes(state.error_code)) labels.interrupted = '后端已重启或停止；已完成步骤保留，可手动恢复本次提问。';
      if (state.status === 'running') labels.running = { reading: '正在读取原文片段…', generating: '正在生成回答…',
        finalizing: '正在根据已取得的证据生成最终回答…' }[state.phase] || labels.running;
      for (const step of state.tool_steps || []) updateEnhancementCard(container,
        step.status === 'running' && state.status !== 'running' ? { ...step, status: 'error', error: '步骤已中断，结果尚未确认' } : step);
      if (state.kb_progress && state.status !== 'running') updateEnhancementCard(container, state.kb_progress);
      label.textContent = state.status === 'cancelled' ? labels.cancelled : pending ? compactionLabel(progress)
        : (labels[state.status] || '暂时无法确认处理状态。');
      if (state.document_analysis) label.textContent += '\n' + documentAnalysisLabel(state.document_analysis);
      label.className = ['failed', 'interrupted'].includes(state.status) ? 'error-text' : 'request-status';
      button.textContent = state.can_retry ? (pending ? '继续处理原问题' : '重试本次提问')
        : ['completed', 'partial'].includes(state.status) ? '查看已保存回答' : '检查处理状态';
      button.hidden = state.status === 'cancelled';
      button.disabled = false;
      cancel.hidden = !state.can_cancel || !['failed', 'interrupted'].includes(state.status);
      if (state.status === 'running' && container.isConnected && currentChatId === chatId) {
        observedRequest = bindChatRequest(base, chatId, requestId, /** 应用请求终态，已保存回答属于当前会话时刷新历史展示。 */ async result => {
          update(result);
          if (currentChatId === chatId && ['completed', 'partial'].includes(result.status)) await resumeSession(chatId);
        });
        if (!recovering) {
          recovering = new AbortController();
          const activeRecovery = recovering;
          resolveApiRequestConfig().then(/** 确认后端地址未变后恢复原请求的事件订阅。 */ settings => {
            if (settings.safeApiUrl !== base) throw new Error('后端地址已改变，请切回原地址恢复');
            return ChatTransport.follow({
              stateUrl: buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/requests/${encodeURIComponent(requestId)}`),
              headers: settings.apiKey ? { Authorization: `Bearer ${settings.apiKey}` } : {},
              signal: activeRecovery.signal,
              /** 在请求恢复连接断开时展示重连状态，说明后端任务仍保留。 */
              onConnection: status => { if (status === 'reconnecting') label.textContent = '连接已断开，正在恢复进度；后端任务未取消。'; },
              /** 处理恢复订阅的快照、工具步骤和正文，离开原会话时停止展示连接。 */
              onEvent: async event => {
                if (!container.isConnected || currentChatId !== chatId) { activeRecovery.abort(); return; }
                if (event.request_snapshot) {
                  update(event.request_snapshot);
                  if (['completed', 'partial'].includes(event.request_snapshot.status)) await resumeSession(chatId);
                } else if (event.enhancement_step) {
                  updateEnhancementCard(container, event.enhancement_step);
                } else if (event.choices?.some(/** 判断恢复事件是否包含新增正文。 */ choice => choice.delta?.content)) {
                  recoveredBubble ||= createMessageNode('ai');
                  if (!recoveredBody) {
                    recoveredBody = document.createElement('div'); recoveredBody.className = 'markdown-body';
                    recoveredBubble.appendChild(recoveredBody);
                  }
                  recoveredText += event.choices.map(/** 提取恢复流中各候选响应的正文增量。 */ choice => choice.delta?.content || '').join('');
                  renderMarkdownInto(recoveredBody, recoveredText, { streaming: true });
                } else if (event.heartbeat) {
                  label.textContent = {finalizing:'正在根据已取得的资料生成回答…',reading:'正在读取资料…',generating:'正在处理当前提问…'}[event.heartbeat.phase] || '后端仍在处理当前提问…';
                }
              }
            });
          }).catch(/** 在恢复区域仍存在时显示连接错误。 */ error => { if (container.isConnected) label.textContent = error.message; })
            .finally(/** 仅清除本轮恢复订阅持有的控制器引用。 */ () => { if (recovering === activeRecovery) recovering = null; });
        }
      } else if (observedRequest && !observedRequest.live) {
        releaseChatRequest(observedRequest, state.status);
      }
      if (state.status !== 'running') recovering?.abort();
    };
    /** 查询原会话请求状态，在未建立事件订阅时安排后续状态刷新。 */
    const refresh = async () => {
      if (!container.isConnected || currentChatId !== chatId) return;
      try {
        const state = await callBackendApi(buildBackendEndpointUrl(base,
          `/v1/sessions/${encodeURIComponent(chatId)}/requests/${encodeURIComponent(requestId)}`));
        update(state);
        if (state.status === 'running' && !recovering) setTimeout(refresh, 3000);
      } catch { /* 状态读取失败时保留手动检查入口。 */ }
    };
    setTimeout(refresh, 0);
    button.addEventListener('click', /** 查询原请求状态；按可恢复性显示已保存回答或显式重试原问题。 */ async () => {
      if (_sendingLock || _chatRequest?.stopping) return;
      if (currentChatId !== chatId) { alert('请先打开此请求所属的会话。'); return; }
      _sendingLock = true;
      updateSendButton();
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
        update(state);
        if (state.status === 'completed' || state.status === 'partial') {
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
      } finally { _sendingLock = false; updateSendButton(); refresh(); }
    });
    container.appendChild(button);
  }

  /** 标记未完整生成的回答，校验可续写状态后为用户提供继续生成入口。 */
  function addPartialAnswer(container, base, chatId, requestId, canContinue = true) {
    const status = document.createElement('div');
    status.className = 'partial-answer-status';
    const label = document.createElement('div');
    label.textContent = '已保存，回答因输出长度限制尚未完成。';
    status.appendChild(label);
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'tool-btn'; button.textContent = '继续生成';
    button.disabled = !canContinue;
    let continuationId = null;
    button.addEventListener('click', /** 校验部分回答仍可续写后，以新的请求标识提交继续生成。 */ async () => {
      if (_sendingLock || _chatRequest?.stopping) return;
      if (currentChatId !== chatId) { alert('请先打开此回答所属的会话。'); return; }
      _sendingLock = true; button.disabled = true;
      updateSendButton();
      try {
        const state = await callBackendApi(buildBackendEndpointUrl(base,
          `/v1/sessions/${encodeURIComponent(chatId)}/requests/${encodeURIComponent(requestId)}`));
        if (!state.can_continue) {
          label.textContent = '已有正文已保存；此回答当前不可直接续写，请检查后续消息或重新提供必要的图片。';
          return;
        }
        const settings = await resolveApiRequestConfig();
        if (settings.safeApiUrl !== base || currentChatId !== chatId) {
          throw new Error('会话或后端地址已改变，请回到原会话后继续。');
        }
        continuationId ||= createMessageId();
        const original = state.retry_request;
        const body = { context_mode: 'server', chat_id: chatId, request_id: continuationId,
          continuation_of: requestId, expected_last_seq: state.last_seq, stream: true,
          model: original.model, kb_id: original.kb_id || '', search_query: original.search_query || '',
          messages: [{ role: 'user', content: '继续生成' }] };
        sessionSequences.set(`${base}|${chatId}`, state.last_seq);
        await runServerChat('继续生成', null, '', settings, body, true);
      } catch (error) {
        button.disabled = false;
        alert('无法继续生成: ' + (error.message || error));
      } finally { _sendingLock = false; updateSendButton(); }
    });
    status.appendChild(button);
    container.appendChild(status);
  }

  /** 校验会话及附件并提交聊天，通过可恢复事件流渲染回答、工具进度和保存状态。 */
  async function runServerChat(text, image, searchQuery, settings, retryBody = null, newTurn = false) {
    const { apiKey, modelName, safeApiUrl: base } = settings;
    const kbId = window._kbBoundId || '';
    const chatId = retryBody?.chat_id || await getOrCreateCurrentChatId();
    const key = `${base}|${chatId}`;
    const epoch = chatAttachmentEpoch;
    const pending = !retryBody && image ? { chatId, base, controller: new AbortController() } : null;
    if (pending) { chatUpload = pending; renderComposerImage(); updateSendButton(); }
    /** 检查发送是否已取消，或附件版本、当前会话是否发生变化。 */
    const checkPending = () => {
      if (pending?.controller.signal.aborted || epoch !== chatAttachmentEpoch || currentChatId !== chatId) {
        throw new DOMException('发送已取消或会话已切换', 'AbortError');
      }
    };
    let body;
    try {
      const capabilities = await requireServerContext(base);
      checkPending();
      if ((image || retryBody?.attachment_ids?.length) && (capabilities.attachments?.protocol_version !== 1 || !capabilities.attachments.enabled)) {
        throw new Error('后端尚未启用 Chat 附件协议，请更新后端并配置附件地址和签名密钥。');
      }
      if (!retryBody || !sessionSequences.has(key)) {
        const state = await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/messages?limit=1`));
        sessionSequences.set(key, state.last_seq);
        if (!retryBody && state.context_compaction && ['running', 'failed', 'interrupted'].includes(state.context_compaction.status)) {
          throw new Error('当前会话有未完成的历史压缩，请先恢复或取消原请求。');
        }
      }
      checkPending();
      if (image && !retryBody) {
        if (image.uploaded && (image.base !== base || image.chatId !== chatId)) throw new Error('图片属于其他后端或会话，请重新选择');
        if (!image.uploaded) {
          const uploaded = await ChatImages.upload(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/attachments`), apiKey, image, pending.controller.signal);
          image.uploaded = uploaded; image.base = base; image.chatId = chatId;
        }
      }
      checkPending();
      const currentSettings = await resolveApiRequestConfig();
      checkPending();
      if (currentSettings.safeApiUrl !== base || currentSettings.modelName !== modelName || currentSettings.apiKey !== apiKey) throw new Error('发送期间配置已改变，尚未提交聊天');
      body = retryBody || {
        context_mode: 'server', chat_id: chatId, request_id: createMessageId(),
        expected_last_seq: sessionSequences.get(key), model: String(modelName).trim(), stream: true,
        kb_id: kbId, search_query: searchQuery,
        messages: [{ role: 'user', content: text }],
        attachment_ids: image ? [image.uploaded.attachment_id] : [],
      };
    } catch (error) {
      if (!retryBody && epoch === chatAttachmentEpoch && currentChatId === chatId) {
        if (!document.getElementById('chatInput').value) document.getElementById('chatInput').value = text;
      }
      if (error.name !== 'AbortError') alert('尚未提交聊天: ' + error.message);
      return;
    } finally {
      if (pending && chatUpload === pending) { chatUpload = null; renderComposerImage(); updateSendButton(); }
    }
    if (!retryBody) {
      const input = document.getElementById('chatInput');
      if (input.value.trim() === text) input.value = '';
      if (image && chatImage === image) clearChatImage();
      if (searchQuery) {
        const toggle = document.getElementById('webSearchToggle');
        if (toggle) toggle.checked = false;
        document.getElementById('webSearchBtn')?.classList.remove('is-active');
      }
    }
    if (!retryBody || newTurn) {
      const user = createMessageNode('user');
      user.dataset.requestId = body.request_id;
      const label = document.createElement('div'); label.textContent = text;
      user.appendChild(label);
      if (image) {
        renderChatAttachment(user, { ...image.uploaded, first_frame: image.first_frame || image.uploaded.first_frame }, base, chatId, image);
      }
    }
    const bubble = createMessageNode('ai');
    bubble.dataset.requestId = body.request_id;
    const content = document.createElement('div'); content.className = 'markdown-body'; bubble.appendChild(content);
    const streamer = createMarkdownStreamer(content);
    const progressLabel = document.createElement('div'); bubble.appendChild(progressLabel);
    const msgId = createMessageId();
    let reply = ''; let meta = null; let sources = []; let done = false;
    scrollToBottom('instant');
    await new Promise(/** 绑定当前聊天请求与展示连接，等待终态收尾后完成本轮发送。 */ resolve => {
      let request = null;
      const connection = new AbortController();
      /** 结束本轮展示连接，完成正文渲染，并依据持久化确认显示引用或恢复入口。 */
      const finish = (error = '') => {
        if (done) return;
        done = true;
        connection.abort();
        releaseChatRequest(request, meta?.status || 'interrupted');
        progressLabel.remove();
        if (reply) streamer.finalize(reply); else streamer.cancel();
        if (meta?.persisted && !error) {
          sessionSequences.set(key, meta.last_seq);
          if (sources.length || bubble.dataset.invalidCitations) renderSearchCitations(bubble, sources);
          if (meta.status === 'partial') addPartialAnswer(bubble, base, chatId, body.request_id, meta.can_continue);
        } else {
          const status = meta?.status === 'cancelled' ? '本轮已停止，问题记录和已完成的压缩进度仍保留。' : meta?.context_compaction && meta.context_compaction.status !== 'completed'
            ? compactionLabel(meta.context_compaction)
            : `未确认保存：${error || '连接已结束，但没有收到保存确认'}`;
          if (meta?.status === 'cancelled') {
            const label = document.createElement('div'); label.className = 'request-status'; label.textContent = status;
            bubble.appendChild(label);
          } else addRequestRecovery(bubble, base, chatId, body.request_id, body, status);
        }
        resolve();
      };
      /** 按请求标识分发正文、工具进度和终态事件，更新当前回答气泡。 */
      const listener = msg => {
        if (msg.msgId !== msgId) return;
        if (msg.type === 'LLM_CHUNK') { reply += msg.chunk; streamer.update(reply); scrollToBottom(); }
        else if (msg.type === 'LLM_SESSION_META') meta = msg.session_meta;
        else if (msg.type === 'LLM_CONTEXT_COMPACTION') {
          const history = document.getElementById('chatHistory');
          const following = history.scrollHeight - history.scrollTop - history.clientHeight < 80;
          progressLabel.textContent = compactionLabel(msg.progress);
          if (following) scrollToBottom('instant');
        }
        else if (msg.type === 'LLM_DOCUMENT_ANALYSIS') {
          progressLabel.textContent = documentAnalysisLabel(msg.progress);
        }
        else if (msg.type === 'LLM_ENHANCEMENT_STEP') {
          updateEnhancementCard(bubble, msg.step);
          if (msg.step.status === 'done') sources.push(...(msg.step.sources || []));
        } else if (msg.type === 'LLM_ERROR') finish(msg.error);
        else if (msg.type === 'LLM_DONE') finish();
      };
      request = bindChatRequest(base, chatId, body.request_id, /** 处理用户停止后的终态，必要时载入已保存回答。 */ async state => {
        meta = { ...state, persisted: ['completed', 'partial'].includes(state.status) };
        finish();
        if (meta.persisted && currentChatId === chatId) await resumeSession(chatId);
      }, true);
      const headers = { 'Content-Type': 'application/json' };
      if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
      /** 为传输事件补充当前消息标识，再交给统一的界面事件处理器。 */
      const emit = (type, values = {}) => listener({ type, msgId, ...values });
      ChatTransport.follow({ submitUrl: `${base}/chat/completions`, body, headers, signal: connection.signal,
        stateUrl: buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/requests/${encodeURIComponent(body.request_id)}`),
        /** 显示本轮聊天连接或重连状态。 */
        onConnection: state => {
          progressLabel.textContent = state === 'reconnecting' ? '连接已断开，正在恢复进度；后端任务未取消。' : '已连接，正在处理…';
        },
        /** 将后端事件转换为界面更新，收到终态快照时恢复已保存正文并收尾。 */
        onEvent: event => {
          if (event.request_snapshot) {
            const state = event.request_snapshot;
            meta = { ...state, persisted: ['completed', 'partial'].includes(state.status) };
            if (meta.persisted) {
              reply = state.messages.find(/** 从已保存消息中定位助手正文。 */ m => m.role === 'assistant')?.content || '';
              sources = (state.tool_steps || []).flatMap(/** 汇集已保存工具步骤的引用来源。 */ step => step.sources || []);
              for (const step of state.tool_steps || []) updateEnhancementCard(bubble, step);
            }
            finish();
            return;
          }
          if (event.session_meta) emit('LLM_SESSION_META', { session_meta: event.session_meta });
          if (event.context_compaction) emit('LLM_CONTEXT_COMPACTION', { progress: event.context_compaction });
          if (event.document_analysis) emit('LLM_DOCUMENT_ANALYSIS', { progress: event.document_analysis });
          if (event.enhancement_step) emit('LLM_ENHANCEMENT_STEP', { step: event.enhancement_step });
          if (event.heartbeat) progressLabel.textContent = {finalizing:'正在根据已取得的资料生成回答…',reading:'正在读取资料…',generating:'正在处理当前提问…'}[event.heartbeat.phase] || '后端仍在处理当前提问…';
          if (event.error) { emit('LLM_ERROR', { error: event.error.message || event.error.code }); return; }
          for (const choice of event.choices || []) if (choice.delta?.content) emit('LLM_CHUNK', { chunk: choice.delta.content });
        }
      }).then(/** 事件订阅结束但未收尾时进入状态恢复流程。 */ () => { if (!done) finish('连接已结束，正在检查请求状态'); }).catch(/** 以传输错误结束当前展示连接并提供恢复入口。 */ error => finish(error.message));
    });
  }

  // ── 会话历史(抽屉:列表 / 续谈 / 重命名 / 删除)──

  /** 读取并返回经过配置与凭证绑定校验的当前后端基址。 */
  async function backendBase() {
    // 取当前后端 base url(localhost:8000/v1 之类);失败抛错由调用方降级
    const { safeApiUrl } = await resolveApiRequestConfig();
    return safeApiUrl;
  }

  /** 打开会话历史抽屉并触发列表加载。 */
  function openDrawer() {
    document.getElementById('sessionDrawer')?.classList.add('open');
    document.getElementById('drawerMask')?.classList.remove('hidden');
    loadSessionList().catch(/** 记录会话列表加载失败。 */ (e) => console.warn('加载会话列表失败', e));
  }

  /** 关闭会话历史抽屉及其遮罩。 */
  function closeDrawer() {
    document.getElementById('sessionDrawer')?.classList.remove('open');
    document.getElementById('drawerMask')?.classList.add('hidden');
  }

  /** 读取历史会话列表，渲染当前会话标记与续谈、重命名、删除入口。 */
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
      titleEl.addEventListener('click', /** 选择历史会话并恢复消息展示。 */ () => resumeSession(s.chat_id));
      item.appendChild(titleEl);

      const actions = document.createElement('div');
      actions.className = 'session-item-actions';
      const renameBtn = document.createElement('button');
      renameBtn.textContent = '✏️';
      renameBtn.title = '重命名';
      renameBtn.addEventListener('click', /** 阻止卡片点击冒泡后执行会话重命名。 */ (ev) => { ev.stopPropagation(); renameSession(s.chat_id, s.title); });
      const delBtn = document.createElement('button');
      delBtn.textContent = '🗑️';
      delBtn.title = '删除';
      delBtn.addEventListener('click', /** 阻止卡片点击冒泡后请求删除会话。 */ (ev) => { ev.stopPropagation(); deleteSession(s.chat_id, s.title); });
      actions.appendChild(renameBtn);
      actions.appendChild(delBtn);
      item.appendChild(actions);

      listEl.appendChild(item);
    }
  }

  /** 按序号分页恢复历史展示及请求状态，并丢弃会话切换后迟到的响应。 */
  async function resumeSession(chatId, beforeSeq = null) {
    if (!beforeSeq) invalidateChatAttachments();
    const epoch = chatAttachmentEpoch;
    let base;
    try { base = await backendBase(); } catch { return; }
    let data;
    try {
      await requireServerContext(base);
      const query = `?limit=100${beforeSeq ? `&before_seq=${beforeSeq}` : ''}`;
      data = await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}/messages${query}`), 'GET');
    } catch (e) {
      if (epoch !== chatAttachmentEpoch) return;
      alert('载入会话失败: ' + (e?.message || ''));
      return;
    }
    if (epoch !== chatAttachmentEpoch || (beforeSeq && currentChatId !== chatId)) return;
    const messages = data?.messages || [];
    // 切到该会话，只恢复展示；模型上下文始终由后端构建。
    currentChatId = chatId;
    updateSendButton();
    await chrome.storage.session.set({ [CURRENT_CHAT_ID_KEY]: chatId });
    if (epoch !== chatAttachmentEpoch) return;
    sessionSequences.set(`${base}|${chatId}`, data.last_seq);
    // DOM 全量渲染(用户看得到完整历史)
    const historyEl = document.getElementById('chatHistory');
    const oldNodes = beforeSeq ? Array.from(historyEl.children).filter(/** 加载更早消息时保留现有消息节点，排除旧分页按钮。 */ node => node.id !== 'loadOlderMessages') : [];
    historyEl.replaceChildren();
    if (data.has_more) {
      const more = document.createElement('button'); more.id = 'loadOlderMessages'; more.type = 'button';
      more.className = 'tool-btn'; more.textContent = '加载更早消息';
      more.addEventListener('click', /** 按返回的消息序号加载更早历史。 */ () => resumeSession(chatId, data.before_seq));
      historyEl.appendChild(more);
    }
    for (const m of messages) {
      const bubble = createMessageNode(m.role === 'user' ? 'user' : 'ai');
      if (m.request_id) bubble.dataset.requestId = m.request_id;
      if (m.role === 'user') {
        const node = document.createElement('div');
        node.textContent = m.content;
        bubble.appendChild(node);
        for (const attachment of m.attachments || []) renderChatAttachment(bubble, attachment, base, chatId);
        if (m.request_id && !['completed', 'partial'].includes(m.status)) {
          for (const step of m.tool_steps || []) updateEnhancementCard(bubble,
            step.status === 'running' && m.status !== 'running' ? { ...step, status: 'error', error: '步骤已中断，结果尚未确认' } : step);
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
        if (allSources.length || bubble.dataset.invalidCitations) {
          renderSearchCitations(bubble, allSources);
        }
        if (m.status === 'partial') {
          addPartialAnswer(bubble, base, chatId, m.request_id, m.can_continue === true);
        }
      }
    }
    for (const node of oldNodes) historyEl.appendChild(node);
    if (!beforeSeq) scrollToBottom();
    closeDrawer();
  }

  /** 收集新标题并更新指定会话，成功后刷新历史列表。 */
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

  /** 确认后删除指定会话；若为当前会话则清空展示并创建新会话。 */
  async function deleteSession(chatId, title) {
    if (!confirm(`删除会话「${title || '未命名'}」?`)) return;
    try {
      const base = await backendBase();
      await callBackendApi(buildBackendEndpointUrl(base, `/v1/sessions/${encodeURIComponent(chatId)}`), 'DELETE');
      // 若删的是当前会话,顺带清空并开新会话
      if (chatId === currentChatId) {
        document.getElementById('chatHistory').replaceChildren();
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

  /** 加载并按类型展示长期记忆，仅允许最新一次请求更新界面。 */
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

  /** 构建带主题、到期状态和编辑删除入口的单条记忆界面。 */
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
    editBtn.addEventListener('click', /** 打开当前记忆的编辑操作。 */ () => editMemory(m.memory_id, m.content));
    const delBtn = document.createElement('button');
    delBtn.textContent = '🗑️';
    delBtn.title = '删除';
    delBtn.addEventListener('click', /** 请求删除当前记忆。 */ () => deleteMemory(m.memory_id, m.content));
    actions.appendChild(editBtn);
    actions.appendChild(delBtn);
    item.appendChild(actions);
    return item;
  }

  /** 收集非空记忆内容并保存修改，再刷新记忆列表。 */
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

  /** 确认后删除指定记忆，并刷新列表或报告失败。 */
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

  /** 根据新增记忆的空闲、处理中和完成状态更新按钮。 */
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

  /** 防止重复提交，保存用户输入的核心记忆及主题，并刷新列表。 */
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
      _addMemoryDoneTimer = setTimeout(/** 完成提示结束后将新增记忆按钮恢复为空闲状态。 */ () => {
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

  /** 启动长期记忆整理，读取进度事件并展示冲突、过期与合并处理结果。 */
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

    /** 追加一条指定样式的记忆整理日志，并滚动到最新记录。 */
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

    /** 解析记忆整理事件，更新进度、分类计数和完成或失败提示。 */
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

  /** 获取或创建当前回答的工具步骤区域，优先放在正文之前。 */
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

  /** 按工具调用标识更新检索、读取和资料整理步骤，展示证据范围与未完成状态。 */
  function updateEnhancementCard(bubbleEl, step) {
    const card = getOrCreateEnhancementCard(bubbleEl);
    /** 转义插入工具步骤 HTML 的文本，避免内容被解释为标签或属性。 */
    const esc = (t) => String(t || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    const stepId = step.tool_call_id || `${step.type}_${step.batch_id || step.query || ''}_${step.part || ''}`;
    let stepEl = card.querySelector(`[data-step-id="${CSS.escape(stepId)}"]`);

    const isHistory = ['list_session_history', 'search_session_history', 'read_session_history'].includes(step.type);
    const isDocument = step.type === 'analyze_session_document';
    const icon = isHistory ? '🕘' : isDocument ? '📄' : step.type === 'web_search' ? '🔍' : '📚';

    if (step.status === 'running') {
      if (!stepEl) {
        stepEl = document.createElement('div');
        stepEl.setAttribute('data-step-id', stepId);
        card.appendChild(stepEl);
      }
      stepEl.className = 'enhancement-step running';
      const label = step.type === 'kb_workspace' ? `正在整理已取得资料（片段 ${step.part || 1}/${step.parts || 1}）` : isDocument ? '正在分片分析原文' : isHistory ? '正在回查会话历史' : step.type === 'web_search' ? '正在搜索' : step.type === 'kb_list_documents' ? '正在读取知识库目录' : step.type === 'kb_read_document' ? '正在读取知识库正文' : '正在检索知识库';
      stepEl.innerHTML = `<span class="step-icon">${icon}</span><span class="step-text">${label}${step.query ? ` “${esc(step.query)}”` : ''}</span>`;
    } else if (step.status === 'done') {
      if (!stepEl) {
        stepEl = document.createElement('div');
        stepEl.setAttribute('data-step-id', stepId);
        card.appendChild(stepEl);
      }
      stepEl.className = 'enhancement-step done';
      if (step.type === 'kb_workspace') {
        const labels = {complete:'资料整理已保存，原文仍可回查', processing:'本片整理已保存，整包尚未发布', no_gain:'整理未减少输入，继续保留原文', failed:'本包整理失败，原文仍保留', too_large:'本包无法在整理窗口内处理，原文仍保留'};
        stepEl.textContent = `📚 ${labels[step.outcome] || '资料整理进度已保存'}（${step.part || 1}/${step.parts || 1}；不代表结论已核实）`;
        return;
      }
      if (step.type === 'citation_warning') {
        bubbleEl.dataset.invalidCitations = JSON.stringify(step.invalid_indices || []);
        stepEl.textContent = `⚠ 引用 ${(step.invalid_indices || []).map(/** 把失效引用编号格式化为方括号标记。 */ i => `[${i}]`).join('、')} 未对应本次可见证据，未自动改引（不做语义核验）`;
        return;
      }
      if (step.type === 'kb_progress') {
        const reasons = {time_reserve: '已预留回答时间', tool_limit: '工具次数已到限', token_budget: '知识库额度已用尽',
          decision_limit: '检索轮数已到限', model_call_limit: '模型调用次数已到限', no_progress: '连续步骤未新增材料、定位或需求进展',
          tool_arguments_not_repaired: '连续工具参数纠错未成功，使用已取得资料收尾',
          needs_assessed: '正在按问题整理资料', context_space_exhausted: '当前上下文空间不足，整理已取得资料'};
        const invalid = (step.kb_pending || []).filter(/** 统计定位参数错误的资料项。 */ p => p.error_kind === 'invalid_location').length;
        const pending = (step.kb_pending || []).filter(/** 排除定位错误，保留可继续读取的资料缺口。 */ p => p.error_kind !== 'invalid_location');
        const missing = pending.filter(/** 筛选必须补读的资料缺口。 */ p => p.required !== false).length;
        const optional = pending.length - missing;
        const restoredLabel = step.restored ? (step.request_status === 'running' && step.request_phase === 'finalizing'
          ? '正在根据已取得的资料生成回答' : '已恢复本轮资料读取记录（不代表已进入回答阶段）') : null;
        stepEl.textContent = `📚 ${restoredLabel || reasons[step.kb_stop_reason] || '开始整理已取得的知识库资料'}${missing ? `；仍有 ${missing} 项读取缺口` : ''}${optional ? `；另有 ${optional} 段未读范围，可按需续读` : ''}`;
        if (invalid) stepEl.textContent += `；${invalid} 项定位参数错误（不是未读正文）`;
        if (step.kb_read_coverage?.status === 'selected_reads_complete') stepEl.textContent += '；已选择的完整原文范围均已交付（不代表结论已核实）';
        if (step.kb_workspace?.version) {
          const w = step.kb_workspace;
          stepEl.textContent += `；当前可见：原文 ${w.raw_citations?.length || 0} 项，派生摘要 ${w.derived_citations?.length || 0} 项`;
          if (w.pending_parts) stepEl.textContent += `；尚有 ${w.pending_parts} 片未整理，已保存片段可恢复`;
        }
        if (step.kb_goals?.length) {
          const details = document.createElement('details');
          const summary = document.createElement('summary');
          summary.textContent = '查看各目标取证情况（非语义核验）';
          details.append(summary);
          for (const goal of step.kb_goals) {
            const row = document.createElement('div');
            row.textContent = `${goal.label}：${({provided:'所需片段已交付',partial:'片段未齐',located:'仅定位，待补读'})[goal.status] || goal.status}`;
            details.append(row);
          }
          stepEl.append(details);
        }
        return;
      }
      if (isDocument) {
        stepEl.textContent = '📄 ' + documentAnalysisLabel(step.document_analysis);
        return;
      }
      if (step.type === 'kb_update_task') {
        stepEl.classList.add('kb-result');
        stepEl.textContent = step.status === 'running' ? '📚 正在梳理问题需求' : step.status === 'error'
          ? `📚 需求更新失败：${step.error || '请查看工具记录'}` : '📚 已更新问题需求（不代表结论已核实）';
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = `查看 ${step.kb_needs?.length || 0} 项需求`;
        details.className = 'kb-catalog';
        details.append(summary);
        for (const need of step.kb_needs || []) {
          const row = document.createElement('div');
          row.textContent = `${need.id} ${need.question}：${({open:'待补充',ready:'模型认为可回答',unresolved:'尚未解决'})[need.status] || need.status}${need.missing ? `；缺少 ${need.missing}` : ''}`;
          details.append(row);
        }
        stepEl.append(details);
        return;
      }
      if (isHistory) {
        if (step.type === 'list_session_history') {
          stepEl.textContent = `🕘 已浏览历史目录：${step.result_count || 0} 项（目录不代表已读正文）`;
          return;
        }
        const seqs = (step.history_seqs || []).filter(Number.isInteger).join('、');
        const ranges = (step.history_ranges || []).map(/** 格式化本次历史回查的消息及字符范围。 */ r => `M${r.seq}[${r.content_offset},${r.content_end})`).join('、');
        const detail = seqs ? `${ranges || `消息 ${seqs}`}${step.has_more || step.has_unread_content ? '（未读完，仅本次片段）' : ''}`
          : step.outcome === 'budget_exhausted' ? '预算不足，未纳入原文' : '本次未找到匹配，不代表历史中不存在';
        stepEl.textContent = `🕘 已回查会话历史：${detail}`;
        return;
      }
      if (step.type === 'evidence_check') {
        stepEl.classList.add('kb-result');
        stepEl.textContent = step.outcome === 'reviewed'
          ? '📎 历史记录：旧版模型复核已完成（不代表人工核实）'
          : '⚠ 历史记录：旧版模型复核未完成';
        return;
      }
      stepEl.classList.toggle('kb-result', ['kb_search', 'kb_list_documents', 'kb_read_document', 'kb_get_document_outline'].includes(step.type));
      if (step.type === 'kb_list_documents' && step.catalog) {
        const catalog = step.catalog;
        const counts = catalog.counts;
        stepEl.replaceChildren();
        const heading = document.createElement('span');
        heading.className = 'step-text';
        heading.textContent = `📚 目录统计：共 ${counts.total} 份，已索引 ${counts.indexed} 份（可检索 ${counts.searchable} 份），处理中 ${counts.pending} 份，失败 ${counts.failed} 份`;
        const details = document.createElement('details');
        details.className = 'kb-catalog';
        const summary = document.createElement('summary');
        summary.textContent = `第 ${catalog.page} 页 · 本页 ${catalog.documents.length} 份 · ${catalog.has_more ? '还有下一页，未列全' : catalog.page === 1 ? '已到末页' : '已到末页，仅展示本页'}（目录不含正文）`;
        details.append(summary);
        const list = document.createElement('ul');
        for (const doc of catalog.documents) {
          const row = document.createElement('li');
          const status = doc.searchable ? '可检索' : doc.status === 'failed' ? '索引失败' : doc.status === 'pending' ? '处理中' : '已索引，暂不可检索';
          row.textContent = `${doc.filename} · ${status} · ${doc.chunk_count} 个片段`;
          list.append(row);
        }
        details.append(list);
        stepEl.append(heading, details);
        return;
      }
      const label = step.type === 'web_search' ? '已搜索' : step.type === 'kb_read_document' ? '已读取知识库页块（仅所列范围）' : step.type === 'kb_get_document_outline' ? '已读取文档目录（不代表正文）' : '已检索知识库';
      const count = step.result_count || 0;
      const outcomeText = {
        empty: '搜索完成，未找到匹配结果',
        empty_kb: '知识库没有文档',
        no_searchable_documents: '文档存在，暂无可检索文档',
        no_match: '检索完成，0 条匹配',
        budget_exhausted: '本轮知识库预算不足，未继续读取',
        version_changed: '索引已换版，本轮未混入新版本证据',
      }[step.outcome];
      let countText = ` · ${count} 项${step.type === 'web_search' && step.outcome === 'partial' ? '（部分引擎失败）' : ''}`;
      if (step.type === 'web_search' && step.reader) {
        const reader = step.reader;
        countText += `；已读取 ${Number(reader.read_count) || 0} 篇正文`;
        if (Number.isInteger(reader.included_count)) countText += `，纳入 ${reader.included_count} 篇`;
        if (reader.failed_count) countText += `，${Number(reader.failed_count) || 0} 篇读取失败`;
        if (reader.error_code === 'reader_configuration_error') countText += '（正文服务配置无效，仅有摘要）';
        else if (reader.error_code === 'budget_exhausted') countText += '（读取或上下文预算不足）';
        else if (reader.outcome === 'error') countText += '（仅有搜索摘要）';
      }
      const counts = step.counts;
      const stateText = step.outcome === 'no_searchable_documents' && counts
        ? `（共 ${counts.total} 份，已索引 ${counts.indexed} 份，处理中 ${counts.pending} 份，失败 ${counts.failed} 份）` : '';
      const invalidGaps = (step.kb_pending || []).filter(/** 统计知识库定位错误项。 */ p => p.error_kind === 'invalid_location').length;
      const readablePending = (step.kb_pending || []).filter(/** 筛选仍具有有效读取位置的待读项。 */ p => p.error_kind !== 'invalid_location');
      const requiredGaps = readablePending.filter(/** 统计必须读取但尚未交付的范围。 */ p => p.required !== false).length;
      const optionalGaps = readablePending.length - requiredGaps;
      const gapText = (requiredGaps ? `；${requiredGaps} 项待补读/处理` : '') + (optionalGaps ? `；${optionalGaps} 段未读范围，可按需续读` : '') + (invalidGaps ? `；${invalidGaps} 项定位参数错误（不是未读正文）` : '');
      stepEl.innerHTML = `<span class="step-icon">${icon}</span><span class="step-text">${esc(outcomeText || label)}${step.query ? ` “${esc(step.query)}”` : ''}${outcomeText ? esc(stateText) : countText}${esc(gapText)}</span>`;
      if (step.kb_goals?.length) {
        const progress = document.createElement('small');
        progress.textContent = ` 已定位 ${step.kb_goals.length} 个内容目标，其中 ${step.kb_goals.filter(/** 统计已提供所需资料的目标。 */ g => g.status === 'provided').length} 个已交付所需片段；不代表完整阅读或结论正确。`;
        stepEl.append(progress);
      }
    } else if (step.status === 'error') {
      if (!stepEl) {
        stepEl = document.createElement('div');
        stepEl.setAttribute('data-step-id', stepId);
        card.appendChild(stepEl);
      }
      stepEl.className = 'enhancement-step error';
      const failed = step.outcome === 'error';
      const label = step.type === 'kb_update_task' ? '需求状态更新失败' : failed ? (isHistory ? '会话历史回查失败' : step.type === 'web_search' ? '联网搜索失败' : '知识库查询失败') : '工具未执行';
      const itemErrors = (step.items || []).filter(/** 筛选包含错误的批量工具结果。 */ item => item.error).map(/** 组合工具错误与合法字符范围提示。 */ item => item.error +
        (item.valid_range ? `（合法字符范围 [${item.valid_range.offset}, ${item.valid_range.end})）` : ''));
      const errorText = step.error || itemErrors.join('；') || '工具参数无效';
      stepEl.innerHTML = `<span class="step-icon">⚠</span><span class="step-text">${label}：${esc(errorText)}</span>`;
      stepEl.title = errorText;
    }
  }

  // ─── 引用相关函数 ─────────────────────────────────────────

  /** 整理本轮引用来源，替换正文中的可用引用标记并渲染可展开的来源面板。 */
  function renderSearchCitations(bubbleEl, sources) {
    if ((!sources || !sources.length) && !bubbleEl.dataset.invalidCitations) return;
    sources = sources || [];
    const entries = [...new Map(sources.map(/** 规范化引用地址和元数据，按引用编号构建去重项。 */ (source, i) => {
      const url = typeof source.url === 'string' ? source.url : '';
      let href = '';
      try {
        const parsed = new URL(url);
        if (parsed.protocol === 'https:' || parsed.protocol === 'http:') href = parsed.href;
      } catch (_) { /* 非法来源仅显示文本。 */ }
      const index = Number.isInteger(source.index) && source.index > 0 ? source.index : i + 1;
      return [index, {
        index,
        title: String(source.title || ''), snippet: String(source.snippet || ''),
        contentSource: source.content_source, readStatus: source.read_status,
        contextStatus: source.context_status, structureMode: source.structure_mode,
        readError: String(source.read_error || ''), truncated: Boolean(source.truncated),
        fetchedAt: String(source.fetched_at || ''), extractedDate: String(source.extracted_date || ''),
        isKb: url.startsWith('kb://'), href,
        hasDocumentId: Boolean(source.doc_id),
        kbSource: source,
        docKey: source.doc_id ? `${source.kb_id}/${source.doc_id}` : (url || `legacy-${index}`),
      }];
    })).values()];
    const byIndex = new Map(entries.map(/** 建立引用编号到来源信息的查询映射。 */ source => [source.index, source]));
    const invalid = new Set(JSON.parse(bubbleEl.dataset.invalidCitations || '[]'));
    const contentEl = bubbleEl.querySelector('.markdown-body');
    if (contentEl) {
      for (const link of contentEl.querySelectorAll('a')) {
        const match = /^\[(\d+)\]$/.exec(link.textContent);
        if (match && invalid.has(Number(match[1]))) {
          const label = document.createElement('span');
          label.textContent = link.textContent;
          label.title = '没有对应本次可见证据';
          link.replaceWith(label);
        }
      }
      const walker = document.createTreeWalker(contentEl, NodeFilter.SHOW_TEXT, {
        /** 排除链接、代码、公式等区域，仅允许普通文本节点参与引用标记替换。 */
        acceptNode(node) {
          return node.parentElement.closest('a, code, pre, script, style, textarea, button, .katex, math, .search-citation')
            ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
        },
      });
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      for (const node of nodes) {
        const fragment = document.createDocumentFragment();
        let offset = 0;
        for (const match of node.textContent.matchAll(/\[(\d+)\]/g)) {
          const before = node.textContent.slice(0, match.index);
          const after = node.textContent.slice(match.index + match[0].length);
          if (/\b[A-Za-z_]\w*\s*=\s*$/.test(before) || /"\s*:\s*$/.test(before) || /\[$/.test(before) || /^\]/.test(after)) continue;
          const source = byIndex.get(Number(match[1]));
          if (!source || invalid.has(Number(match[1]))) continue;
          fragment.append(document.createTextNode(node.textContent.slice(offset, match.index)));
          const citation = document.createElement(source.href ? 'a' : 'span');
          citation.className = 'search-citation' + (source.isKb ? ' kb-citation-inline' : '');
          citation.title = source.title;
          citation.textContent = match[0];
          if (source.href) {
            citation.href = source.href;
            citation.target = '_blank';
            citation.rel = 'noopener';
          }
          if (source.isKb && source.kbSource.parse_id) {
            citation.style.cursor = 'pointer';
            citation.addEventListener('click', /** 点击知识库引用时展示固定版本的原文位置。 */ () => showKbSource(source.kbSource));
          }
          fragment.append(citation);
          offset = match.index + match[0].length;
        }
        if (offset) {
          fragment.append(document.createTextNode(node.textContent.slice(offset)));
          node.replaceWith(fragment);
        }
      }
    }
    if (!entries.length) return;
    const previousPanel = bubbleEl.querySelector(':scope > .search-sources-panel');
    const panel = document.createElement('div');
    panel.className = 'search-sources-panel';
    const toggle = document.createElement('div');
    toggle.className = 'search-sources-toggle';
    const cited = new Set([...(contentEl?.querySelectorAll('.search-citation') || [])]
      .map(/** 从已渲染的引用标记读取编号。 */ node => Number(node.textContent.slice(1, -1))).filter(/** 仅统计具有对应来源的引用编号。 */ index => byIndex.has(index)));
    const docCount = new Set(entries.map(/** 提取来源文档标识，用于统计不同文档数量。 */ source => source.docKey)).size;
    const documentLabel = entries.some(/** 识别缺少文档身份的旧知识库引用。 */ source => source.isKb && !source.hasDocumentId)
      ? '文档数未知（旧记录）' : `${docCount} 份文档/网页`;
    toggle.textContent = `📎 检索来源：${documentLabel} · ${entries.length} 个片段 · 正文引用 ${cited.size} 个`;
    const list = document.createElement('div');
    list.className = 'search-sources-list';
    list.style.display = previousPanel?.querySelector('.search-sources-list')?.style.display || 'none';
    for (const source of entries) {
      const item = document.createElement('div');
      item.className = 'search-source-item' + (source.isKb ? ' kb-source-item' : '');
      const number = document.createElement('span');
      number.className = 'search-source-num';
      number.textContent = `[${source.index}]`;
      item.title = cited.has(source.index) ? '正文已引用；不代表事实已经人工核实' : '已检索，正文未引用';
      if (source.isKb) {
        const icon = document.createElement('span');
        icon.className = 'kb-source-icon';
        icon.textContent = '📚';
        const detail = document.createElement('div');
        detail.className = 'kb-source-detail';
        const title = document.createElement('div');
        title.className = 'kb-source-title';
        title.textContent = source.title;
        detail.append(title);
        if (source.kbSource.parse_id) {
          const open = document.createElement('button');
          open.textContent = `查看原文来源 · ${(source.kbSource.locations || []).map(/** 将引用位置格式化为文本行或 PDF 页码。 */ p => p.location_kind === 'text_span' ? `行${p.line_start}–${p.line_end}` : `第${p.page}页`).filter(/** 去除重复的页码或行范围说明。 */ (v, i, a) => a.indexOf(v) === i).join('、')}`;
          open.addEventListener('click', /** 从来源面板打开知识库原文。 */ () => showKbSource(source.kbSource));
          detail.append(open);
        } else {
          const legacy = document.createElement('small');
          legacy.textContent = '旧索引：未保留可回查的原文位置，需重新上传建立结构';
          detail.append(legacy);
        }
        if (source.snippet) {
          const snippet = document.createElement('div');
          snippet.className = 'kb-source-snippet';
          snippet.textContent = source.snippet;
          detail.append(snippet);
        }
        item.append(number, icon, detail);
      } else {
        const a = document.createElement(source.href ? 'a' : 'span');
        a.className = 'search-source-link';
        if (source.href) {
          a.href = source.href;
          a.target = '_blank';
          a.rel = 'noopener';
        }
        a.append(number, document.createTextNode(` ${source.title}`));
        item.appendChild(a);
        if (source.contentSource) {
          const status = document.createElement('div');
          status.className = 'search-source-status';
          status.textContent = source.contentSource === 'page'
            ? (source.truncated ? '已读取正文节选' : '已读取提取正文')
            : (source.readStatus === 'error' ? '正文读取失败，仅搜索摘要' : '仅搜索摘要');
          const inclusion = {
            reused: '本轮已有相关块，未重复纳入',
            budget_exhausted: '上下文预算用尽，内容未纳入',
            request_budget_exhausted: '完整请求预算不足，内容已移除',
            no_complete_block: '可用预算不足以容纳完整片段，内容未纳入',
          }[source.contextStatus];
          if (inclusion) status.textContent = `${source.readStatus === 'ok' ? '正文读取成功；' : ''}${inclusion}`;
          if (source.structureMode === 'legacy_text') status.textContent += '（旧版服务文本）';
          const detail = [];
          if (source.readError) detail.push(`错误代码：${source.readError}`);
          if (source.extractedDate) detail.push(`页面提取日期（非核实）：${source.extractedDate}`);
          if (source.fetchedAt) detail.push(`抓取时间：${source.fetchedAt}`);
          status.title = detail.join('\n');
          item.appendChild(status);
        }
      }
      list.appendChild(item);
    }
    toggle.addEventListener('click', /** 展开或收起引用来源列表。 */ () => {
      list.style.display = list.style.display === 'none' ? 'block' : 'none';
    });
    panel.appendChild(toggle);
    panel.appendChild(list);
    if (previousPanel) previousPanel.replaceWith(panel);
    else bubbleEl.appendChild(panel);
  }

  // 5. 绑定各种交互事件
  /** 按固定解析版本展示知识库引用原文，提供原文件、相邻文本和页图定位。 */
  async function showKbSource(source) {
    const dialog = document.createElement('dialog');
    dialog.style.cssText = 'width:90%;max-height:90vh;overflow:auto;background:#1e293b;color:#eee;border:1px solid #64748b';
    const close = document.createElement('button');
    close.textContent = '关闭';
    close.addEventListener('click', /** 关闭原文来源弹窗。 */ () => dialog.close());
    dialog.addEventListener('close', /** 原文弹窗关闭后移除节点。 */ () => dialog.remove());
    const title = document.createElement('h3');
    title.textContent = `${source.title} · ${source.parse_id}`;
    dialog.append(close, title);
    document.body.append(dialog);
    dialog.showModal();
    const base = `http://localhost:8000/v1/kb/${encodeURIComponent(source.kb_id)}/docs/${encodeURIComponent(source.doc_id)}`;
    for (const location of source.locations || []) {
      const heading = document.createElement('p');
      const isText = location.location_kind === 'text_span';
      heading.textContent = isText
        ? `行 ${location.line_start}–${location.line_end} · 原文字符 [${location.source_start},${location.source_end}) · 本轮提供范围`
        : `物理页 ${location.page} · ${location.block_id} [${location.start},${location.end}) · 本轮提供范围（OCR 派生文本）`;
      const text = document.createElement('pre');
      text.style.whiteSpace = 'pre-wrap';
      dialog.append(heading, text);
      try {
        const params = new URLSearchParams({parse_id: source.parse_id, block_id: location.block_id,
          offset: location.start, end: location.end});
        const response = await fetch(`${base}/read?${params}`);
        if (!response.ok) throw new Error(`读取失败 HTTP ${response.status}`);
        const data = await response.json();
        const shown = data.locations?.[0];
        if (shown?.printed_page) heading.textContent += ` · 印刷页码 ${shown.printed_page}`;
        if (shown && shown.end < location.end) {
          heading.textContent += ` · 当前仅显示块内 [${shown.start},${shown.end})，完整引用可下载原文件`;
        }
        text.textContent = data.content + (data.warnings?.length ? `\n\n警告：${data.warnings.join('；')}` : '');
        if (data.related_blocks?.length > 1) {
          const related = document.createElement('p');
          related.textContent = `已确认的关联块：${data.related_blocks.join('、')}（未在本轮引用范围内的内容不代表模型已读）`;
          dialog.append(related);
        }
        const original = document.createElement('a');
        original.href = `${base}/file${isText ? '' : `#page=${location.page}`}`;
        original.target = '_blank';
        original.rel = 'noopener';
        original.textContent = isText ? '下载完整原文件' : '打开原 PDF 页';
        dialog.append(original);
        if (isText) {
          const context = document.createElement('button');
          context.textContent = '查看相邻原文（不代表模型已读）';
          context.addEventListener('click', /** 读取引用附近的原文，并按 Unicode 字符位置高亮实际引用范围。 */ async () => {
            context.disabled = true;
            try {
              const offset = Math.max(0, location.source_start - 500);
              const response = await fetch(`${base}/text?${new URLSearchParams({parse_id:source.parse_id,offset})}`);
              if (!response.ok) throw new Error(`HTTP ${response.status}`);
              const result = await response.json();
              const preview = document.createElement('pre');
              preview.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere';
              const points = Array.from(result.content);
              const a = Math.max(0, location.source_start - offset), b = Math.min(points.length, location.source_end - offset);
              const mark = document.createElement('mark');
              mark.textContent = points.slice(a,b).join('');
              preview.append(document.createTextNode(`行 ${result.line_start}–${result.line_end}（${result.truncated ? '局部预览' : '至文件末尾'}）\n`),
                document.createTextNode(points.slice(0,a).join('')),mark,document.createTextNode(points.slice(b).join('')));
              context.after(preview);
            } catch (error) { context.textContent = `读取失败：${error.message}`; context.disabled = false; }
          });
          dialog.append(context);
        }
        if (Array.isArray(location.bbox) && location.bbox.length === 4) {
          const canvas = document.createElement('canvas');
          canvas.style.width = '100%';
          const image = new Image();
          const pageResponse = await fetch(`${base}/pages/${location.page}.png?${new URLSearchParams({parse_id: source.parse_id})}`);
          if (!pageResponse.ok) throw new Error(`原页图读取失败 HTTP ${pageResponse.status}`);
          const pageUrl = URL.createObjectURL(await pageResponse.blob());
          dialog.addEventListener('close', /** 原文弹窗关闭时释放页图临时地址。 */ () => URL.revokeObjectURL(pageUrl), {once: true});
          /** 在原 PDF 页图上绘制引用区域边框，并释放页图临时地址。 */
          image.onload = () => {
            canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(image, 0, 0);
            ctx.strokeStyle = '#f97316'; ctx.lineWidth = 4;
            const [x1, y1, x2, y2] = location.bbox;
            ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
            URL.revokeObjectURL(pageUrl);
          };
          /** 显示原页图加载失败提示，并释放临时图片地址。 */
          image.onerror = () => {
            const error = document.createElement('p');
            error.textContent = '原页图加载失败，请重试或打开原 PDF；上方文字不是原页图。';
            canvas.replaceWith(error);
            URL.revokeObjectURL(pageUrl);
          };
          image.src = pageUrl;
          dialog.append(canvas);
        }
      } catch (error) {
        if (text.textContent) {
          const warning = document.createElement('p');
          warning.textContent = error.message;
          dialog.append(warning);
        } else text.textContent = error.message;
      }
    }
  }


  document.getElementById('sendBtn').addEventListener('click', /** 根据当前活动任务，将按钮点击分派为停止或发送。 */ () => {
    if (agentState.active) { agentState.stop?.(); return; }
    if (chatUpload?.chatId === currentChatId || (_chatRequest?.chatId === currentChatId && (_chatRequest.status === 'running' || _chatRequest.stopping))) {
      stopChatRequest();
    } else handleSend();
  });
  document.getElementById('chatInput').addEventListener('keydown', /** 按 Enter 发送消息，保留 Shift 与 Enter 组合用于换行。 */ (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  });

  // 联网搜索切换按钮(toggle,和自动化按钮同模式)
  const _searchBtn = document.getElementById('webSearchBtn');
  const _searchToggle = document.getElementById('webSearchToggle');
  if (_searchBtn && _searchToggle) {
    _searchBtn.addEventListener('click', /** 切换联网搜索状态并同步按钮样式。 */ () => {
      _searchToggle.checked = !_searchToggle.checked;
      _searchBtn.classList.toggle('is-active', _searchToggle.checked);
    });
  }
  /** 读取输入区联网搜索开关的当前状态。 */
  function isSearchMode() {
    const t = document.getElementById('webSearchToggle');
    return t && t.checked;
  }

  // 会话抽屉:开/关/遮罩/新建
  document.getElementById('openSessionsBtn')?.addEventListener('click', openDrawer);
  document.getElementById('closeSessionsBtn')?.addEventListener('click', closeDrawer);
  document.getElementById('drawerMask')?.addEventListener('click', closeDrawer);
  document.getElementById('newSessionBtn')?.addEventListener('click', /** 清空消息展示、创建新会话并关闭历史抽屉。 */ async () => {
    document.getElementById('chatHistory').replaceChildren();
    await resetCurrentChatId();
    closeDrawer();
  });

  // 记忆面板:刷新 / 添加(添加支持 Enter)/ 一键整理(P3)
  document.getElementById('refreshMemoryBtn')?.addEventListener('click', /** 响应用户操作刷新记忆列表。 */ () => loadMemoryPanel());
  document.getElementById('memoryAddBtn')?.addEventListener('click', addMemory);
  document.getElementById('memoryAddInput')?.addEventListener('keydown', /** 在记忆输入框按 Enter 时提交新增记忆。 */ (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addMemory(); }
  });
  document.getElementById('rethinkMemoryBtn')?.addEventListener('click', /** 响应用户操作启动记忆整理。 */ () => rethinkMemories());
  document.getElementById('rethinkModalCloseBtn')?.addEventListener('click', /** 关闭记忆整理弹窗并重置界面状态。 */ () => {
    const m = document.getElementById('rethinkModal');
    if (m) m.style.display = 'none';
    _rethinkState = 'idle';
  });

  document.getElementById('uploadImageBtn').addEventListener('click', /** 打开图片文件选择器。 */ () => {
    document.getElementById('imageInput').click();
  });

  document.getElementById('imageInput').addEventListener('change', /** 按当前模式处理所选图片，失败时提示并清理相应状态。 */ async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;

    try {
      if (shouldUseAgent(document.getElementById('chatInput').value)) await setAttachedImage(file);
      else await selectChatImage(Array.from(event.target.files));
    } catch (error) {
      alert(error.message || '图片读取失败');
      if (shouldUseAgent(document.getElementById('chatInput').value)) clearAttachedImage();
    }
  });

  document.getElementById('clearImageBtn').addEventListener('click', /** 清除当前模式的待发送图片。 */ () => {
    if (shouldUseAgent(document.getElementById('chatInput').value)) clearAttachedImage(); else clearChatImage();
  });
  document.getElementById('imagePreviewImg').addEventListener('click', /** 仅在聊天模式下打开输入区图片的大图预览。 */ event => {
    if (!shouldUseAgent(document.getElementById('chatInput').value) && chatImage) ChatImages.enlarge(event.target.src);
  });
  let composerWasAgent = false;
  document.getElementById('chatInput').addEventListener('input', /** 输入导致模式变化时使旧附件处理失效，并刷新预览与发送按钮。 */ () => {
    const isAgent = shouldUseAgent(document.getElementById('chatInput').value);
    if (isAgent !== composerWasAgent) { chatAttachmentEpoch++; chatImageProcessing = false; cancelChatUpload(); }
    composerWasAgent = isAgent; renderComposerImage(); updateSendButton();
  });
  const composer = document.querySelector('.composer');
  composer.addEventListener('dragover', /** 允许聊天模式接收文件拖放，避免浏览器直接打开文件。 */ event => {
    if (!shouldUseAgent(document.getElementById('chatInput').value) && Array.from(event.dataTransfer.types).includes('Files')) event.preventDefault();
  });
  composer.addEventListener('drop', /** 在聊天模式下读取拖入图片并报告准备失败。 */ async event => {
    if (shouldUseAgent(document.getElementById('chatInput').value) || !event.dataTransfer.files.length) return;
    event.preventDefault();
    try { await selectChatImage(Array.from(event.dataTransfer.files)); } catch (error) { alert(error.message); }
  });
  window.addEventListener('pagehide', invalidateChatAttachments);

  document.getElementById('chatInput').addEventListener('paste', /** 按聊天或自动化模式处理粘贴的图片文件或 data URL，保留伴随文本。 */ async (event) => {
    if (!shouldUseAgent(document.getElementById('chatInput').value)) {
      const files = Array.from(event.clipboardData?.items || []).filter(/** 筛选剪贴板中的文件项。 */ item => item.kind === 'file').map(/** 把剪贴板文件项转换为文件对象。 */ item => item.getAsFile()).filter(Boolean);
      if (!files.length) return;
      event.preventDefault();
      const text = event.clipboardData.getData('text/plain');
      if (text) { const input = event.target; input.setRangeText(text, input.selectionStart, input.selectionEnd, 'end'); }
      try { await selectChatImage(files); } catch (error) { alert(error.message); }
      return;
    }
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
  document.getElementById('clearChatBtn')?.addEventListener('click', /** 清空当前展示和输入附件，再创建新的会话标识。 */ async () => {
    document.getElementById('chatHistory').replaceChildren();
    document.getElementById('chatInput').value = '';
    clearAttachedImage();
    await resetCurrentChatId();
  });

  /** 判断页面 URL 是否使用支持读取的 HTTP 或 HTTPS 协议。 */
  function isReadablePageUrl(url) {
    try {
      return ['http:', 'https:'].includes(new URL(url).protocol);
    } catch {
      return false;
    }
  }

  /** 根据已知站点和主机名特征识别可能包含敏感内容的页面。 */
  function looksSensitivePageUrl(url) {
    try {
      const hostname = new URL(url).hostname.toLowerCase();
      return [
        'mail.google.com',
        'docs.google.com',
        'drive.google.com',
        'slack.com',
        'notion.so'
      ].some(/** 匹配已知敏感站点及其子域名。 */ (domain) => hostname === domain || hostname.endsWith(`.${domain}`))
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

  /** 根据自动化命令前缀或模式开关判断本次输入是否启动浏览器任务。 */
  function shouldUseAgent(text) {
    const trimmed = text.trim();
    // 方式1: /browser-operation 前缀触发
    if (trimmed.startsWith(AGENT_COMMAND.trim())) return true;
    // 方式2: 自动化开关打开时，所有输入走 agent
    const toggle = document.getElementById('agentModeToggle');
    if (toggle && toggle.checked) return true;
    return false;
  }

  /** 在目标页等待 DOM 短暂稳定或达到时限，帮助减少动作后立即观察的瞬时变化。 */
  async function waitForPageSettle(tabId, timeoutMs) {
    const timeout = timeoutMs || AGENT_SETTLE_TIMEOUT_MS;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId, allFrames: false },
        /** 在页面上下文观察 DOM 变化，以静默间隔和最长等待时间决定何时返回。 */
        func: (maxWait) => {
          return new Promise(/** 建立 DOM 静默观察与最长等待时间的异步结果。 */ resolve => {
            let timer = null;
            let settled = false;
            const observer = new MutationObserver(/** DOM 发生变化时重新计算静默等待间隔。 */ () => {
              clearTimeout(timer);
              timer = setTimeout(/** 静默间隔达到后解除观察并报告页面已稳定。 */ () => {
                observer.disconnect();
                settled = true;
                resolve(true);
              }, 300);
            });
            observer.observe(document.body || document.documentElement, {
              childList: true, subtree: true,
              attributes: true, characterData: true
            });
            timer = setTimeout(/** 没有后续 DOM 变化时结束初始静默等待。 */ () => {
              observer.disconnect();
              if (!settled) resolve(true);
            }, 300);
            setTimeout(/** 达到最长等待时间后解除观察并报告未等到稳定。 */ () => {
              if (!settled) { observer.disconnect(); resolve(false); }
            }, maxWait);
          });
        },
        args: [timeout]
      });
      return results?.[0]?.result !== false;
    } catch {
      await new Promise(/** 页面脚本无法执行时进行短暂等待。 */ r => setTimeout(r, 500));
      return true;
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  // Set-of-Marks 截图标注（对齐 browser-use python_highlights.py）
  // 在裸截图上叠加"元素编号框"，让 LLM 看到的视觉元素与它能填的 index 一一对应。
  // 没有这层标注，多模态反而有害：模型看得见按钮却猜不准编号，会反复点错。
  // ═══════════════════════════════════════════════════════════════════════════


  /** 根据当前后端基址构造自动化接口地址。 */
  function buildAgentApiUrl(safeApiUrl, path) {
    return buildBackendEndpointUrl(safeApiUrl, path);
  }

  /** 将自动化参数编码为带鉴权的 POST 请求，通过扩展后台发送。 */
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

  /** 创建自动化执行进度区域并渲染当前计划。 */
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

  /** 刷新执行计划，并把已有动作记录保留在新的当前目标下。 */
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

  /** 渲染已完成目标、当前目标和剩余工作。 */
  function _renderPlanContent(container, plan) {
    // 已完成目标
    if (plan.completed_goals && plan.completed_goals.length > 0) {
      plan.completed_goals.forEach(/** 将已完成目标逐项渲染到计划区域。 */ g => {
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

  /** 展示单步意图、动作和核验结果，优先归入当前执行目标。 */
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
      if (action.params?.text) actionDesc += ` (文本长度: ${action.params.text.length})`;
      if (action.params?.direction) actionDesc += ` (${action.params.direction})`;
      if (action.params?.key) actionDesc += ` (${action.params.key})`;
      actionEl.textContent = actionDesc;
      stepDiv.appendChild(actionEl);
    }

    if (result) {
      const resultEl = document.createElement('div');
      const unconfirmed = ['not_satisfied', 'unknown'].includes(result.verification?.status) ||
        (result.target_probe && result.target_probe.clear !== true);
      resultEl.className = result.success && !unconfirmed ? 'agent-result-success' : 'agent-result-fail';
      resultEl.textContent = result.success
        ? `${unconfirmed ? '⚠' : '✓'} ${result.details || '操作已返回'}`
        : `✗ ${result.error || '失败'}`;
      if (result.verification?.kind) {
        const label = { satisfied: '局部条件已确认', not_satisfied: '局部条件尚未满足', unknown: '局部结果尚不能确认' }[result.verification.status];
        resultEl.textContent += result.verification.reason === 'invalid_expectation'
          ? `；附加验证条件无效：${result.verification.details || '未核实局部结果'}`
          : `；${label}（${result.verification.kind}）`;
      }
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

  /** 根据任务成功状态展示完成或未完成提示及 Markdown 总结。 */
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

  /** 在自动化消息中追加错误提示并滚动到最新内容。 */
  function renderAgentError(bubble, error) {
    const errorEl = document.createElement('div');
    errorEl.className = 'agent-error';
    errorEl.textContent = `❌ 错误: ${error}`;
    bubble.appendChild(errorEl);
    scrollToBottom();
  }

  /** 展示待执行动作及覆盖信息，等待用户允许或取消。 */
  function showAgentConfirmDialog(bubble, action, thought) {
    return new Promise(/** 创建动作确认界面，并将允许或取消结果返回等待中的任务。 */ (resolve) => {
      const confirmDiv = document.createElement('div');
      confirmDiv.className = 'agent-confirm';

      const msgEl = document.createElement('div');
      msgEl.className = 'agent-confirm-msg';
      let desc = `⚠️ 即将执行: ${action.type}`;
      if (action.index != null) desc += ` → [${action.index}]`;
      if (thought) desc += `\n原因: ${thought}`;
      if (action.params?.confirm_overwrite) desc += `\n字段“${action.params.material_label}”已有值。\n${action.params.expected_prior_complete === false ? '现值预览（非全文，执行前会校验完整值）' : '现值'}：${action.params.expected_prior}\n物料值：${action.params.text ?? action.params.option_text}\n是否允许替换？`;
      msgEl.textContent = desc;
      confirmDiv.appendChild(msgEl);

      const btnContainer = document.createElement('div');
      btnContainer.className = 'agent-confirm-btns';

      const allowBtn = document.createElement('button');
      allowBtn.textContent = '允许';
      allowBtn.className = 'agent-btn-allow';
      /** 移除动作确认界面并返回用户允许的结果。 */
      allowBtn.onclick = () => { confirmDiv.remove(); resolve(true); };

      const denyBtn = document.createElement('button');
      denyBtn.textContent = '取消';
      denyBtn.className = 'agent-btn-deny';
      /** 移除动作确认界面并返回用户取消的结果。 */
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
    /** 组合元素标签、角色、短文本和名称，用于识别新展开的元素。 */
    const sig = (e) => `${e.tag}|${e.role}|${(e.text || '').slice(0, 20)}|${e.name}`;
    const prevSet = new Set(prevEls.map(sig));
    const added = newEls.filter(/** 识别上一轮观察中不存在的新交互元素。 */ e => !prevSet.has(sig(e)));
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

  /** 组织任务启动、观察、决策、确认、执行和核验循环，结束时取消未完成工作并释放资源。 */
  async function runAgentTask(task, taskImage = '') {
    const sessionId = `agent_${createMessageId()}`;
    Object.assign(agentState, { active: true, sessionId, task, status: 'running', currentStep: 0 });
    /** 检查当前任务是否仍拥有侧边栏状态，避免旧任务更新新任务界面。 */
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
    aiBubble.appendChild(header);
    scrollToBottom();

    let run = null;
    let keepalivePort = null;
    let stopRequested = false;
    let backendTerminal = false;
    let taskFailed = false;
    /** 登记停止意图，更新界面并请求运行器取消后续工作。 */
    agentState.stop = () => {
      if (!ownsUI() || stopRequested) return;
      stopRequested = true;
      statusText.textContent = '正在停止后续动作并释放输入…';
      if (ownsUI()) agentState.status = 'stopping';
      updateSendButton();
      if (run) run.stop().catch(/** 消费停止请求的异常，后续由任务收尾确认状态。 */ () => {});
    };
    updateSendButton();

    try {
      keepalivePort = chrome.runtime.connect({ name: 'agent-keepalive' });
      keepalivePort.onDisconnect.addListener(/** 保活端口断开时不直接派发额外任务操作。 */ () => {});
      const tab = await getActiveBrowserTab();
      if (!tab?.id) throw new Error('无法获取当前标签页');
      const { apiKey, modelName, safeApiUrl } = await resolveApiRequestConfig();
      await requireServerContext(safeApiUrl);
      const kbId = window._kbBoundId || '';
      const interactionCapability = await callAgentApi(safeApiUrl, '/v1/agent/capabilities', {}, apiKey);
      if (interactionCapability.interaction_version !== 1 || interactionCapability.action_verification_version !== 1 || interactionCapability.autonomous_recovery_version !== 1) {
        throw new Error('点击诊断协议不匹配，请重启新后端并重新加载扩展');
      }
      if (kbId) {
        const capability = await callAgentApi(safeApiUrl, '/v1/agent/capabilities', {}, apiKey);
        if (capability.material_fill_version !== 1 || capability.task_workspace_version !== 2 || capability.field_verification_version !== 1 || capability.field_snapshot_version !== 1 || capability.field_read_version !== 1) throw new Error('字段核实协议不匹配，请更新并重启后端，同时重新加载扩展');
      }
      let llmParams = {};
      try {
        const stored = await chrome.storage.local.get(['agentLlmParams']);
        const parsed = JSON.parse(stored.agentLlmParams || '{}');
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) llmParams = parsed;
      } catch { /* 保持已有默认参数 */ }

      run = new AgentRunner.Run(sessionId, tab.id, {
        /** 把运行器控制消息转发给扩展后台。 */
        message: message => chrome.runtime.sendMessage(message),
        /** 使用本次固定的后端地址和凭证发送运行器接口请求。 */
        api: (path, body) => callAgentApi(safeApiUrl, path, body, apiKey),
        /** 为本轮页面截图绘制交互元素编号。 */
        annotate: page => AgentObservation.annotate(page),
        /** 仅在任务仍拥有界面且未停止时更新自动化状态。 */
        status: (state, text) => {
          if (!ownsUI() || stopRequested) return;
          agentState.status = state;
          statusText.textContent = text;
        }
      }, { interactionVersion: 1, verificationVersion: 1, recoveryVersion: 1 });
      if (stopRequested) { await run.stop(); run.check(); }
      await run.start();
      let pageState = await run.observe();
      let response = await run.decide('/v1/agent/execute', {
        task, page_state: pageState, model: modelName || 'gpt-4o',
        require_confirmation: [], task_image: taskImage || '', llm_params: llmParams,
        kb_id: kbId, materials_version: 1, task_workspace_version: 2
      });

      while (['action_required', 'confirm_required', 'observation_required'].includes(response.status)) {
        run.check();
        if (!ownsUI()) throw new Error('任务已被替换');
        renderAgentMaterials(aiBubble, response.materials, response.task_workspace);
        if (response.status === 'observation_required') {
          statusText.textContent = response.progress || '资料处理完成，正在重新观察页面';
          pageState = await run.observe();
          response = await run.decide('/v1/agent/resume', {
            page_state: pageState, continuation_id: response.continuation_id
          });
          continue;
        }
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

        // 等稳仅帮助减少瞬时变化；只有后续成功的新观察才允许推进。
        await run.wait(waitForPageSettle(tab.id), 5000).catch(/** 允许页面等稳超时后重新观察，其他错误继续向上传递。 */ error => {
          if (error.code !== 'timeout') throw error;
        });
        run.check();
        let nextPage = await run.observe();
        nextPage = await run.verify(action, result, nextPage);
        renderAgentStepInBubble(aiBubble, response.step, null, null, result);
        if (!nextPage.active_popup) {
          const inlineGroup = detectInlineGroup(pageState.interactive_elements || [], nextPage.interactive_elements || []);
          if (inlineGroup) {
            nextPage.active_popup = inlineGroup;
            /** 生成元素比较标识，用于标记新出现的行内展开组成员。 */
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
      renderAgentMaterials(aiBubble, response.materials, response.task_workspace);
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
        taskFailed = true;
        statusText.textContent = '任务未完成';
        renderAgentError(aiBubble, error.message || '执行失败');
      }
    } finally {
      if (run && !backendTerminal) await run.stop();
      const cleanup = run ? await run.finish() : { safe: true };
      if (cleanup.safe === false) {
        if (['execution_unknown', 'action_pending'].includes(cleanup.reason)) {
          statusText.textContent = '旧动作尚未确认结束，已阻止后续输入';
          renderAgentError(aiBubble, cleanup.reason === 'action_pending'
            ? '后台会在旧动作确认结束后继续收尾；不会自动重放动作。'
            : '请检查页面；旧动作执行状态未知，不会自动重放动作。');
        } else {
          statusText.textContent = cleanup.reason === 'startup_unconfirmed' ? '启动状态未确认' : '任务收尾未完成';
          renderAgentError(aiBubble, cleanup.reason === 'cleanup_failed'
            ? '动作执行链已结束，但资源清理未完成；再次发起任务时会先重试清理。'
            : '未能确认扩展后台的收尾状态，请检查扩展通信；这不代表存在未结束的页面动作。');
        }
      } else if (!taskFailed && (stopRequested || run?.abort.signal.aborted && !backendTerminal)) {
        statusText.textContent = stopRequested ? '已停止；已发出的操作不会回滚' : '任务已结束';
      }
      if (ownsUI()) Object.assign(agentState, { active: false, status: 'idle', stop: null });
      aiBubble.querySelectorAll('.agent-confirm').forEach(/** 任务结束时移除遗留的动作确认界面。 */ element => element.remove());
      updateSendButton();
      if (keepalivePort) { try { keepalivePort.disconnect(); } catch { /* 已断开 */ } }
    }
  }

  /** 展示来源、成果、字段填写及修订审核状态，区分资料可用和已回读确认。 */
  function renderAgentMaterials(bubble, materials, workspace) {
    if (!materials && !workspace) return;
    let panel = bubble.querySelector('.agent-materials');
    if (!panel) {
      panel = document.createElement('details'); panel.className = 'agent-materials';
      bubble.appendChild(panel);
    }
    panel.replaceChildren();
    const heading = document.createElement('summary');
    const fields = materials?.fields || [];
    heading.textContent = `📚 任务资料：${workspace?.source_count || 0} 条来源 · ${workspace?.artifacts?.length || 0} 份成果` +
      (fields.length ? ` · 填写 ${fields.filter(/** 统计已通过字段回读确认的填写项。 */ f => f.fill_status === 'verified').length}/${fields.length} 已回读` : '');
    panel.appendChild(heading);
    for (const artifact of workspace?.artifacts || []) {
      const row = document.createElement('div');
      const state = artifact.status !== 'available' ? '来源失效' : artifact.missing?.length ? '仍有缺项' : '可复用';
      const origin = {verbatim:'原文摘取',assembled:'原文组合',user_input:'用户原文',derived:'整理稿'}[artifact.origin] || '派生成果';
      row.textContent = `${artifact.id} · ${artifact.subject} / ${artifact.purpose}：${state}（${origin}）`;
      if (artifact.length_constraint) {
        const limit = artifact.length_constraint;
        row.textContent += ` · 限长稿 ${limit.actual_length}/${limit.max_length}，原稿 ${limit.source_artifact_id} 保留`;
      }
      if (artifact.field_intent) row.textContent += ` · 待填字段：${artifact.field_intent.field_label}；内容范围：${artifact.field_intent.scope}`;
      panel.appendChild(row);
    }
    if (workspace?.evidence_view?.archived_count != null) {
      const view = workspace.evidence_view;
      const row = document.createElement('div');
      row.textContent = `已归档 ${view.archived_count} 个证据单元；当前可见原文 ${view.raw_citations?.length || 0} 个、派生摘要 ${view.derived_citations?.length || 0} 个。归档不代表当前已读，也不代表页面操作完成。`;
      panel.appendChild(row);
    }
    const states = { available: '有来源', missing: '缺失', conflict: '冲突', ambiguous: '待澄清', pending: '正在取材', invalid: '来源失效' };
    for (const field of fields) {
      const row = document.createElement('div');
      row.textContent = `${field.material_id || field.field_id} · ${field.entity} / ${field.label}：${states[field.status] || field.status}` +
        (field.status === 'available' ? `\n原值：${field.raw_value}\n填充值：${field.value} · ${field.fill_status === 'verified' ? '已确认' : '未确认'}` : `\n${field.reason || ''}`);
      row.style.whiteSpace = 'pre-wrap';
      for (const source of field.sources || []) {
        const evidence = document.createElement('div');
        evidence.textContent = `来源：${source.title || source.doc_id} · KB${source.citation}\n${source.quote}`;
        row.appendChild(evidence);
      }
      panel.appendChild(row);
    }
    if (materials?.error || workspace?.error) {
      const error = document.createElement('div'); error.textContent = workspace?.error || materials.error; panel.appendChild(error);
    }
    for (const failure of workspace?.pending_saves || []) {
      const row = document.createElement('div');
      row.textContent = `成果待修正：${failure.subject || ''} / ${failure.purpose || ''} · ${failure.error}；${failure.hint}`;
      panel.appendChild(row);
    }
    for (const repair of workspace?.pending_length_repairs || []) {
      const row = document.createElement('div');
      row.textContent = `${repair.field_label}：原稿 ${repair.artifact_id} 长度 ${repair.actual_length}，字段上限 ${repair.max_length}（UTF-16）；` +
        (repair.status === 'prepared' ? `精简稿 ${repair.result_artifact_id} 已生成，待填写回读` : '正在精简待填内容，原稿保留，尚未写入');
      panel.appendChild(row);
    }
    for (const repair of workspace?.pending_content_repairs || []) {
      const row = document.createElement('div');
      row.textContent = `${repair.field_label}：` + (repair.status === 'prepared'
        ? `待填稿 ${repair.result_artifact_id} 已生成，待填写回读`
        : repair.status === 'blocked' ? '连续修稿未通过，此字段未完成，未继续写入'
        : '正在按字段整理内容、检查重复；原资料保留，未写入');
      panel.appendChild(row);
    }
    const reviewStates = {pending:'待独立语义审核，未放行填写', approved:'审核通过，待填写回读',
      resolved:'审核通过且已回读', needs_revision:'需要修稿，未放行填写', uncertain:'依据不足，待澄清',
      blocked:'修稿次数已到上限，此字段未完成', error:'审核未完成，不代表内容不合格'};
    for (const review of workspace?.field_reviews || []) {
      const row = document.createElement('div');
      row.textContent = `${review.field_label} · ${review.artifact_id}：${reviewStates[review.status] || review.status}` +
        (review.cached ? '（复用已有审核）' : '') + (review.error ? `；${review.error}` : '');
      for (const issue of review.issues || []) {
        const detail = document.createElement('div');
        detail.textContent = `${(issue.draft_ids || []).join('、')} ${(issue.ranges || []).map(/** 格式化字段审核问题对应的字符范围。 */ r => `[${r[0]},${r[1]})`).join('、')}：${issue.reason}；修改要求：${issue.required_change}`;
        row.appendChild(detail);
      }
      panel.appendChild(row);
    }
  }

  // 拦截发送：如果用户输入匹配 agent 关键词，走 agent 流程而非普通聊天
  (/** 绑定自动化模式切换及斜杠命令菜单，初始化输入区相关交互。 */ function installAgentInterceptor() {
    const inputEl = document.getElementById('chatInput');
    const btnEl = document.getElementById('sendBtn');
    const toggleEl = document.getElementById('agentModeToggle');
    const agentBtn = document.getElementById('agentModeBtn');
    if (!inputEl || !btnEl) return;

    // 按钮点击切换自动化模式
    if (agentBtn && toggleEl) {
      agentBtn.addEventListener('click', /** 切换自动化模式，使旧附件处理失效并同步输入区提示。 */ () => {
        chatAttachmentEpoch++; chatImageProcessing = false; cancelChatUpload();
        toggleEl.checked = !toggleEl.checked;
        composerWasAgent = toggleEl.checked;
        renderComposerImage(); updateSendButton();
        agentBtn.classList.toggle('is-active', toggleEl.checked);
        inputEl.placeholder = toggleEl.checked
          ? '输入自动化指令 (如: 帮我点击搜索按钮)...'
          : '输入问题 (Enter发送, Shift+Enter换行)...';
      });
    }

    // ═══ 斜杠命令菜单 ═══
    let slashMenu = null;

    /** 按命令配置创建可复用的斜杠菜单，并绑定选择后填入输入框的行为。 */
    function createSlashMenu() {
      if (slashMenu) return slashMenu;
      slashMenu = document.createElement('div');
      slashMenu.className = 'slash-menu';
      slashMenu.style.display = 'none';
      SLASH_COMMANDS.forEach(/** 为每个斜杠命令创建菜单项和选择行为。 */ (cmd, i) => {
        const item = document.createElement('div');
        item.className = 'slash-menu-item';
        item.dataset.index = i;
        item.innerHTML = `<span class="slash-menu-cmd">${cmd.name}</span><span class="slash-menu-desc">${cmd.description}</span>`;
        item.addEventListener('click', /** 将选中命令填入输入框并触发状态刷新，然后关闭菜单。 */ () => {
          inputEl.value = cmd.name + ' ';
          inputEl.dispatchEvent(new Event('input'));
          inputEl.focus();
          hideSlashMenu();
        });
        slashMenu.appendChild(item);
      });
      inputEl.parentElement.style.position = 'relative';
      inputEl.parentElement.appendChild(slashMenu);
      return slashMenu;
    }

    /** 按当前输入过滤斜杠命令，仅在存在匹配项时展示菜单。 */
    function showSlashMenu(filter) {
      const menu = createSlashMenu();
      const items = menu.querySelectorAll('.slash-menu-item');
      let hasVisible = false;
      items.forEach(/** 按当前筛选文本更新命令项可见性。 */ (item, i) => {
        const cmd = SLASH_COMMANDS[i];
        const show = !filter || cmd.name.includes(filter);
        item.style.display = show ? 'flex' : 'none';
        if (show) hasVisible = true;
      });
      menu.style.display = hasVisible ? 'block' : 'none';
    }

    /** 隐藏已创建的斜杠命令菜单。 */
    function hideSlashMenu() {
      if (slashMenu) slashMenu.style.display = 'none';
    }

    inputEl.addEventListener('input', /** 在输入斜杠命令前缀时展示匹配菜单，其余输入关闭菜单。 */ () => {
      const val = inputEl.value;
      if (val.startsWith('/') && !val.includes(' ')) {
        showSlashMenu(val);
      } else {
        hideSlashMenu();
      }
    });

    inputEl.addEventListener('blur', /** 输入框失焦后延迟隐藏菜单，使点击选择有机会完成。 */ () => {
      setTimeout(hideSlashMenu, 150);
    });

    // ═══ 发送拦截 ═══
    /** 识别自动化输入并阻止原发送事件，再去除命令前缀启动任务。 */
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

  const savedChat = await chrome.storage.session.get([CURRENT_CHAT_ID_KEY]);
  if (savedChat[CURRENT_CHAT_ID_KEY] && !currentChatId && !_sendingLock) {
    await resumeSession(savedChat[CURRENT_CHAT_ID_KEY]);
  }
});


// ═══════════════════════════════════════════════════════════════════
// KB 绑定 chip + 选择器(聊天 tab 内)
// ═══════════════════════════════════════════════════════════════════
(/** 恢复会话的知识库绑定，并初始化绑定标签、选择器及变更事件。 */ function initKbChip() {
  const API_BASE = 'http://localhost:8000';
  let _kbListCache = [];

  window._kbBoundId = sessionStorage.getItem('kb_id') || '';

  /** 获取并缓存可用知识库列表，读取失败时清空缓存。 */
  async function _fetchKbList() {
    try {
      const res = await fetch(`${API_BASE}/v1/kb`);
      _kbListCache = await res.json();
    } catch { _kbListCache = []; }
    return _kbListCache;
  }

  /** 利用文本节点转义知识库名称或描述中的 HTML 字符。 */
  function _escHtml(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

  /** 显示当前绑定知识库的名称，找不到对应知识库时清除失效绑定。 */
  async function renderKbChip() {
    const bar = document.getElementById('kb-chip-bar');
    const nameSpan = document.getElementById('kb-chip-name');
    if (!bar || !nameSpan) return;

    if (!window._kbBoundId) {
      bar.style.display = 'none';
      return;
    }

    if (!_kbListCache.length) await _fetchKbList();
    const kb = _kbListCache.find(/** 从缓存列表中查找当前绑定知识库。 */ k => k.kb_id === window._kbBoundId);
    if (kb) {
      nameSpan.textContent = kb.name;
      bar.style.display = 'flex';
    } else {
      window._kbBoundId = '';
      sessionStorage.removeItem('kb_id');
      bar.style.display = 'none';
    }
  }

  /** 刷新知识库列表并展示选择窗口，标出当前绑定项。 */
  async function showKbSelector() {
    const selector = document.getElementById('kbSelector');
    const list = document.getElementById('kbSelectorList');
    if (!selector || !list) return;

    await _fetchKbList();
    if (!_kbListCache.length) {
      list.innerHTML = '<div class="kb-selector-empty">暂无知识库，请先在 📚 知识库 tab 创建</div>';
    } else {
      list.innerHTML = _kbListCache.map(/** 为每个可选知识库生成名称、描述和选中状态。 */ kb => `
        <div class="kb-selector-item ${kb.kb_id === window._kbBoundId ? 'active' : ''}" data-kb-id="${kb.kb_id}">
          <div class="kb-selector-item-name">${_escHtml(kb.name)}</div>
          <div class="kb-selector-item-desc">${_escHtml(kb.description || '')}</div>
        </div>
      `).join('');
    }
    selector.style.display = 'flex';
  }

  /** 隐藏知识库选择窗口。 */
  function hideKbSelector() {
    const s = document.getElementById('kbSelector');
    if (s) s.style.display = 'none';
  }

  document.getElementById('kbSelectBtn')?.addEventListener('click', showKbSelector);
  document.getElementById('kbSelectorCancel')?.addEventListener('click', hideKbSelector);

  document.getElementById('kbSelectorList')?.addEventListener('click', /** 保存用户选择的知识库绑定，关闭选择器并刷新标签。 */ (e) => {
    const item = e.target.closest('.kb-selector-item');
    if (!item) return;
    window._kbBoundId = item.dataset.kbId;
    sessionStorage.setItem('kb_id', window._kbBoundId);
    hideKbSelector();
    renderKbChip();
  });

  document.getElementById('kb-chip-remove')?.addEventListener('click', /** 清除当前知识库绑定并隐藏标签。 */ () => {
    window._kbBoundId = '';
    sessionStorage.removeItem('kb_id');
    renderKbChip();
  });

  window.addEventListener('kb-changed', /** 接收知识库管理区的绑定变化并同步会话存储和标签。 */ (e) => {
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
(/** 初始化知识库、文档和回收站界面，绑定上传、恢复、发布及删除操作。 */ function initKB() {
  let currentKbId = sessionStorage.getItem('kb_id') || '';
  let kbs = [];
  let docs = [];
  let viewMode = 'active';
  let trashKbs = [];
  const _busy = new Map();  // key → {text, done}  操作中状态追踪

  const API_BASE = 'http://localhost:8000';

  /** 调用知识库接口并解析 JSON，将 HTTP 或业务失败转换为异常。 */
  async function kbRequest(path, options) {
    const response = await fetch(`${API_BASE}${path}`, options);
    const data = await response.json();
    if (!response.ok || data?.ok === false) {
      throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${response.status}`);
    }
    return data;
  }

  /** 加载知识库列表并渲染，返回本次加载是否成功。 */
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

  /** 渲染知识库卡片、当前选中状态和正在进行的管理操作。 */
  function renderKBList() {
    const container = document.getElementById('kbList');
    if (!container) return;
    container.innerHTML = kbs.map(/** 渲染知识库卡片，并反映当前选中及删除中状态。 */ kb => {
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

  document.getElementById('kbRefreshBtn')?.addEventListener('click', /** 按当前视图刷新知识库或回收站，并在需要时刷新当前文档列表。 */ async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = '刷新中…';
    try {
      let loaded = viewMode === 'trash' ? await loadTrash() : await loadKBs();
      if (loaded && viewMode !== 'trash' && currentKbId) loaded = await loadDocs(currentKbId);
      btn.textContent = loaded ? '↻ 刷新' : '刷新失败，重试';
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById('createKbBtn')?.addEventListener('click', /** 收集名称与描述后创建知识库，显示成功或失败状态。 */ async () => {
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
      setTimeout(/** 创建成功后恢复按钮并刷新知识库列表。 */ () => { btn.disabled = false; btn.textContent = '+ 新建知识库'; loadKBs(); }, 800);
    } catch (err) {
      btn.textContent = '创建失败';
      setTimeout(/** 创建失败提示结束后恢复按钮。 */ () => { btn.disabled = false; btn.textContent = '+ 新建知识库'; }, 1500);
    }
  });

  document.getElementById('kbList')?.addEventListener('click', /** 处理知识库卡片选择或确认后的删除，并同步绑定与文档区域。 */ async (e) => {
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
        setTimeout(/** 删除成功提示结束后清除忙状态并刷新列表。 */ () => { _busy.delete('kb-del-' + kb_id); loadKBs(); }, 800);
      } catch (err) {
        _busy.set('kb-del-' + kb_id, {text: '删除失败'});
        renderKBList();
        setTimeout(/** 删除失败提示结束后恢复卡片操作状态。 */ () => { _busy.delete('kb-del-' + kb_id); renderKBList(); }, 1500);
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
      const kb = kbs.find(/** 查找当前选中知识库的名称。 */ k => k.kb_id === currentKbId);
      document.getElementById('currentKbName').textContent = kb ? kb.name : '';
    }
  });

  document.getElementById('uploadDocBtn')?.addEventListener('click', /** 打开知识库文档选择器。 */ () => {
    document.getElementById('fileInput').click();
  });

  document.getElementById('fileInput')?.addEventListener('change', /** 校验文件大小并上传到固定知识库，随后加载文档与处理进度。 */ async (e) => {
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

  /** 加载指定知识库的文档列表，对尚未完成的处理任务启动状态轮询。 */
  async function loadDocs(kb_id) {
    try {
      const loaded = await kbRequest(`/v1/kb/${kb_id}/docs`);
      if (currentKbId !== kb_id) return true;
      docs = loaded;
      renderDocList();
      for (const doc of docs) {
        if (['queued', 'parsing', 'indexing'].includes(doc.phase)) pollDocStatus(kb_id, doc.doc_id);
      }
      return true;
    } catch (err) {
      console.error('加载文档失败:', err);
      return false;
    }
  }

  /** 展示文档解析、索引与发布状态，并按状态提供恢复、取消和目录入口。 */
  function renderDocList() {
    const container = document.getElementById('docList');
    if (!container) return;
    container.innerHTML = docs.map(/** 按文档处理、同步和发布状态渲染文档卡片及可用操作。 */ doc => {
      const b = _busy.get('doc-del-' + doc.doc_id);
      const btnText = b ? b.text : '删除';
      const btnDis = b ? 'disabled' : '';
      return `
      <div class="doc-item" data-doc-id="${doc.doc_id}">
        <span class="doc-name">${escapeHtml(doc.filename)}</span>
        <span class="doc-status">${escapeHtml(doc.phase ? ({queued: '已保存原文件，排队中', parsing: doc.file_type === 'pdf' ? `解析 ${doc.completed_pages}/${doc.page_count} 页` : '正在解析文本结构', indexing: `索引 ${doc.indexed_chunks}/${doc.total_chunks} 片段`, published: '已发布可检索版本', failed: '处理失败，可恢复', interrupted: '处理已中断，可恢复', cancelled: '已取消，检查点保留'}[doc.phase] || doc.phase) + (doc.published_available && doc.phase !== 'published' ? '（旧版本仍可用）' : '') : doc.status === 'pending' ? '⏳ 索引中' : doc.sync_pending ? (doc.status === 'failed' ? '索引失败，待清理' : '待同步') : statusIcon(doc.status))}</span>
        <span class="doc-error">${escapeHtml(doc.job_error || doc.sync_error || doc.error_msg || '')}</span>
        ${doc.failed_page_count ? `<details class="doc-page-errors"><summary>${Number(doc.failed_page_count)} 页未完成</summary>${(doc.failed_pages || []).map(/** 逐页展示解析失败原因与尝试次数。 */ p => `<div>${escapeHtml(`物理页 ${p.page} · ${p.status} · ${p.attempts} 次尝试：${p.error}`)}</div>`).join('')}</details>` : ''}
        ${doc.uncertain_page_count ? `<span class="doc-warning">${Number(doc.uncertain_page_count)} 页上游未提供截断判定；已解析不代表识别无遗漏</span>` : ''}
        <span class="doc-size">${(doc.file_bytes / 1024).toFixed(1)} KB</span>
        <button class="doc-delete" data-doc-id="${doc.doc_id}" ${btnDis}>${btnText}</button>
        ${['failed', 'interrupted', 'cancelled'].includes(doc.phase) ? `<button class="doc-retry" data-doc-id="${doc.doc_id}">恢复处理</button>` : ''}
        ${doc.phase === 'published' ? `<button class="doc-reparse" data-doc-id="${doc.doc_id}">重新解析</button><button class="doc-reindex" data-doc-id="${doc.doc_id}">重建索引</button>` : ''}
        ${doc.legacy ? (doc.original_available ? `<button class="doc-reparse" data-doc-id="${doc.doc_id}">重新解析建立结构</button>` : '<small>旧片段无完整原文件，重新上传可建立结构与原文定位</small>') : ''}
        ${doc.published_structure_id ? `<button class="doc-outline" data-doc-id="${doc.doc_id}">层级目录 · ${Number(doc.structure_nodes)} 节点</button>` : doc.published_available ? '<small>尚无层级目录，可重建索引；原页块仍可读取</small>' : ''}
        ${['queued', 'parsing', 'indexing'].includes(doc.phase) ? `<button class="doc-cancel" data-doc-id="${doc.doc_id}">取消处理</button>` : ''}
      </div>`;
    }).join('');
  }

  /** 展示固定结构版本的文档目录，支持分支加载、原文续读及结构审核发布。 */
  async function showDocumentTree(kbId, doc) {
    const dialog = document.createElement('dialog');
    dialog.className = 'kb-tree-dialog';
    dialog.style.cssText = 'width:90%;max-height:90vh;overflow:auto;background:#1e293b;color:#eee;border:1px solid #64748b';
    const close = document.createElement('button');
    close.textContent = '关闭';
    close.addEventListener('click', /** 关闭文档层级目录弹窗。 */ () => dialog.close());
    dialog.addEventListener('close', /** 目录弹窗关闭后移除 DOM 节点。 */ () => dialog.remove());
    const heading = document.createElement('h3');
    heading.textContent = doc.filename;
    const notice = document.createElement('p');
    notice.textContent = doc.file_type === 'pdf' ? '目录是导航，不表示已读取正文；OCR 与结构归属均可能有误。' : '目录按文本语法或保守规则建立，不表示模型已读；文本位置使用后端 Unicode 字符范围。';
    const tree = document.createElement('div');
    const preview = document.createElement('section');
    dialog.append(close, heading, notice, tree, preview);
    document.body.append(dialog);
    dialog.showModal();
    const base = `/v1/kb/${encodeURIComponent(kbId)}/docs/${encodeURIComponent(doc.doc_id)}`;
    const structure = doc.published_structure_id;
    let readGeneration = 0;
    /** 读取所选目录节点或其续读范围，仅让最新读取请求更新原文预览。 */
    async function readNode(node, continuation) {
      const generation = ++readGeneration;
      preview.textContent = '正在读取原文片段…';
      try {
        const target = continuation?.targets?.[0] || {node_id:node.node_id,structure_id:structure};
        const params = new URLSearchParams(Object.entries(target).filter(/** 从续读目标中排除已由接口路径确定或不属于查询参数的字段。 */ ([key]) => !['doc_id','mode'].includes(key)));
        const value = await kbRequest(`${base}/read?${params}`);
        if (!dialog.isConnected || generation !== readGeneration) return;
        preview.replaceChildren();
        const label = document.createElement('p');
        label.textContent = `${node.title} · ${value.excerpt_truncated ? '本次仅提供片段，尚未读完' : '本次所选节点范围已返回'}（不代表模型已读或语义核验）`;
        const text = document.createElement('pre');
        text.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere';
        text.textContent = value.content;
        preview.append(label,text);
        for (const page of [...new Set((value.locations || []).map(/** 提取原文位置对应的 PDF 物理页码。 */ p => p.page).filter(/** 过滤没有有效物理页码的文本位置。 */ p => Number.isInteger(p)))]) {
          const original = document.createElement('button');
          original.textContent = `查看原页 ${page}`;
          original.addEventListener('click', /** 读取固定解析版本的原页图，仅向当前有效的预览区域插入图片。 */ async () => {
            original.disabled = true;
            try {
              const response = await fetch(`${API_BASE}${base}/pages/${page}.png?${new URLSearchParams({parse_id:value.parse_id})}`);
              if (!response.ok) throw new Error(`HTTP ${response.status}`);
              const url = URL.createObjectURL(await response.blob());
              const image = document.createElement('img');
              image.alt = `原 PDF 物理页 ${page}`;
              image.style.width = '100%';
              image.src = url;
              image.addEventListener('load', /** 原页图加载完成后释放临时地址。 */ () => URL.revokeObjectURL(url), {once:true});
              dialog.addEventListener('close', /** 目录弹窗关闭时释放原页图临时地址。 */ () => URL.revokeObjectURL(url), {once:true});
              if (dialog.isConnected && generation === readGeneration) preview.append(image);
              else URL.revokeObjectURL(url);
            } catch (error) { original.textContent = `原页加载失败：${error.message}`; }
            finally { original.disabled = false; }
          });
          preview.append(original);
        }
        if (value.next_read) {
          const next = document.createElement('button');
          next.textContent = '继续读取下一段';
          next.addEventListener('click', /** 使用后端返回的续读目标读取下一段原文。 */ () => readNode(node,value.next_read));
          preview.append(next);
        }
      } catch (error) {
        if (dialog.isConnected && generation === readGeneration) preview.textContent = `读取失败：${error.message}`;
      }
    }
    /** 分页加载指定目录分支，按需展开子节点并提供失败重试入口。 */
    async function loadBranch(container, nodeId, offset = 0) {
      const params = new URLSearchParams({view:'tree',structure_id:structure,offset,limit:10});
      if (nodeId) params.set('node_id',nodeId);
      try {
        const result = await kbRequest(`${base}/outline?${params}`);
        if (!dialog.isConnected) return;
        for (const node of result.blocks) {
          const row = document.createElement('details');
          const summary = document.createElement('summary');
          const roles = {document_title:'文档总标题',section_heading:'正文章节',toc:'印刷目录',front_matter:'前置材料',header:'页眉',footer:'页脚',unknown:'角色未确定'};
          const state = node.review_status ? ` · ${node.review_status === 'unresolved' ? '待复核' : '模型推断，未作语义核验'}` : '';
          const level = node.declared_level != null ? ` · 章节级别 ${node.declared_level}` : '';
          summary.textContent = `${node.title} · ${node.page == null ? `原文行 ${node.line_start}–${node.line_end}` : `第 ${node.page}–${node.end_page} 页`} · ${roles[node.role] || node.kind}${level}${state}`;
          const text = document.createElement('p');
          text.textContent = `${node.preview || ''}（导航节选；依据：${node.basis}）`;
          const read = document.createElement('button');
          read.textContent = '读取此节点';
          read.addEventListener('click', /** 读取用户选中的目录节点。 */ () => readNode(node));
          const children = document.createElement('div');
          children.style.paddingLeft = '12px';
          row.append(summary,text,read,children);
          let loaded = false;
          row.addEventListener('toggle', /** 首次展开具有子节点的目录项时加载其分支。 */ () => {
            if (row.open && !loaded && node.children) {
              loaded = true;
              loadBranch(children,node.node_id);
            }
          });
          container.append(row);
        }
        if (result.next_offset != null) {
          const more = document.createElement('button');
          more.textContent = '加载更多目录项';
          more.addEventListener('click', /** 移除旧分页按钮并读取下一页目录项。 */ () => { more.remove(); loadBranch(container,nodeId,result.next_offset); });
          container.append(more);
        }
      } catch (error) {
        const failure = document.createElement('p');
        failure.textContent = `目录加载失败：${error.message}`;
        const retry = document.createElement('button');
        retry.textContent = '重试加载目录';
        retry.addEventListener('click', /** 清除失败提示后重试同一目录分支及分页位置。 */ () => { failure.remove(); retry.remove(); loadBranch(container,nodeId,offset); });
        if (dialog.isConnected) container.append(failure,retry);
      }
    }
    await loadBranch(tree,null);
    if (doc.file_type === 'pdf') {
      const reviews = document.createElement('details');
      const label = document.createElement('summary');
      label.textContent = '结构分析与显式发布';
      const area = document.createElement('div');
      reviews.append(label, area);
      dialog.insertBefore(reviews, tree);
      /** 查询已有结构分析任务，展示建议、发布资格和用户触发的发布入口。 */
      async function refreshReviews() {
        area.textContent = '正在查询结构任务…';
        try {
          const tasks = await kbRequest(`${base}/structure-reviews`);
          if (!dialog.isConnected) return;
          area.replaceChildren();
          const help = document.createElement('p');
          help.textContent = '仅展示已创建的分析任务，不自动调用模型。发布会更新正式目录和向量索引；旧版本保留。模型推断不代表结构必然正确。';
          area.append(help);
          for (const task of tasks) {
            const row = document.createElement('div');
            const title = document.createElement('p');
            const phases = {completed:'已完成',not_started:'未开始',published:'已发布',queued:'排队中',running:'进行中',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
            title.textContent = `${task.updated_at} · 分析 ${phases[task.status] || task.status} · 发布 ${phases[task.publication_status] || task.publication_status}`;
            const inspect = document.createElement('button');
            inspect.textContent = '查看结构建议';
            inspect.addEventListener('click', /** 加载并展示指定结构分析任务的建议结果。 */ async () => {
              try {
                const value = await kbRequest(`${base}/structure-review/${encodeURIComponent(task.review_id)}`);
                const text = document.createElement('pre');
                text.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere';
                text.textContent = JSON.stringify(value.result, null, 2);
                preview.replaceChildren(text);
              } catch (error) { title.textContent = `加载失败：${error.message}`; }
            });
            row.append(title, inspect);
            if (task.publishable && !['published','queued','running'].includes(task.publication_status)) {
              const publish = document.createElement('button');
              publish.textContent = task.publication_status === 'not_started' ? '发布此结构' : '恢复发布';
              publish.addEventListener('click', /** 用户确认后按预期基线发布或恢复结构发布，并启动文档状态轮询。 */ async () => {
                if (!confirm('将此结构建议发布为正式目录并重建向量索引？保留原文和旧版本，不调用聊天模型。')) return;
                publish.disabled = true;
                try {
                  const value = await kbRequest(`${base}/structure-review/${encodeURIComponent(task.review_id)}/publish`, {
                    method:'POST', body:JSON.stringify({expected_run_id:doc.published_run_id})});
                  title.textContent = `发布状态：${value.status}。请刷新进度；发布完成后重新打开目录查看新版本。`;
                  pollDocStatus(kbId,doc.doc_id);
                } catch (error) { title.textContent = `发布失败：${error.message}`; }
                finally { publish.disabled = false; }
              });
              row.append(publish);
            } else if (task.status === 'completed' && task.publication_status === 'not_started') {
              const stale = document.createElement('small');
              stale.textContent = '协议或正式结构基线已变化，仅供历史查看，不能直接发布。';
              row.append(stale);
            }
            area.append(row);
          }
          if (!tasks.length) area.append(document.createTextNode('尚无结构分析任务。'));
          const refresh = document.createElement('button');
          refresh.textContent = '刷新发布进度';
          refresh.addEventListener('click', refreshReviews);
          area.append(refresh);
        } catch (error) { area.textContent = `查询失败：${error.message}`; }
      }
      reviews.addEventListener('toggle', /** 展开结构分析区域时刷新已有任务列表。 */ () => { if (reviews.open) refreshReviews(); });
    }
  }

  /** 将文档处理状态转换为对应图标，未知状态保留原文本。 */
  function statusIcon(status) {
    if (status === 'pending') return '⏳';
    if (status === 'indexed') return '✅';
    if (status === 'failed') return '❌';
    return status;
  }

  const pollingDocs = new Set();
  /** 对文档处理状态去重轮询，任务结束或离开知识库时停止并清理登记。 */
  async function pollDocStatus(kb_id, doc_id) {
    const key = `${kb_id}/${doc_id}`;
    if (pollingDocs.has(key)) return;
    pollingDocs.add(key);
    try {
    let firstPoll = true;
    while (firstPoll || currentKbId === kb_id) {
      firstPoll = false;
      await new Promise(/** 在两次文档状态请求之间等待固定间隔。 */ r => setTimeout(r, 2000));
      try {
        const data = await kbRequest(`/v1/kb/${kb_id}/docs/${doc_id}/status`);
        if (data.status === 'not_found') {
          await loadDocs(kb_id);
          break;
        }
        if (currentKbId === kb_id) {
          docs = docs.map(/** 仅合并目标文档的最新状态，保留其他文档数据。 */ doc => doc.doc_id === doc_id ? {...doc, ...data} : doc);
          renderDocList();
        }
        if (data.phase ? !['queued', 'parsing', 'indexing'].includes(data.phase) : (data.status === 'indexed' || data.status === 'failed') && !data.sync_pending) {
          await loadDocs(kb_id);
          break;
        }
      } catch (err) {
        console.error('轮询状态失败:', err);
        break;
      }
    }
    } finally { pollingDocs.delete(key); }
  }

  document.getElementById('docList')?.addEventListener('click', /** 分派文档目录、恢复、取消、重新解析、重建索引和删除操作。 */ async (e) => {
    const outlineButton = e.target.closest('.doc-outline');
    if (outlineButton) {
      const kbId = currentKbId;
      const doc = docs.find(/** 定位目录按钮所属的文档记录。 */ d => d.doc_id === outlineButton.dataset.docId);
      if (doc) await showDocumentTree(kbId, doc);
      return;
    }
    const action = e.target.closest('.doc-retry, .doc-cancel, .doc-reparse, .doc-reindex');
    if (action) {
      action.disabled = true;
      const kbId = currentKbId;
      try {
        const operation = action.classList.contains('doc-reparse') ? 'retry?reparse=true' : action.classList.contains('doc-reindex') ? 'retry?reindex=true' : action.classList.contains('doc-retry') ? 'retry' : 'cancel';
        await kbRequest(`/v1/kb/${kbId}/docs/${action.dataset.docId}/${operation}`, {method: 'POST'});
        await loadDocs(kbId);
      } catch (error) { alert(error.message); action.disabled = false; }
      return;
    }
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
        setTimeout(/** 删除文档成功后清除忙状态并刷新列表。 */ () => { _busy.delete('doc-del-' + doc_id); loadDocs(kb_id); }, 800);
      } catch (err) {
        _busy.set('doc-del-' + doc_id, {text: '删除失败'});
        renderDocList();
        setTimeout(/** 删除失败提示结束后恢复文档操作界面。 */ () => { _busy.delete('doc-del-' + doc_id); renderDocList(); }, 1500);
      }
    }
  });

  /** 通过文本节点将待展示内容转义为安全的 HTML 文本。 */
  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // ─── 回收站 ─────────────────────────────────────────────────────

  /** 加载已删除知识库列表并刷新回收站，返回加载是否成功。 */
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

  /** 渲染回收站条目、同步状态以及还原和永久删除入口。 */
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
    container.innerHTML = trashKbs.map(/** 渲染回收站卡片及还原、永久删除操作的状态。 */ kb => {
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

  /** 切换到回收站视图并加载已删除知识库。 */
  function enterTrash() {
    viewMode = 'trash';
    document.getElementById('kbList').style.display = 'none';
    document.getElementById('kbDocsSection').style.display = 'none';
    document.getElementById('kbHeaderTitle').textContent = '回收站';
    document.getElementById('createKbBtn').style.display = 'none';
    document.getElementById('kbTrashBtn').textContent = '← 返回';
    loadTrash();
  }

  /** 恢复知识库主视图并重新加载可用知识库。 */
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

  document.getElementById('kbTrashBtn')?.addEventListener('click', /** 在活动知识库和回收站视图之间切换。 */ () => {
    viewMode === 'active' ? enterTrash() : exitTrash();
  });

  document.getElementById('kbTrashList')?.addEventListener('click', /** 处理回收站的还原或经二次确认的永久删除，并更新操作状态。 */ async (e) => {
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
        setTimeout(/** 还原成功后清除忙状态并刷新回收站。 */ () => { _busy.delete('kb-restore-' + kb_id); loadTrash(); }, 800);
      } catch (err) {
        _busy.set('kb-restore-' + kb_id, {text: '还原未完成，重试'});
        renderTrashList();
        setTimeout(/** 还原未完成时结束提示并重新加载同步状态。 */ () => { _busy.delete('kb-restore-' + kb_id); loadTrash(); }, 1500);
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
        setTimeout(/** 永久删除成功后清除忙状态并刷新回收站。 */ () => { _busy.delete('kb-hard-' + kb_id); loadTrash(); }, 800);
      } catch (err) {
        _busy.set('kb-hard-' + kb_id, {text: '删除失败'});
        renderTrashList();
        setTimeout(/** 永久删除失败提示结束后恢复回收站操作状态。 */ () => { _busy.delete('kb-hard-' + kb_id); renderTrashList(); }, 1500);
      }
      return;
    }
  });

  loadKBs();

  /** 向其他界面逻辑提供当前选中的知识库标识。 */
  window.getCurrentKbId = () => currentKbId;
})();
