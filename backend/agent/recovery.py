"""任务内失败账本。只比较相关状态，不把新编号、时间或无关文字当成进展。"""
import hashlib
import json


def describe(action, page):
    target = next((e for e in page.interactive_elements if e.get('id') == action.index), {})
    identity = target.get('control_key') or target.get('target_key') or target.get('node_ref') or f'legacy:{action.index}'
    if target.get('control_key'):
        target = next((e for e in page.interactive_elements if e.get('control_key') == identity and e.get('tag') != 'label'), target)
    relevant = {k: target.get(k) for k in ('checked', 'indeterminate', 'aria_expanded', 'enabled', 'popup_ref')}
    relevant['field_digest'] = (target.get('field_snapshot') or {}).get('digest')
    relevant['box'] = target.get('bounding_box')
    relevant['popups'] = sorted(p.get('key', p.get('ref', '')) for p in page.popups if p.get('ref') == target.get('popup_ref'))
    input_params = {k: action.params[k] for k in ('text', 'option_text', 'clear', 'key', 'modifiers') if k in action.params}
    return {'target': identity, 'document': page.document_epoch, 'url': page.url,
            'state': relevant, 'type': action.type, 'expectation': action.expectation,
            'params_hash': hashlib.sha256(json.dumps(input_params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


def record(session, action, result, page):
    state = session.recovery_state
    pending = state.pop('pending', None)
    if not pending:
        return
    verification = result.verification or {}
    state['last'] = {'action_id': action.action_id, 'type': action.type, 'target': pending['target'],
        'dispatch_success': result.success, 'verification': verification, 'error_code': result.error_code,
        'click_effects': result.click_effects, 'target_probe': result.target_probe,
        'expectation': action.expectation, 'goal': session.progress[:160]}
    after = describe(action, page)
    # 下标可能已指向新节点；后态不能靠旧 index 重定位。
    old_target = pending['target']
    target = next((e for e in page.interactive_elements if (e.get('control_key') or e.get('target_key') or e.get('node_ref')) == old_target), None)
    if target is not None:
        rebound = action.model_copy(update={'index': target['id']})
        after = describe(rebound, page)
    else:
        after['target'] = old_target
        after['state'] = None
    attempts = state.setdefault('attempts', [])
    if action.type == 'inspect_target' and result.success and (result.target_probe or {}).get('clear') is True:
        hit_failures = {'occluded', 'disabled', 'no_geometry', 'outside_viewport', 'not_visible', 'independent_control', 'unsupported_hit_test', 'target_stale'}
        state['attempts'] = [a for a in attempts if not (old_target in a['targets'] and a['reason'] in hit_failures)]
        state['feedback'] = ''
        return
    failed = not result.success or verification.get('status') in ('not_satisfied', 'unknown') or (
        result.target_probe is not None and result.target_probe.get('clear') is not True)
    if not failed:
        if verification.get('status') == 'satisfied':
            state['attempts'] = [a for a in attempts if a['target'] != old_target]
            state['feedback'] = '本次声明的局部条件已确认；继续检查剩余任务，不因一次局部成功宣称全部完成。'
        return
    reason = result.error_code or (result.target_probe or {}).get('reason') or verification.get('reason') or verification.get('status') or 'action_failed'
    hits = sorted({c.get('hit', {}).get('path', '') for c in (result.click_diagnostic or {}).get('checks', []) if c.get('hit')})
    blocker = f"{(result.click_diagnostic or {}).get('frame_id', '')}:{'|'.join(hits)}" if hits else old_target
    # 相同遮挡可覆盖 input/label/父节点；同时保留各目标，供下一次重绑定比较。
    key = (pending['document'], reason, blocker, pending['type'])
    existing = next((a for a in attempts if tuple(a['key']) == key and a['state'] == after['state']), None)
    if existing:
        existing['count'] += 1
        existing['targets'] = list(dict.fromkeys([*existing['targets'], old_target]))[-8:]
        existing['target'] = old_target
    else:
        attempts.append({**pending, 'state': after['state'], 'key': list(key), 'reason': reason, 'count': 1, 'targets': [old_target]})
    state['attempts'] = attempts[-12:]
    state['feedback'] = f'已记录 {action.type} 未确认达成目标：{reason}。依据新观察换入口、处理阻挡或 inspect_target 只读检查；任务资料与成果仍保留。'


def context(session):
    state = session.recovery_state
    if not state.get('attempts') and not state.get('last'):
        return ''
    rows = [{k: a[k] for k in ('type', 'target', 'reason', 'count')} for a in state.get('attempts', [])[-6:]]
    return ('## 自动恢复账本（执行事实，不是动作禁令；局部结果不等于整个任务结果）\n'
            + json.dumps({'attempts': rows, 'last_result': state.get('last')}, ensure_ascii=False)
            + '\n' + state.get('feedback', ''))
