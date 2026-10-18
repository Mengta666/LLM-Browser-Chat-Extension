(() => {
  function describeCheckable() {
    const control = this.tagName === 'LABEL' ? this.control : this;
    if (!control?.isConnected) return null;
    const native = control.tagName === 'INPUT' && ['checkbox', 'radio'].includes(control.type);
    const kind = native ? control.type : control.getAttribute('role');
    if (!['checkbox', 'radio', 'switch'].includes(kind)) return null;
    const aria = control.getAttribute('aria-checked');
    const path = [];
    for (let n = control; n?.tagName && path.length < 12; n = n.parentElement || n.getRootNode()?.host) {
      path.unshift(`${n.tagName}:${Array.from(n.parentNode?.children || []).indexOf(n)}`);
    }
    return { control_kind: kind, checked: native ? control.checked : aria === 'true' ? true : aria === 'false' ? false : null,
      control_path: path.join('/'),
      indeterminate: native ? control.indeterminate : aria === 'mixed', state_source: native ? 'live_dom' : 'aria',
      control_label: (control.getAttribute('aria-label') || '').slice(0, 100),
      disabled: control.matches(':disabled') || control.getAttribute('aria-disabled') === 'true' };
  }

  function inspectPoint(x, y, q, ...independent) {
    const brief = node => {
      if (!node) return null;
      const path = [];
      for (let current = node; current?.tagName && path.length < 8; current = current.parentElement) {
        path.unshift(`${current.tagName.toLowerCase()}:${Array.from(current.parentNode?.children || []).indexOf(current)}`);
      }
      return { tag: node.tagName?.toLowerCase() || '', role: node.getAttribute?.('role') || '', path: path.join('/') };
    };
    const result = { ok: false, reason: '', target: brief(this), hit: null, roots: [] };
    const fail = reason => ({ ...result, reason });
    if (!this.isConnected) return fail('target_stale');
    const control = this.tagName === 'LABEL' ? this.control : this;
    result.control_state = { disabled: !!control?.matches(':disabled'),
      aria_disabled: control?.getAttribute('aria-disabled') || '', opacity: getComputedStyle(this).opacity };
    const r = this.getBoundingClientRect();
    const left = Math.min(q[0], q[2], q[4], q[6]), top = Math.min(q[1], q[3], q[5], q[7]);
    const width = Math.max(q[0], q[2], q[4], q[6]) - left, height = Math.max(q[1], q[3], q[5], q[7]) - top;
    if (width <= 0 || height <= 0 || r.width <= 0 || r.height <= 0) return fail('no_geometry');
    const lx = r.x + (x - left) * r.width / width, ly = r.y + (y - top) * r.height / height;
    const doc = this.ownerDocument;
    let at = doc.elementFromPoint(lx, ly);
    result.hit = brief(at);
    if (!at) return fail('outside_viewport');
    // 逐层确认外部命中宿主，再进入目标所属根；不能跳过宿主外的遮挡。
    const roots = [];
    for (let root = this.getRootNode(); root.host; root = root.host.getRootNode()) roots.unshift(root);
    for (const root of roots) {
      result.roots.push(root.mode);
      if (at !== root.host) return fail('occluded');
      if (typeof root.elementFromPoint !== 'function') return fail('unsupported_hit_test');
      at = root.elementFromPoint(lx, ly);
      result.hit = brief(at);
      if (!at || at === root.host) return fail('unsupported_hit_test');
    }
    while (at?.shadowRoot) {
      const inner = at.shadowRoot.elementFromPoint(lx, ly);
      if (!inner || inner === at) break;
      at = inner;
    }
    result.hit = brief(at);
    const contains = (parent, child) => {
      for (let node = child; node; node = node.parentNode || node.host) if (node === parent) return true;
      return false;
    };
    if (independent.some(child => child !== (this.tagName === 'LABEL' ? this.control : null) && contains(child, at))) return fail('independent_control');
    const matches = contains(this, at) || (this.tagName === 'INPUT' && Array.from(this.labels || []).some(label => contains(label, at))) ||
      (this.tagName === 'LABEL' && this.control === at);
    return matches ? { ...result, ok: true } : fail('occluded');
  }

  globalThis.AgentClick = { describeCheckable, inspectPoint };
  if (typeof module !== 'undefined') module.exports = globalThis.AgentClick;
})();
