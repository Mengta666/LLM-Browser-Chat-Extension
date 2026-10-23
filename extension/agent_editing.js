// 在目标节点所属页面执行；描述对象只存活于一次动作，不跨观察复用 DOM 引用。
/** 识别原生输入框、富文本和 CodeMirror 编辑区域，返回观察信息或受校验的操作接口。 */
function describeEditingTarget(observe = false) {
  const node = this;
  const doc = node.ownerDocument;
  const win = doc.defaultView;
  const wrapper = node.closest('.CodeMirror');
  let cm = wrapper?.CodeMirror;
  const cmInput = cm?.getInputField?.();
  const surface = node === wrapper || node === cmInput ||
    ['CodeMirror-scroll', 'CodeMirror-sizer', 'CodeMirror-lines', 'CodeMirror-code'].some(/** 识别 CodeMirror 可编辑表面的已知类名。 */ c => node.classList.contains(c));
  if (!surface || cm?.getWrapperElement?.() !== wrapper || !wrapper?.contains(cmInput)) cm = null;
  let root = cm ? wrapper : node;
  let input = cm ? cmInput : node;
  let kind = cm ? 'codemirror5' : ['INPUT', 'TEXTAREA'].includes(node.tagName) ? 'native' :
    node.isContentEditable ? 'contenteditable' : 'none';
  if (kind === 'contenteditable') {
    while (root.parentElement?.isContentEditable) root = root.parentElement;
    input = root;
  }
  const direct = kind === 'native' && ['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'].includes(input.type);
  if (kind === 'native' && ['hidden', 'file', 'button', 'checkbox', 'radio', 'submit', 'reset', 'image'].includes(input.type)) kind = 'none';
  /** 按编辑器类型读取完整文本，并统一富文本换行及不换行空格。 */
  const value = () => {
    if (cm) return cm.getValue('\n');
    if (kind === 'native') return input.value;
    if (kind !== 'contenteditable') return '';
    // 浏览器用块元素分行，并在空行末尾保留占位 BR；innerText 会额外计算这些占位换行。
    /** 递归重建富文本的可见文本，保留块级换行并排除占位换行。 */
    const textOf = element => {
      let text = '', previousBlock = false;
      for (const child of element.childNodes) {
        if (child.nodeType === 3) {
          if (previousBlock) text += '\n';
          text += child.nodeValue; previousBlock = false; continue;
        }
        if (child.nodeType !== 1) continue;
        const style = win.getComputedStyle(child);
        if (style.display === 'none') continue;
        if (child.tagName === 'BR') {
          if (child.nextSibling) text += '\n';
          previousBlock = false;
          continue;
        }
        const block = ['block', 'list-item', 'flow-root'].includes(style.display);
        if (child.previousSibling && (previousBlock || block && !text.endsWith('\n'))) text += '\n';
        text += textOf(child);
        previousBlock = block;
      }
      return text;
    };
    // 普通富文本编辑时 Chromium 会把边界空格转成 NBSP；纯文本回读统一为空格。
    return textOf(root).replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ');
  };
  /** 判断文档及目标编辑区域是否仍持有输入焦点。 */
  const focused = () => {
    if (!doc.hasFocus()) return false;
    const active = input.getRootNode().activeElement;
    return active === input || kind === 'contenteditable' && input.contains(active);
  };
  /** 收集原生校验失败项，并补充程序赋值时可能未触发的长度检查。 */
  const validity = (element) => {
    const violations = element.validity ? ['valueMissing', 'typeMismatch', 'patternMismatch', 'tooLong', 'tooShort',
      'rangeUnderflow', 'rangeOverflow', 'stepMismatch', 'badInput', 'customError'].filter(/** 筛选原生有效性状态中的失败项。 */ key => element.validity[key]) : [];
    if (kind === 'native' && element.maxLength >= 0 && element.value.length > element.maxLength) violations.push('tooLong');
    if (kind === 'native' && element.value && element.minLength > element.value.length) violations.push('tooShort');
    return { valid: violations.length === 0, violations: [...new Set(violations)] };
  };
  /** 确认编辑目标未被替换，返回可见性、只读状态、焦点及编辑能力。 */
  const state = () => {
    if (!root.isConnected || !input.isConnected || kind === 'contenteditable' && !root.isContentEditable || cm &&
        (wrapper.CodeMirror !== cm || cm.getInputField() !== input)) {
      return { success: false, stale: true, error: '编辑目标已变化，请重新观察' };
    }
    let visible = root.getBoundingClientRect().width > 0 && root.getBoundingClientRect().height > 0;
    for (let p = root; p; p = p.parentElement) {
      const s = win.getComputedStyle(p);
      if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse' || s.opacity === '0') visible = false;
    }
    const readOnly = !!(input.readOnly || input.matches(':disabled') ||
      input.closest('[inert],[aria-disabled="true"],[aria-readonly="true"]') || cm?.getOption('readOnly'));
    return { success: true, kind, editable: kind !== 'none' && visible && !readOnly,
      readOnly, visible, focused: focused(), direct, multiline: !!cm || kind === 'contenteditable' || input.tagName === 'TEXTAREA',
      proxy: !!cm && node !== wrapper };
  };
  /** 分派读取、聚焦、选区和写入操作，在修改前检查目标、焦点及完整原值。 */
  const call = (operation, arg) => {
    const s = state();
    if (!s.success) return s;
    if (operation === 'state') return s;
    if (operation === 'focus') {
      if (!s.visible || input.matches(':disabled') || input.closest('[inert]') || cm?.getOption('readOnly') === 'nocursor')
        return { success: false, error: '目标当前不可聚焦' };
      root.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      if (cm) cm.focus(); else input.focus({ preventScroll: true });
      const after = state();
      return after.success && after.focused ? after : { success: false, stale: after.stale, error: '未能确认目标焦点，未继续输入' };
    }
    if (kind === 'none') return { success: false, error: '目标不是支持的编辑区域，请选择标记为可输入的元素' };
    if (operation === 'read') return { ...s, value: value(), ...validity(input),
      rich_text: kind === 'contenteditable' && !!root.querySelector('a,img,table,strong,b,em,i,u,ul,ol,pre,code,[style]') };
    if (operation === 'validate') {
      if (kind !== 'native') return { success: true, valid: true, violations: [] };
      const probe = input.cloneNode(false);
      probe.value = arg;
      const result = validity(probe);
      if (probe.value !== arg) return { success: true, valid: false, violations: ['valueSanitized'] };
      return { success: true, ...result };
    }
    if (!s.editable) return { success: false, error: '目标不可编辑或只读，未修改内容' };
    if (!s.focused) return { success: false, error: '编辑焦点已转移，未继续输入' };
    if (operation === 'guard') return s;
    if (operation === 'guard_value' || operation === 'replace') {
      // 用完整原值做并发校验，防止观察之后的用户编辑或页面更新被旧动作覆盖。
      if (value() !== arg.prior) return { success: false, stale: true, error: '字段完整原值已改变，未覆盖，请重新观察' };
      if (operation === 'guard_value') return s;
      if (kind === 'native') {
        const prototype = input.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(prototype, 'value').set.call(input, arg.text);
        input.dispatchEvent(new win.Event('input', { bubbles: true }));
        input.dispatchEvent(new win.Event('change', { bubbles: true }));
      } else if (cm) {
        cm.operation(/** 在一次 CodeMirror 操作中替换完整原文。 */ () => cm.replaceRange(arg.text, { line: 0, ch: 0 }, cm.posFromIndex(arg.prior.length), '+input'));
      } else return { success: false, error: '此编辑器不支持同步替换' };
      return state();
    }
    if (operation === 'insert_cm') {
      if (!cm) return { success: false, error: '目标不是已确认的 CodeMirror 编辑器' };
      if (cm.listSelections().length !== 1) return { success: false, error: '多光标输入暂不支持，未写入' };
      cm.replaceSelection(arg, 'end', '+input');
      return state();
    }
    if (operation === 'select_all') {
      if (cm) cm.setSelection({ line: 0, ch: 0 }, cm.posFromIndex(value().length));
      else if (kind === 'native') input.select();
      else {
        const range = doc.createRange(); range.selectNodeContents(root);
        const selection = win.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      }
      return state();
    }
    if (operation === 'set_native') {
      if (kind !== 'native') return { success: false, error: '非原生控件不允许直接赋值' };
      const prototype = input.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, 'value').set.call(input, arg);
      input.dispatchEvent(new win.Event('input', { bubbles: true }));
      input.dispatchEvent(new win.Event('change', { bubbles: true }));
      return state();
    }
    if (operation === 'expected') {
      // 先根据当前选区推导输入后的全文，供执行后回读比较；反向选区也按起止范围处理。
      const text = value();
      let start, end;
      if (cm) {
        const ranges = cm.listSelections();
        if (ranges.length !== 1) return { success: false, error: '多光标输入暂不支持，未写入' };
        start = cm.indexFromPos(ranges[0].anchor); end = cm.indexFromPos(ranges[0].head);
      } else if (kind === 'native') {
        start = input.selectionStart; end = input.selectionEnd;
        if (text === '') start = end = 0;
      } else {
        const selection = win.getSelection();
        const range = selection.rangeCount === 1 ? selection.getRangeAt(0) : null;
        if (!range || !root.contains(range.startContainer) || !root.contains(range.endContainer) || text !== root.textContent.replace(/\u00a0/g, ' '))
          return { success: false, error: '当前富文本选区无法可靠回读，请使用 clear=true 替换内容' };
        const before = range.cloneRange(); before.selectNodeContents(root); before.setEnd(range.startContainer, range.startOffset);
        start = before.toString().length; end = start + range.toString().length;
      }
      if (typeof start !== 'number' || typeof end !== 'number') return { success: false, error: '无法确认插入位置，未写入' };
      return { success: true, value: text.slice(0, Math.min(start, end)) + arg + text.slice(Math.max(start, end)) };
    }
    return { success: false, error: '不支持的编辑操作' };
  };
  if (observe) {
    const s = state();
    const current = node.tagName === 'SELECT' ? node.value :
      s.success && kind !== 'none' && input.type !== 'password' ? value() : '';
    const group = node.closest('fieldset');
    const form = node.closest('form');
    const row = node.closest('tr');
    const hints = [...(node.labels || [])].map(/** 读取关联标签的文本提示。 */ label => label.textContent.trim());
    for (const id of (node.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)) {
      const hint = doc.getElementById(id)?.textContent?.trim();
      if (hint) hints.push(hint);
    }
    if (row) hints.push(...[...row.querySelectorAll('th')].map(/** 提取当前表格行的表头提示。 */ cell => cell.textContent.trim()));
    return { ...s, preview: current.slice(0, 120), value_truncated: current.length > 120,
      ...(kind !== 'none' && input.type !== 'password' || node.tagName === 'SELECT' ? { snapshot_value: current } : {}),
      value_attribute: node.getAttribute('value') || '',
      tag: node.tagName.toLowerCase(), type: node.getAttribute('type') || '', name: node.getAttribute('name') || '',
      html_id: node.id || '', placeholder: node.getAttribute('placeholder') || '', aria_label: node.getAttribute('aria-label') || '',
      form_context: [form?.id, form?.getAttribute('name'), form?.getAttribute('aria-label'),
        group?.querySelector('legend')?.textContent?.trim()].filter(Boolean).join(' / ').slice(0, 400),
      field_context: hints.join(' / ').slice(0, 600),
      ...(node.tagName === 'SELECT' ? { selected_text: node.selectedOptions[0]?.textContent.trim() || '',
        options: [...node.options].map(/** 序列化选项文本、值及禁用状态。 */ o => ({ text: o.textContent.trim(), value: o.value,
          disabled: o.disabled || o.parentElement.tagName === 'OPTGROUP' && o.parentElement.disabled })) } : {}) };
  }
  return { call };
}

/** 精确匹配原生单选下拉项，校验原值与选项身份，按模式只读核验或执行选择。 */
function selectNativeTarget(params, readOnly) {
  if (!this.isConnected) return { success: false, stale: true, error: '选择目标已脱离页面' };
  if (this.multiple) return { success: false, error: '单项选择不能用于多选控件' };
  if (this.matches(':disabled')) return { success: false, error: '下拉框已禁用' };
  if (params.material_id && !(readOnly && params.expected_prior_complete === false) &&
      (typeof params.expected_prior !== 'string' || this.value !== params.expected_prior))
    return { success: false, stale: true, error: '字段值已改变，未覆盖，请重新观察' };
  const matches = [...this.options].filter(/** 精确匹配请求指定的选项文本。 */ o => o.textContent.trim() === params.option_text);
  if (matches.length !== 1) return { success: false, error: '目标选项不存在或不唯一，未进行模糊选择' };
  const option = matches[0];
  /** 读取目标选项的索引、文本和值，供执行前后检查身份是否一致。 */
  const identity = () => ({ index: option.index, text: option.textContent.trim(), value: option.value });
  const expected = params.expected_option || identity();
  if (Object.entries(expected).some(/** 检查选择前的选项身份是否变化。 */ ([key, value]) => identity()[key] !== value))
    return { success: false, stale: true, error: '目标选项身份已变化，请重新观察' };
  if (option.disabled || option.parentElement.tagName === 'OPTGROUP' && option.parentElement.disabled)
    return { success: false, error: '目标选项已禁用' };
  if (!readOnly) {
    const probe = this.cloneNode(true);
    probe.selectedIndex = option.index;
    if (!probe.validity.valid) return { success: false, valid: false, error: '目标选项不满足字段约束，未修改' };
    this.selectedIndex = option.index;
    this.dispatchEvent(new Event('input', { bubbles: true }));
    this.dispatchEvent(new Event('change', { bubbles: true }));
  }
  if (!this.isConnected || !this.contains(option) || Object.entries(expected).some(/** 检查选择后的选项身份是否仍一致。 */ ([key, value]) => identity()[key] !== value))
    return { success: false, stale: true, error: '选择目标被替换或选项变化，请重新观察' };
  const matched = option.selected && this.selectedIndex === expected.index && this.value === expected.value;
  const valid = this.validity.valid;
  return { success: matched && valid, matched, valid, value: this.selectedOptions[0]?.textContent.trim() || '',
    option: { index: this.selectedIndex, text: this.selectedOptions[0]?.textContent.trim() || '', value: this.value },
    ...(matched && valid ? {} : { error: matched ? '选项已匹配但字段校验未通过' : '当前选项与目标不符，未确认成功' }) };
}

globalThis.AgentEditing = { describeEditingTarget, selectNativeTarget };
if (typeof module !== 'undefined') module.exports = globalThis.AgentEditing;
