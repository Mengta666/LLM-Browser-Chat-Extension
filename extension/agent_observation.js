(() => {
  function markCandidates(elements, viewport) {
    const names = new Map();
    for (const el of elements) {
      const name = el.text || el.aria_label || el.title || el.alt || '';
      if (name) names.set(name, (names.get(name) || 0) + 1);
    }
    return elements.filter(el => {
      const box = el.bounding_box || {};
      const name = el.text || el.aria_label || el.title || el.alt || '';
      return el.enabled !== false && !el.occluded && box.width > 0 && box.height > 0 &&
        box.x < viewport.width && box.y < viewport.height && box.x + box.width > 0 && box.y + box.height > 0 &&
        (!name || names.get(name) > 1 || el.label_source === 'svg-icon');
    });
  }

  function layoutMarks(elements, viewport, measure) {
    const labels = [];
    const candidates = markCandidates(elements, viewport);
    const overlaps = (a, b) => a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
    return candidates.map(el => {
      const b = el.bounding_box;
      const x = Math.max(0, b.x), y = Math.max(0, b.y);
      const box = { x, y, width: Math.min(viewport.width, b.x + b.width) - x,
        height: Math.min(viewport.height, b.y + b.height) - y };
      const width = Math.min(viewport.width, measure(String(el.id)) + 8), height = 20;
      const xs = [x, x + box.width - width, x + box.width + 2, x - width - 2];
      const ys = [y - height - 2, y + box.height + 2, y, y + box.height - height];
      const positions = ys.flatMap(ly => xs.map(lx => ({ x: Math.max(0, Math.min(lx, viewport.width - width)),
        y: Math.max(0, Math.min(ly, viewport.height - height)), width, height })));
      const free = positions.filter(p => !labels.some(l => overlaps(p, l)));
      const label = free.find(p => !candidates.some(c => overlaps(p, c.bounding_box))) || free[0] || null;
      if (label) labels.push(label);
      return { id: el.id, box, label };
    }).filter(mark => mark.label);
  }

  async function annotate(pageState) {
    pageState.screenshot_marked = false;
    pageState.screenshot_mark_ids = [];
    if (!pageState.screenshot) return pageState;
    const viewport = pageState.viewport || {};
    if (!(viewport.width > 0 && viewport.height >= 20)) return pageState;
    try {
      const img = new Image();
      await new Promise((resolve, reject) => {
        img.onload = resolve; img.onerror = reject; img.src = pageState.screenshot;
      });
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      ctx.scale(canvas.width / viewport.width, canvas.height / viewport.height);
      ctx.font = 'bold 14px sans-serif';
      const marks = layoutMarks(pageState.interactive_elements || [], viewport, text => ctx.measureText(text).width);
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
      pageState.screenshot_mark_ids = marks.map(mark => mark.id);
      pageState.screenshot_marked = true;
    } catch { /* 保留原图与本轮坐标，不宣称已经标注。 */ }
    return pageState;
  }

  globalThis.AgentObservation = { annotate, layoutMarks, markCandidates };
  if (typeof module !== 'undefined') module.exports = globalThis.AgentObservation;
})();
