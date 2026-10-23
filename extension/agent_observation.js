(/** 封装并导出截图编号的筛选、布局和绘制接口。 */ () => {
  /** 筛选视口内可交互且需要编号区分的无名、重名或图标元素。 */
  function markCandidates(elements, viewport) {
    const names = new Map();
    for (const el of elements) {
      const name = el.text || el.aria_label || el.title || el.alt || '';
      if (name) names.set(name, (names.get(name) || 0) + 1);
    }
    return elements.filter(/** 保留可见、可用且需要编号区分的元素。 */ el => {
      const box = el.bounding_box || {};
      const name = el.text || el.aria_label || el.title || el.alt || '';
      return el.enabled !== false && !el.occluded && box.width > 0 && box.height > 0 &&
        box.x < viewport.width && box.y < viewport.height && box.x + box.width > 0 && box.y + box.height > 0 &&
        (!name || names.get(name) > 1 || el.label_source === 'svg-icon');
    });
  }

  /** 为候选元素安排不重叠的编号位置，无法放置时省略该标记。 */
  function layoutMarks(elements, viewport, measure) {
    const labels = [];
    const candidates = markCandidates(elements, viewport);
    /** 判断两个轴对齐矩形是否存在面积重叠。 */
    const overlaps = (a, b) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
    return candidates.map(/** 为单个元素裁剪边界并选择编号位置。 */ el => {
      const b = el.bounding_box;
      const x = Math.max(0, b.x), y = Math.max(0, b.y);
      const box = { x, y, width: Math.min(viewport.width, b.x + b.width) - x,
        height: Math.min(viewport.height, b.y + b.height) - y };
      const width = Math.min(viewport.width, measure(String(el.id)) + 8), height = 20;
      const xs = [x, x + box.width - width, x + box.width + 2, x - width - 2];
      const ys = [y - height - 2, y + box.height + 2, y, y + box.height - height];
      const positions = ys.flatMap(/** 为当前候选纵坐标生成各横向位置。 */ ly => xs.map(/** 把编号矩形限制在视口范围内。 */ lx => ({ x: Math.max(0, Math.min(lx, viewport.width - width)),
        y: Math.max(0, Math.min(ly, viewport.height - height)), width, height })));
      const free = positions.filter(/** 排除与已有编号重叠的位置。 */ p => !labels.some(/** 检查候选位置是否遮盖已有编号。 */ l => overlaps(p, l)));
      // 优先避开元素本体；若所有位置都会覆盖已有编号，则省略此标记，避免编号混淆。
      const label = free.find(/** 优先寻找不遮盖候选元素的位置。 */ p => !candidates.some(/** 检查编号位置是否与元素本体重叠。 */ c => overlaps(p, c.bounding_box))) || free[0] || null;
      if (label) labels.push(label);
      return { id: el.id, box, label };
    }).filter(/** 仅保留成功分配编号位置的元素。 */ mark => mark.label);
  }

  /** 按视口与截图的比例绘制元素编号，失败时保留原始截图和观察信息。 */
  async function annotate(pageState) {
    pageState.screenshot_marked = false;
    pageState.screenshot_mark_ids = [];
    if (!pageState.screenshot) return pageState;
    const viewport = pageState.viewport || {};
    if (!(viewport.width > 0 && viewport.height >= 20)) return pageState;
    try {
      const img = new Image();
      await new Promise(/** 等待截图解码成功或失败。 */ (resolve, reject) => {
        img.onload = resolve; img.onerror = reject; img.src = pageState.screenshot;
      });
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      // 观察坐标使用视口单位，截图像素尺寸可能不同，绘制前统一缩放到同一坐标系。
      ctx.scale(canvas.width / viewport.width, canvas.height / viewport.height);
      ctx.font = 'bold 14px sans-serif';
      const marks = layoutMarks(pageState.interactive_elements || [], viewport, /** 测量当前字体下编号文本的宽度。 */ text => ctx.measureText(text).width);
      if (!marks.length) return pageState;
      ctx.lineWidth = 2;
      for (const { id, box, label } of marks) {
        ctx.strokeStyle = '#b000ef';
        ctx.strokeRect(box.x, box.y, box.width, box.height);
        ctx.beginPath(); ctx.moveTo(label.x + label.width / 2, label.y + label.height / 2);
        ctx.lineTo(Math.max(box.x, Math.min(label.x + label.width / 2, box.x + box.width)),
          Math.max(box.y, Math.min(label.y + label.height / 2, box.y + box.height)));
        ctx.stroke();
        ctx.fillStyle = '#b000ef'; ctx.fillRect(label.x, label.y, label.width, label.height);
        ctx.fillStyle = '#fff'; ctx.fillText(String(id), label.x + 4, label.y + 15);
      }
      pageState.screenshot = canvas.toDataURL('image/jpeg', 0.85);
      pageState.screenshot_mark_ids = marks.map(/** 提取已绘制编号供观察结果记录。 */ mark => mark.id);
      pageState.screenshot_marked = true;
    } catch { /* 保留原图与本轮坐标，不宣称已经标注。 */ }
    return pageState;
  }

  globalThis.AgentObservation = { annotate, layoutMarks, markCandidates };
  if (typeof module !== 'undefined') module.exports = globalThis.AgentObservation;
})();
