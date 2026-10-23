"""只补完已明确选择的原文范围；预览、搜索候选不自动升级为通读任务。"""

import hashlib
import json

from search import kb_coverage as coverage


def record(state, kb_id, items):
    """登记显式选择的读取范围及交付定位，分页请求合并到同一范围任务。"""
    scopes = state.setdefault('read_scopes', [])
    for item in items:
        target = item.get('scope_target', item['target'])
        # 续读位置不参与范围身份，同一目标的多次分页读取归入同一覆盖记录。
        base = {k:v for k,v in target.items() if k not in ('cursor','block_offset','intent')}
        key = hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()[:16]
        scope = next((s for s in scopes if s['id'] == key), None)
        required = target.get('intent', 'preview' if target['mode'] in ('page','section') else 'complete') == 'complete'
        if scope is None:
            scope = {'id':key,'kb_id':kb_id,'target':base,'required':required,'need_ids':[],
                     'requested_ranges':[], 'read_positions':[], 'error':None}
            scopes.append(scope)
        scope['required'] |= required
        scope['need_ids'] = list(dict.fromkeys([*scope['need_ids'], *item.get('need_ids', [])]))
        for span in item.get('requested_ranges', []):
            if span not in scope['requested_ranges']:
                scope['requested_ranges'].append(span)
        for position in item.get('read_positions', []):
            if position not in scope.setdefault('read_positions', []):
                scope['read_positions'].append(position)
        scope['error'] = item.get('error') if item['delivery'] == 'failed' else None
        scope['error_kind'] = item.get('error_kind')
        scope['resolution_ranges'] = item.get('resolution_ranges', [])
        scope['repair_read'] = item.get('repair_read')
    reconcile(state)


def reconcile(state):
    """根据实际已读范围重算缺口，并区分定位失效、读取失败及完成状态。"""
    for scope in state.get('read_scopes', []):
        if scope.get('error') in ('字符范围超出边界', '表格行范围超出边界'):
            scope['error_kind'] = 'invalid_location'
        unread = [p for span in scope['requested_ranges'] for p in coverage.missing(span, state['ranges'])]
        scope['unread_ranges'] = unread
        if scope.get('error_kind') == 'invalid_location':
            spans = scope.get('resolution_ranges', [])
            resolved = spans and not any(coverage.missing(s, state['ranges']) for s in spans)
            scope['status'] = 'resolved' if resolved else 'invalid'
        else:
            scope['status'] = 'failed' if scope['error'] else 'partial' if unread else 'complete'


def pending(state):
    """返回仍需要补齐且定位有效的显式通读范围。"""
    reconcile(state)
    return [s for s in state.get('read_scopes', []) if s['required'] and s['status'] not in ('complete','invalid','resolved')]


def next_read(state, kb_id, limit):
    """为待补齐范围生成下一批精确读取目标，沿用原有节点或块内定位。"""
    targets, ids = [], []
    for scope in pending(state):
        if scope['kb_id'] != kb_id or scope['error']:
            continue
        doc,pid,block,start,end = scope['unread_ranges'][0]
        target = {'doc_id':doc,'parse_id':pid,'mode':'range','block_id':block,'offset':start,'end':end}
        for position in scope.get('read_positions', []):
            span = position['range']
            if span[:3] != [doc,pid,block] or not span[3] <= start < span[4]:
                continue
            if position['atomic_start'] and span[3] != start:
                continue
            target = dict(position['read_args']['targets'][0])
            if target['mode'] == 'range':
                target['offset'] = start
            else:
                target['block_offset'] = target.get('block_offset',0) + start-span[3]
            break
        target['intent'] = 'complete'
        if target not in targets:
            targets.append(target)
        ids = list(dict.fromkeys([*ids,*scope['need_ids']]))
        if len(targets) >= limit:
            break
    if targets:
        return {'kb_id':kb_id,'targets':targets, **({'need_ids':ids} if ids else {})}


def report(state):
    """汇总读取范围的客观覆盖状态，不把已交付等同于语义核验。"""
    gaps = pending(state)
    scopes = sorted(state.get('read_scopes', []), key=lambda s:not (s['required'] and s['status']!='complete'))
    invalid = [s for s in scopes if s['status'] == 'invalid']
    return {'status':'partial' if gaps else 'invalid_selection' if invalid else 'selected_reads_complete' if any(s['required'] and s['status']=='complete' for s in scopes)
            else 'preview_only' if scopes else 'not_requested',
            'semantics_verified':False, 'missing_scope_count':len(gaps),
            'invalid_scope_count':len(invalid),
            'invalid_scopes':[{'target':s['target'], 'error':s['error'], 'repair_read':s.get('repair_read')} for s in invalid[:8]],
            'scope_count':len(scopes), 'required_scope_count':sum(s['required'] for s in scopes),
            'scopes':[{'id':s['id'],'need_ids':s['need_ids'],'target':s['target'],'required':s['required'],
                       'status':s['status'],'unread_chars':coverage.covered_chars(s['unread_ranges']),
                       **({'error':s['error']} if s['error'] else {})}
                      for s in gaps[:8]]}
