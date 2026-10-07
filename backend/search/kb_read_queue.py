"""只补完已明确选择的原文范围；预览、搜索候选不自动升级为通读任务。"""

import hashlib
import json

from search import kb_coverage as coverage


def record(state, kb_id, items):
    scopes = state.setdefault('read_scopes', [])
    for item in items:
        target = item.get('scope_target', item['target'])
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
    reconcile(state)


def reconcile(state):
    for scope in state.get('read_scopes', []):
        unread = [p for span in scope['requested_ranges'] for p in coverage.missing(span, state['ranges'])]
        scope['unread_ranges'] = unread
        scope['status'] = 'failed' if scope['error'] else 'partial' if unread else 'complete'


def pending(state):
    reconcile(state)
    return [s for s in state.get('read_scopes', []) if s['required'] and s['status'] != 'complete']


def next_read(state, kb_id, limit):
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
    gaps = pending(state)
    scopes = sorted(state.get('read_scopes', []), key=lambda s:not (s['required'] and s['status']!='complete'))
    return {'status':'partial' if gaps else 'selected_reads_complete' if any(s['required'] for s in scopes)
            else 'preview_only' if scopes else 'not_requested',
            'semantics_verified':False, 'missing_scope_count':len(gaps),
            'scope_count':len(scopes), 'required_scope_count':sum(s['required'] for s in scopes),
            'scopes':[{'id':s['id'],'need_ids':s['need_ids'],'target':s['target'],'required':s['required'],
                       'status':s['status'],'unread_chars':coverage.covered_chars(s['unread_ranges']),
                       **({'error':s['error']} if s['error'] else {})}
                      for s in gaps[:8]]}
