"""客观来源覆盖与去重，不判定模型结论真假。"""

import copy
import hashlib
import json


def missing(span, ranges):
    """从一个来源范围中扣除已经交付的区间，返回尚未覆盖的部分。"""
    start, end = span[3:]
    result = []
    for a,b in sorted((r[3],r[4]) for r in ranges if r[:3]==span[:3]):
        if b<=start or a>=end:
            continue
        if a>start:
            result.append([*span[:3],start,min(a,end)])
        start = max(start,b)
        if start>=end:
            break
    if start<end:
        result.append([*span[:3],start,end])
    return result


def row_ranges(row):
    """把证据行的定位转换为带文档和解析版本的统一范围表示。"""
    return [[row['doc_id'],row['parse_id'],s['block_id'],s['start'],s['end']] for s in row.get('locations',[]) if row.get('parse_id')]


def covered_chars(ranges):
    """合并同一原文块中的重叠区间，计算实际覆盖的字符数。"""
    groups = {}
    for doc, parse, block, start, end in ranges:
        groups.setdefault((doc, parse, block), []).append((start, end))
    total = 0
    for spans in groups.values():
        cursor = 0
        for start, end in sorted(spans):
            total += max(0, end - max(start, cursor))
            cursor = max(cursor, end)
    return total


def reconcile_pending(state):
    """未读是实际交付范围的投影；兼容旧检查点只存 read_args 的范围入口。"""
    pending = []
    for original in state.get('pending', []):
        item = copy.deepcopy(original)
        if item.get('reason') in ('字符范围超出边界', '表格行范围超出边界'):
            item['error_kind'] = 'invalid_location'
        if item.get('error_kind') == 'invalid_location':
            spans = item.get('resolution_ranges', [])
            if spans and not any(missing(s, state.get('ranges', [])) for s in spans):
                continue
            item['required'] = False
        if (state.get('settings', {}).get('EXECUTION_MODE') == 'task' and 'required' not in item
                and item.get('reason') == 'not_provided' and 'question' in item
                and item.get('read_args') and 'target' not in item and 'goal_id' not in item):
            item['required'] = False
            item['origin'] = 'search_candidate'
        spans = item.get('required_ranges')
        targets = item.get('read_args', {}).get('targets', [])
        exact = bool(targets) and all(t.get('mode') == 'range' and t.get('parse_id')
            and type(t.get('offset')) is int and type(t.get('end')) is int for t in targets)
        if not spans and exact:
            spans = [[t['doc_id'], t['parse_id'], t['block_id'], t['offset'], t['end']] for t in targets]
            item['required_ranges'] = spans
        if spans:
            unread = [part for span in spans for part in missing(span, state.get('ranges', []))]
            if not unread:
                continue
            item['unread_ranges'] = unread
            if exact:
                limit = state.get('settings', {}).get('READ_MAX_TARGETS', 4)
                next_targets = []
                for d, p, k, a, b in unread[:limit]:
                    template = next((t for t in targets if [t['doc_id'],t['parse_id'],t['block_id']] == [d,p,k]), {})
                    next_targets.append({**template, 'mode':'range', 'doc_id':d, 'parse_id':p,
                                         'block_id':k, 'offset':a, 'end':b})
                item['read_args']['targets'] = next_targets
        if item not in pending:
            pending.append(item)
    state['pending'] = pending


def novel_rows(rows, delivered):
    """从候选证据中选取尚未交付的内容，保留不可拆分单元的完整性。"""
    kept, seen = [], list(delivered)
    for original in rows:
        spans = row_ranges(original)
        if spans and not any(missing(span,seen) for span in spans):
            continue
        row = copy.deepcopy(original)
        if spans and not row.get('atomic'):
            parts = []
            for source in row.get('segments',[]):
                for _,_,_,a,b in missing([row['doc_id'],row['parse_id'],source['block_id'],source['start'],source['end']],seen):
                    parts.append({**source,'start':a,'end':b,'text':source['text'][a-source['start']:b-source['start']]})
            row['segments'] = parts
            row['locations'] = [{k:v for k,v in p.items() if k!='text'} for p in parts]
            row['content'] = '\n'.join(p['text'] for p in parts)
        kept.append(row)
    return kept


def register_goals(state, rows):
    """将检索发现的证据包要求登记为本轮需要补齐的读取目标。"""
    goals = state.setdefault('goals',[])
    for row in rows:
        if not row.get('bundle_id') or row.get('bundle_role')!='core' or not row.get('required_ranges'):
            continue
        identity = [row.get('doc_id'),row.get('parse_id'),row['bundle_id']]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
        required = row['required_ranges']
        spans = [[identity[0],identity[1],s['block_id'],s['start'],s['end']] for s in required]
        if any(g['id']==key for g in goals):
            continue
        goals.append({'id':key,'doc_id':row['doc_id'],'node_id':row.get('node_id'),
            'label':(row.get('node_path') or [row.get('question','')])[-1][:120],
            'question':row.get('question','')[:160], 'required_ranges':spans,'status':'located',
            **({'read_units':row['read_units']} if row.get('read_units') else {})})


def update_goals(state, kb_id):
    """依据实际交付范围更新目标状态，并生成尚缺原文的读取入口。"""
    from rag.document_config import READ_MAX_TARGETS
    pending = [p for p in state['pending'] if p.get('reason')!='goal_unread']
    for goal in state.get('goals',[]):
        unread = [part for span in goal['required_ranges'] for part in missing(span,state['ranges'])]
        required_chars = sum(s[4]-s[3] for s in goal['required_ranges'])
        goal['status'] = 'provided' if not unread else 'partial' if sum(s[4]-s[3] for s in unread)<required_chars else 'located'
        goal['missing_ranges'] = unread
        if unread:
            doc,pid,bid,start,end = unread[0]
            targets = []
            for unit in goal.get('read_units',[]):
                for i,span in enumerate(unit['sources']):
                    parts = missing([doc,pid,span['block_id'],span['start'],span['end']],state['ranges'])
                    if parts:
                        targets.append({**unit['target'],'cursor':i,'block_offset':parts[0][3]-span['start']})
                        break
            pending.append({'reason':'goal_unread','required':True,'goal_id':goal['id'],'question':goal['question'],
                'label':goal['label'],'required_ranges':goal['required_ranges'],
                'read_args':{'kb_id':kb_id,'targets':(targets or [{'doc_id':d,'parse_id':p,'mode':'range','block_id':b,'offset':a,'end':z}
                    for d,p,b,a,z in unread])[:READ_MAX_TARGETS]}})
    state['pending'] = pending


def public_goals(state):
    """返回可展示的知识库目标身份、问题和覆盖状态。"""
    return [{k:g[k] for k in ('id','doc_id','node_id','label','question','status')} for g in state.get('goals',[])]


def contained_index(rows):
    """查找范围被其他证据完全包含的候选行。"""
    for i,row in enumerate(rows):
        spans = row.get('locations',[])
        if not spans:
            continue
        identity = row.get('doc_id') or row.get('doc_ref'), row.get('parse_id')
        if not identity[0]:
            continue
        for j,other in enumerate(rows):
            if i==j or identity!=(other.get('doc_id') or other.get('doc_ref'),other.get('parse_id')):
                continue
            ranges = [[*identity,s['block_id'],s['start'],s['end']] for s in other.get('locations',[])]
            if all(not missing([*identity,s['block_id'],s['start'],s['end']],ranges) for s in spans):
                reverse = [[*identity,s['block_id'],s['start'],s['end']] for s in spans]
                equal = all(not missing(s,reverse) for s in ranges)
                prefer_other = (other.get('read_kind') == 'read', j) > (row.get('read_kind') == 'read', i)
                if not equal or prefer_other:
                    return i
    return None


def coalesce_evidence(rows):
    """合并被包含的重复证据，并保留相关需求及来源关联。"""
    result = copy.deepcopy(rows)
    while (index := contained_index(result)) is not None:
        removed = result.pop(index)
        identity = removed.get('doc_id') or removed.get('doc_ref'), removed.get('parse_id')
        spans = [[*identity, s['block_id'], s['start'], s['end']] for s in removed['locations']]
        for other in result:
            if identity != (other.get('doc_id') or other.get('doc_ref'), other.get('parse_id')):
                continue
            ranges = [[*identity, s['block_id'], s['start'], s['end']] for s in other.get('locations', [])]
            if all(not missing(s, ranges) for s in spans):
                for field in ('need_ids', 'warnings'):
                    values = list(dict.fromkeys([*other.get(field, []), *removed.get(field, [])]))
                    if values:
                        other[field] = values
                break
    return result


def prune_evidence(rows, needs=()):
    """收尾优先移除重复/补充证据；不能从一个证据包中裁去条件而保留结论。"""
    if not rows:
        return []
    reduced = coalesce_evidence(rows)
    if len(reduced) < len(rows):
        return reduced
    if needs:
        required = {n['id']:set(n.get('citations', [])) for n in needs}
        pinned = {ref for n in needs for ref in n.get('required_citations', [])}
        def owners(row):
            """确定证据行支撑的需求及文档版本，用于裁减时保护必要依据。"""
            return {(n['id'], row.get('doc_id') or row.get('doc_ref'), row.get('parse_id'))
                    for n in needs if row.get('citation') in required[n['id']]
                    or not required[n['id']] and n['id'] in row.get('need_ids', [])}
        counts = {}
        for row in rows:
            for owner in owners(row):
                counts[owner] = counts.get(owner, 0) + 1
        removable = [i for i,row in enumerate(rows) if row.get('citation') not in pinned
                     and all(counts[nid] > 1 for nid in owners(row))]
        if not removable:
            return rows
        removed = max(removable, key=lambda i:(not owners(rows[i]), len(rows[i].get('content', ''))))
        return [row for i,row in enumerate(rows) if i != removed]
    groups = {}
    for i,row in enumerate(rows):
        key = (row.get('doc_id') or row.get('doc_ref'),row.get('parse_id'),
               row.get('bundle_id') or row.get('question') or row.get('citation'))
        groups.setdefault(key,[]).append(i)
    optional = [i for i,r in enumerate(rows) if r.get('bundle_role')=='detail']
    if optional:
        remove = max(optional,key=lambda i:len(rows[i].get('content','')))
        return [r for i,r in enumerate(rows) if i!=remove]
    # 必要包本身也装不下时按整包撤下，并由调用方声明未纳入，不能保留半个公式。
    removed = set(next(reversed(groups.values())))
    return [r for i,r in enumerate(rows) if i not in removed]
