(() => {
  const nodeRef = node => `${node.sessionId || 'main'}:${node.frameId || ''}:${node.backendNodeId}`;
  function targetKey(node) {
    const path = [];
    for (let n = node; n && path.length < 12; n = n.parent) {
      path.unshift(`${n.nodeName}:${n.parent?.children.indexOf(n) ?? 0}`);
    }
    return `${node.frameId || ''}/${path.join('/')}`;
  }
  function inspectOverlay(probes) {
    const rect = this.getBoundingClientRect(), root = this.getRootNode(), doc = this.ownerDocument;
    const style = getComputedStyle(this);
    if (!this.isConnected || rect.width <= 0 || rect.height <= 0 || style.display === 'none' ||
        style.visibility !== 'visible' || Number(style.opacity) === 0) return null;
    const belongs = node => {
      for (let n = node; n; n = n.parentNode || n.host) if (n === this) return true;
      return false;
    };
    const left = Math.max(0, rect.left), top = Math.max(0, rect.top);
    const right = Math.min(doc.defaultView.innerWidth, rect.right), bottom = Math.min(doc.defaultView.innerHeight, rect.bottom);
    if (right <= left || bottom <= top) return null;
    const points = [...probes, ...[[.5,.5],[.15,.15],[.85,.15],[.15,.85],[.85,.85]].map(([x,y]) =>
      ({ x: left + (right-left)*x, y: top + (bottom-top)*y }))];
    const blockingIds = [], samples = [];
    for (const point of points) {
      const hit = root.elementFromPoint?.(point.x, point.y);
      let exposed = belongs(hit);
      // 内层根命中之前，外层根必须仍命中该宿主，避免把被外部遮住的浮层当成顶层。
      for (let current = root; exposed && current.host; current = current.host.getRootNode()) {
        exposed = current.host.getRootNode().elementFromPoint?.(point.x, point.y) === current.host;
      }
      if (exposed && point.id !== undefined) blockingIds.push(point.id);
      samples.push({ x: Math.round(point.x), y: Math.round(point.y), hit_layer: exposed });
    }
    if (!samples.some(p => p.hit_layer)) return null;
    if (style.position !== 'fixed' && !blockingIds.length) return null;
    const heading = this.querySelector('h1,h2,h3,[role="heading"]');
    return { detection: 'layout_hit_test', inferred: true, blocking_ids: [...new Set(blockingIds)],
      header_text: (this.getAttribute('aria-label') || heading?.textContent || this.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 240),
      hit_samples: samples, position: style.position };
  }
  function collectPopups(nodes, elements) {
    const byRef = new Map();
    for (const node of nodes) {
      if (!node.backendNodeId || !node.viewportRect?.width || !node.viewportRect?.height) continue;
      let rendered = true;
      for (let n = node; n; n = n.parent) {
        const style = n.snapshot?.computedStyles || {};
        if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) || Number(style.opacity || 1) === 0) { rendered = false; break; }
      }
      if (!rendered) continue;
      const a = node.attributes || {}, role = a.role || node.ax?.role || '';
      const classes = (a.class || '').split(/\s+/);
      const known = classes.some(c => /^(ant-modal-content|ant-dropdown|ant-picker-panel|ant-select-dropdown|ant-popover-inner|el-dialog|el-dropdown-menu|el-picker-panel|el-select-dropdown|el-popover|jmtd-popup|jmtd-modal|jmtd-dropdown-panel|swal2-popup)$/.test(c));
      const semantic = ['dialog', 'alertdialog', 'listbox', 'menu'].includes(role) || (node.nodeName === 'DIALOG' && a.open !== undefined);
      if (!semantic && !known && !node.overlay) continue;
      const ref = nodeRef(node);
      byRef.set(ref, { ...node.overlay, ref, key: targetKey(node), type: semantic ? role || 'dialog' : known ? 'popup' : 'overlay', frame_id: node.frameId || '',
        detection: semantic ? 'semantic' : known ? 'component' : node.overlay.detection, inferred: !semantic,
        header_text: (a['aria-label'] || node.overlay?.header_text || node.ax?.name || '').slice(0, 240),
        modal: a['aria-modal'] === 'true', visible: node.isVisible, parent_ref: null, member_ids: [],
        paint_order: node.snapshot?.paintOrder ?? null, bounds: node.absolutePosition || null });
    }
    const byNode = new Map(nodes.map(n => [nodeRef(n), n]));
    for (const popup of byRef.values()) {
      for (let n = byNode.get(popup.ref)?.parent; n; n = n.parent) {
        if (byRef.has(nodeRef(n))) { popup.parent_ref = nodeRef(n); break; }
      }
    }
    for (const el of elements) {
      el.popup_ref = null;
      for (let n = byNode.get(el.node_ref); n; n = n.parent) {
        const popup = byRef.get(nodeRef(n));
        if (popup) { el.popup_ref ||= popup.ref; popup.member_ids.push(el.id); }
      }
      el.in_popup = !!el.popup_ref;
    }
    return [...byRef.values()];
  }
  function evaluate(expect, page) {
    if (!expect?.kind) return { status: 'not_requested' };
    const result = { kind: expect.kind, status: 'unknown', observation_id: page.observation_id };
    if (expect.kind === 'invalid') return { ...result, reason: 'invalid_expectation', details: expect.reason };
    if (expect.kind === 'url_changed') return { ...result, status: page.url !== expect.url ? 'satisfied' : 'not_satisfied' };
    if (page.document_epoch !== expect.document_epoch) return { ...result, reason: 'document_changed' };
    if (expect.kind === 'popup_closed') {
      if ((page.popups || []).some(p => p.ref === expect.popup_ref)) return { ...result, status: 'not_satisfied' };
      if ((page.popups || []).some(p => p.key === expect.popup_key)) return { ...result, reason: 'popup_replaced' };
      return { ...result, status: page.observation_complete === true ? 'satisfied' : 'unknown' };
    }
    if (expect.kind === 'popup_opened') {
      const found = (page.popups || []).some(p => p.frame_id === expect.frame_id && !expect.popup_refs.includes(p.ref));
      return { ...result, status: found ? 'satisfied' : page.observation_complete === true ? 'not_satisfied' : 'unknown' };
    }
    const target = (page.interactive_elements || []).find(e => e.node_ref === expect.target_ref);
    if (!target) return { ...result, reason: 'target_replaced_or_absent' };
    const value = expect.kind === 'checked' ? (target.indeterminate ? null : target.checked) :
      expect.kind === 'expanded' ? (target.aria_expanded === 'true' ? true : target.aria_expanded === 'false' ? false : null) : target.enabled;
    return { ...result, actual: value, status: typeof value === 'boolean' ? value === expect.value ? 'satisfied' : 'not_satisfied' : 'unknown' };
  }
  globalThis.AgentVerification = { nodeRef, targetKey, collectPopups, inspectOverlay, evaluate };
  if (typeof module !== 'undefined') module.exports = globalThis.AgentVerification;
})();
