"""对已定位的节点原文按连续范围分页，不用相关性排序改变显式阅读位置。"""

import json
from rag import document_config as C
from storage import kb_store as KS


def split_node(row):
    """按连续原文范围分页节点内容，保留关联公式等需要完整交付的片段。"""
    spans = row.get('segments', [])
    if not spans:
        return []
    with KS._transaction() as conn:
        blocks = {b['block_id']: b for b in conn.execute(
            'SELECT block_id,kind,structure_json FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND block_id IN (SELECT value FROM json_each(?))',
            (row.get('structure_id',''), row['parse_id'], json.dumps(list({s['block_id'] for s in spans}))))}
    groups, current, length, linked = [], [], 0, set()
    for index, span in enumerate(spans):
        block = blocks[span['block_id']]
        structure = json.loads(block['structure_json'])
        members = set(structure.get('formula_members', []))
        atomic = block['kind'] in ('table', 'display_formula', 'formula_number') or bool(members)
        connected = span['block_id'] in linked or bool(members & {p['block_id'] for _,p,_ in current})
        if current and not connected and length + len(span['text']) > C.READ_PASSAGE_CHARS:
            groups.append(current)
            current, length, linked = [], 0, set()
        current.append((index, span, atomic))
        length += len(span['text']) + 1
        linked.update(members)
    if current:
        groups.append(current)
    output = []
    starts = row.get('node_source_starts') or [s['start'] for s in spans]
    for group in groups:
        first, last = group[0][0], group[-1][0]
        parts = [s for _,s,_ in group]
        following = last + 1
        next_read = ({'kb_id':row['kb_id'],'targets':[{'doc_id':row['doc_id'],
            'structure_id':row['structure_id'],'mode':'node','node_id':row['node_id'],
            'cursor':row['node_cursor']+following,'block_offset':0}]} if following < len(spans) else row.get('next_read'))
        output.append({**row,'segments':parts,'content':'\n'.join(s['text'] for s in parts),
            'locations':[{k:v for k,v in s.items() if k!='text'} for s in parts],
            'node_cursor':row['node_cursor']+first,'node_source_starts':starts[first:last+1],
            'node_source_start':starts[first], 'next_read':next_read,
            'atomic':any(a for _,_,a in group), 'read_kind':'read',
            'complete':not next_read, 'evidence_role':row.get('evidence_role') or 'continuous_passage'})
    return output
