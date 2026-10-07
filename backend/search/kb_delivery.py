"""读取交付账本：只报告实际传给模型的原文，不判断答案是否正确。"""

import copy

from search import kb_coverage as coverage


def prepare(rows, target, kb_id):
    cursor, offset = target.get('cursor', 0), target.get('block_offset', 0)
    if not 0 <= cursor <= len(rows) or (cursor == len(rows) and offset):
        raise ValueError('续读游标超出目标范围，请复用 next_read')
    result = []
    base = {k: v for k, v in target.items() if k not in ('cursor', 'block_offset')}
    for i, original in enumerate(rows[cursor:], cursor):
        row = copy.deepcopy(original)
        skip = offset if i == cursor else 0
        if skip > len(row['content']) or (skip and row.get('atomic')):
            raise ValueError('续读位置超出范围或拆分了原子单元，请复用 next_read')
        parts, remaining = [], skip
        for span in row['segments']:
            if remaining >= len(span['text']):
                remaining -= len(span['text']) + 1
                continue
            start = max(0, remaining)
            parts.append({**span, 'start': span['start'] + start, 'text': span['text'][start:]})
            remaining = 0
        if not parts:
            continue
        row.update(segments=parts, locations=[{k:v for k,v in p.items() if k != 'text'} for p in parts],
                   content='\n'.join(p['text'] for p in parts), read_cursor=i, read_offset=skip,
                   read_target=base, next_read=({'kb_id':kb_id, 'targets':[{**base, 'cursor':i+1}]}
                                               if i+1 < len(rows) else None))
        result.append(row)
    return result


def resume(row, chars=0):
    if 'read_target' in row:
        if row['read_target']['mode'] in ('block','range') and len(row['segments']) == 1:
            span = row['segments'][0]
            return {'kb_id':row['kb_id'], 'targets':[{'doc_id':row['doc_id'], 'parse_id':row['parse_id'],
                    'mode':'range', 'block_id':span['block_id'], 'offset':span['start']+chars, 'end':span['end']}]}
        target = {**row['read_target'], 'cursor':row['read_cursor']}
        offset = row['read_offset'] + chars
        if offset:
            target['block_offset'] = offset
    else:
        spans = row['segments']
        starts = row.get('node_source_starts') or [row['node_source_start']]
        index, remaining = 0, chars
        while index < len(spans)-1 and remaining >= len(spans[index]['text']):
            remaining -= len(spans[index]['text']) + 1
            index += 1
        target = {'doc_id':row['doc_id'], 'structure_id':row['structure_id'], 'mode':'node', 'node_id':row['node_id'],
                  'cursor':row['node_cursor']+index,
                  'block_offset':spans[index]['start']-starts[index]+max(0,remaining)}
    if row.get('read_intent'):
        target['intent'] = row['read_intent']
    return {'kb_id':row['kb_id'], 'targets':[target]}


def progress(item, rows, *, details=False):
    result = {k:v for k,v in item.items() if not k.startswith('_') and k != 'target'}
    if item['outcome'] == 'error':
        return {**result, 'delivery':'failed', 'provided_blocks':0, 'partial':True,
                **({'target':item['target']} if details else {})}
    units = item.get('_units', [])
    selected = [r for r in rows if r.get('target_index') == item['target_index']]
    required = [s for r in units for s in coverage.row_ranges(r)]
    delivered = [s for r in selected for s in coverage.row_ranges(r)]
    unread = [part for s in required for part in coverage.missing(s, delivered)]
    total, missing = coverage.covered_chars(required), coverage.covered_chars(unread)
    result.update(delivery='complete' if not missing else 'partial' if total > missing else 'not_provided',
                  requested_chars=total, delivered_chars=total-missing, unread_chars=missing,
                  provided_blocks=len({s[2] for s in delivered}), partial=bool(missing))
    if missing:
        result['reason'] = 'single_result_limit'
        for row in units:
            if not any(coverage.missing(s, delivered) for s in coverage.row_ranges(row)):
                continue
            prefix = 0
            for span in row.get('segments', []):
                gaps = coverage.missing([row['doc_id'],row['parse_id'],span['block_id'],span['start'],span['end']],delivered)
                if gaps:
                    prefix += gaps[0][3] - span['start']
                    break
                prefix += len(span['text']) + 1
            # 原子证据不切割，显式复查仍从该单元原位置读取。
            result['next_read'] = resume(row, 0 if row.get('atomic') else prefix)
            break
    if details:
        result.update(target=item['target'], requested_ranges=required,
                      delivered_ranges=delivered, unread_ranges=unread)
        positions = []
        for row in units:
            prefix = 0
            for span in coverage.row_ranges(row):
                if not row.get('atomic') or row.get('read_mode') == 'node' or prefix == 0:
                    positions.append({'range':span, 'read_args':resume(row, prefix),
                                      'atomic_start':bool(row.get('atomic') and row.get('read_mode') != 'node')})
                prefix += span[4]-span[3]+1
        result['read_positions'] = positions
        if units:
            result['scope_target'] = {**item['target'], 'doc_id':units[0]['doc_id'], 'parse_id':units[0]['parse_id']}
            if units[0].get('structure_id'):
                result['scope_target']['structure_id'] = units[0]['structure_id']
            result['scope_target'].pop('doc_name', None)
    else:
        for key in ('mode','outcome','reason','requested_chars','delivered_chars'):
            result.pop(key, None)
    return result
