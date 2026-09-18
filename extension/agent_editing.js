// 在目标节点所属页面执行；描述对象只存活于一次动作，不跨观察复用 DOM 引用。
function describeEditingTarget(observe = false) {
  const node = this;
  const doc = node.ownerDocument;
  const win = doc.defaultView;
  const wrapper = node.closest('.CodeMirror');
  let cm = wrapper?.CodeMirror;
  const cmInput = cm?.getInputField?.();
  const surface = node === wrapper || node === cmInput ||
    ['CodeMirror-scroll', 'CodeMirror-sizer', 'CodeMirror-lines', 'CodeMirror-code'].some(c => node.classList.contains(c));
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
  const value = () => {
    if (cm) return cm.getValue('\n');
    if (kind === 'native') return input.value;
    if (kind !== 'contenteditable') return '';
    // 浏览器用块元素分行，并在空行末尾保留占位 BR；innerText 会额外计算这些占位换行。
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
  const focused = () => {
    if (!doc.hasFocus()) return false;
    const active = input.getRootNode().activeElement;
    return active === input || kind === 'contenteditable' && input.contains(active);
  };
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
    if (operation === 'read') return { ...s, value: value() };
    if (!s.editable) return { success: false, error: '目标不可编辑或只读，未修改内容' };
    if (!s.focused) return { success: false, error: '编辑焦点已转移，未继续输入' };
    if (operation === 'guard') return s;
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
    return { ...s, preview: s.success && kind !== 'none' && input.type !== 'password' ? value().slice(0, 120) : '' };
  }
  return { call };
}

globalThis.AgentEditing = { describeEditingTarget };
if (typeof module !== 'undefined') module.exports = globalThis.AgentEditing;
