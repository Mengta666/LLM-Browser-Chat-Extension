/* Chat 图片文件与附件接口；不参与自动化的 Data URL 协议。 */
(() => {
  const MAX_BYTES = 10 * 1024 * 1024;
  const MAX_PIXELS = 20_000_000;
  const errors = {
    attachments_not_configured: '后端尚未配置 Chat 附件地址和签名密钥。',
    attachment_protocol_required: '请重新加载扩展，图片需通过附件接口发送。',
    attachment_too_large: '图片超过 10 MiB，请裁剪后重传。',
    attachment_body_too_large: '上传请求过大，请裁剪图片后重传。',
    attachment_pixels_exceeded: '图片超过 2000 万像素，请裁剪后重传。',
    attachment_invalid_image: '图片损坏或无法解码。',
    attachment_type_unsupported: '仅支持 PNG、JPEG、WebP 和 GIF 图片。',
    attachment_unavailable: '图片文件已不可用，请重新上传。',
    attachment_not_found: '附件不存在或不属于当前会话。',
    attachment_in_use: '已发送的图片不能作为临时附件删除。',
    attachment_access_denied: '图片访问链接已失效，请重新加载图片。',
    attachment_upload_conflict: '附件上传标识冲突，请重新选择图片。',
    session_deleted: '会话已删除。'
  };

  function format(bytes) {
    return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
  }

  function detect(bytes) {
    const text = (start, end) => String.fromCharCode(...bytes.slice(start, end));
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (bytes.length >= 8 && text(0, 8) === '\x89PNG\r\n\x1a\n') {
      for (let p = 8; p + 12 <= bytes.length;) {
        const length = view.getUint32(p);
        if (text(p + 4, p + 8) === 'acTL') return { mime: 'image/png', animated: true };
        p += length + 12;
      }
      return { mime: 'image/png', animated: false };
    }
    if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return { mime: 'image/jpeg', animated: false };
    if (['GIF87a', 'GIF89a'].includes(text(0, 6))) return { mime: 'image/gif', animated: true };
    if (text(0, 4) === 'RIFF' && text(8, 12) === 'WEBP') {
      for (let p = 12; p + 8 <= bytes.length;) {
        const length = view.getUint32(p + 4, true);
        if (text(p, p + 4) === 'ANIM') return { mime: 'image/webp', animated: true };
        p += 8 + length + length % 2;
      }
      return { mime: 'image/webp', animated: false };
    }
    throw new Error(errors.attachment_type_unsupported);
  }

  async function prepare(file) {
    if (!file.size || file.size > MAX_BYTES) throw new Error(errors.attachment_too_large);
    const bytes = new Uint8Array(await file.arrayBuffer());
    const detected = detect(bytes);
    let blob = new Blob([bytes], { type: detected.mime });
    let bitmap;
    try { bitmap = await createImageBitmap(blob); }
    catch { throw new Error(errors.attachment_invalid_image); }
    const { width, height } = bitmap;
    try {
      if (width * height > MAX_PIXELS) throw new Error(errors.attachment_pixels_exceeded);
      if (detected.animated) {
        const canvas = document.createElement('canvas');
        canvas.width = width; canvas.height = height;
        canvas.getContext('2d').drawImage(bitmap, 0, 0);
        blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
        if (!blob) throw new Error(errors.attachment_invalid_image);
      }
    } finally { bitmap.close(); }
    if (blob.size > MAX_BYTES) throw new Error(errors.attachment_too_large);
    return { blob, previewUrl: URL.createObjectURL(blob), name: file.name || '截图.png',
      mime: blob.type, size: blob.size, width, height, first_frame: detected.animated,
      clientId: crypto.randomUUID() };
  }

  async function request(url, apiKey, options = {}) {
    const headers = new Headers(options.headers);
    if (apiKey) headers.set('Authorization', `Bearer ${apiKey}`);
    const timeout = AbortSignal.timeout(60000);
    const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
    const response = await fetch(url, { ...options, signal, headers, credentials: 'omit', redirect: 'error' });
    if (!response.ok) {
      let code = '';
      try { const data = await response.json(); code = data.error?.code || data.detail; } catch { /* 非 JSON 错误只显示状态码。 */ }
      const error = new Error(errors[code] || `附件请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return response;
  }

  async function upload(endpoint, apiKey, image, signal) {
    const form = new FormData();
    form.append('file', image.blob, image.name);
    form.append('client_attachment_id', image.clientId);
    form.append('first_frame', String(Boolean(image.first_frame)));
    return (await request(endpoint, apiKey, { method: 'POST', body: form, signal })).json();
  }

  async function preview(endpoint, apiKey, downloadUrl, signal) {
    const access = await (await request(endpoint, apiKey, { method: 'POST', signal })).json();
    // 相对路径由当前后端解析；不跟随响应提供的任意远程 URL。
    if (!/^\/v1\/chat-attachments\/att_[0-9a-f]{32}\/content\?/.test(access.path)) throw new Error('图片访问地址无效');
    const response = await request(downloadUrl(access.path), apiKey, { signal });
    return URL.createObjectURL(await response.blob());
  }

  function enlarge(src) {
    const dialog = document.createElement('dialog');
    dialog.className = 'chat-image-dialog';
    const close = document.createElement('button'); close.textContent = '关闭'; close.type = 'button';
    const img = document.createElement('img'); img.src = src; img.alt = '图片放大预览';
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => dialog.remove());
    dialog.append(close, img); document.body.appendChild(dialog); dialog.showModal();
  }

  const api = { MAX_BYTES, MAX_PIXELS, errors, format, detect, prepare, request, upload, preview, enlarge };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.ChatImages = api;
})();
