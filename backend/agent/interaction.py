"""有限页面条件的绑定与回读；不把事件派发当成任务完成。"""


def prepare(action, raw, page):
    """把动作声明的预期转换为绑定当前节点或弹层的可验证条件。"""
    target = next((e for e in page.interactive_elements if e.get('id') == action.index), {})
    requested = raw.get('expect')
    if requested is None and action.type == 'click' and target.get('control_kind') in ('checkbox', 'radio', 'switch'):
        if isinstance(target.get('checked'), bool):
            requested = {'kind': 'checked', 'value': True if target['control_kind'] == 'radio' else not target['checked']}
    if requested is None:
        return
    if not isinstance(requested, dict):
        raise ValueError('expect 必须是条件对象')
    kind = requested.get('kind')
    expectation = {'kind': kind, 'document_epoch': page.document_epoch}
    if kind in ('checked', 'expanded', 'enabled'):
        if 'index' in requested:
            target = next((e for e in page.interactive_elements if e.get('id') == requested['index']), {})
        if not target.get('node_ref') or not isinstance(requested.get('value'), bool):
            raise ValueError('状态验证必须绑定当前目标并指定布尔 value')
        if kind == 'checked' and not target.get('control_kind') and not isinstance(target.get('checked'), bool):
            raise ValueError('checked 验证必须指向复选框/单选框/开关；若按钮改变另一个控件，请在 expect.index 指定其本轮编号')
        if kind == 'expanded' and target.get('aria_expanded') not in ('true', 'false'):
            raise ValueError('expanded 验证目标没有实时 aria-expanded；请指定正确 expect.index 或使用 popup_opened')
        expectation.update(target_ref=target['node_ref'], value=requested['value'])
    elif kind == 'popup_closed':
        ref = requested.get('popup_ref') or target.get('popup_ref')
        if not ref or not any(p.get('ref') == ref for p in page.popups):
            raise ValueError('关闭验证必须指定本轮已观察到的 popup_ref')
        expectation['popup_ref'] = ref
        expectation['popup_key'] = next(p.get('key') for p in page.popups if p.get('ref') == ref)
    elif kind == 'popup_opened':
        expectation.update(popup_refs=[p['ref'] for p in page.popups], frame_id=target.get('frame_id', ''))
    elif kind == 'url_changed':
        expectation['url'] = page.url
    else:
        raise ValueError('expect.kind 仅支持 checked/expanded/enabled/popup_closed/popup_opened/url_changed')
    action.expectation = expectation


def evaluate(expect, page):
    """依据新观察核实局部预期；目标替换或观察不完整时保留未知状态。"""
    if not expect:
        return {'status': 'not_requested'}
    kind = expect['kind']
    result = {'kind': kind, 'status': 'unknown', 'observation_id': page.observation_id}
    if kind == 'invalid':
        return {**result, 'reason': 'invalid_expectation', 'details': expect['reason']}
    if kind == 'url_changed':
        return {**result, 'status': 'satisfied' if page.url != expect['url'] else 'not_satisfied'}
    if page.document_epoch != expect['document_epoch']:
        return {**result, 'reason': 'document_changed'}
    if kind == 'popup_closed':
        present = any(p.get('ref') == expect['popup_ref'] for p in page.popups)
        if not present and any(p.get('key') and p.get('key') == expect.get('popup_key') for p in page.popups):
            return {**result, 'reason': 'popup_replaced'}
        return {**result, 'status': 'not_satisfied' if present else 'satisfied' if page.observation_complete else 'unknown'}
    if kind == 'popup_opened':
        found = any(p.get('frame_id') == expect['frame_id'] and p.get('ref') not in expect['popup_refs'] for p in page.popups)
        return {**result, 'status': 'satisfied' if found else 'not_satisfied' if page.observation_complete else 'unknown'}
    target = next((e for e in page.interactive_elements if e.get('node_ref') == expect['target_ref']), None)
    if not target:
        return {**result, 'reason': 'target_replaced_or_absent'}
    value = (None if target.get('indeterminate') else target.get('checked')) if kind == 'checked' else (
        {'true': True, 'false': False}.get(target.get('aria_expanded')) if kind == 'expanded' else target.get('enabled'))
    return {**result, 'actual': value, 'status': 'unknown' if not isinstance(value, bool) else 'satisfied' if value == expect['value'] else 'not_satisfied'}
