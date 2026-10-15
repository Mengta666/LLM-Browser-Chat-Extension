"""结构化文档检索、页块目录与精确范围读取。"""

import json
import re
from collections import defaultdict
import jieba

from agent.memory.config import CHAT_USER_ID, INSTRUCT_KB, MEMORY_TYPE_KB_CHUNK
from rag import document_config as C, kb_vectors as V
from rag.document_structure import identifiers
from rag.embedder import embed_query
from rag.reranker import rerank_chunks
from rag.retrieval_trace import record_stage
from storage import kb_store as KS, kb_document_store as DS, kb_text_store as TS
from observability.logger import get_logger


class ReadRangeError(ValueError):
    code = 'kb_range_out_of_bounds'

    def __init__(self, message, kb_id, doc_id, snapshot, block_id, length, row_count=None):
        super().__init__(message)
        target = {'doc_id':doc_id, **snapshot, 'mode':'block', 'block_id':block_id}
        self.details = {'valid_range':{'offset':0, 'end':length},
                        'repair_read':{'kb_id':kb_id, 'targets':[target]},
                        'resolution_ranges':[[doc_id,snapshot['parse_id'],block_id,0,length]],
                        'notice':'原请求未执行、未自动裁剪。核对范围后重新指定，或显式使用 repair_read 读取整块。'}
        if row_count is not None:
            self.details['valid_rows'] = {'row_start':0, 'row_end':row_count}


def formula_numbers(query):
    number = r'[（(]?\s*[0-9]+(?:[.\-][0-9]+)*[a-z]?\s*[)）]?'
    pattern = rf'(?:公式|方程|不等式|式|equations?|eqs?\.?)[\s:：]*({number}(?:\s*(?:[,，、和及与&]|and)\s*(?:公式|式)?\s*{number})*)'
    return list(dict.fromkeys(n for m in re.finditer(pattern, query, re.I)
                             for n in re.findall(r'[0-9]+(?:[.\-][0-9]+)*[a-z]?', m[1], re.I)))[:8]


def diversify(hits, limit):
    groups = defaultdict(list)
    seen = set()
    for hit in hits:
        identity = (hit['doc_id'], re.sub(r'\s+', ' ', hit.get('content', '')).strip())
        if identity in seen:
            continue
        seen.add(identity)
        groups[hit['doc_id']].append(hit)
    result = []
    while any(groups.values()) and len(result) < limit:
        for group in groups.values():
            if group and len(result) < limit:
                result.append(group.pop(0))
    return result


def deduplicate_ranges(hits):
    def covers(outer, inner):
        if (outer.get('doc_id'), outer.get('parse_id')) != (inner.get('doc_id'), inner.get('parse_id')):
            return False
        if outer.get('bundle_id') != inner.get('bundle_id'):
            return False
        if not outer.get('parse_id') or not inner.get('locations'):
            return False
        for part in inner['locations']:
            cursor = part['start']
            for span in sorted((p for p in outer.get('locations', []) if p['block_id'] == part['block_id']),
                               key=lambda p: p['start']):
                if span['start'] > cursor:
                    break
                cursor = max(cursor, span['end'])
                if cursor >= part['end']:
                    break
            if cursor < part['end']:
                return False
        return True

    result = []
    for hit in hits:
        if any(covers(previous, hit) for previous in result):
            continue
        contained = [i for i, previous in enumerate(result) if covers(hit, previous)]
        if contained:
            # 元信息入口与正文块可能互相包含；保留覆盖完整的原文，沿用最早候选的位置。
            primary_read = result[contained[0]].get('read_args')
            result[contained[0]] = {**hit, **({'read_args': primary_read} if primary_read else {})}
            result = [row for i, row in enumerate(result) if i not in contained[1:]]
        else:
            result.append(hit)
    return result


def outline(kb_id, doc_id, parse_id=None, page=None, offset=0, limit=20, section_id=None, view='blocks',
            structure_id=None, index_run_id=None, node_id=None, depth=1):
    from storage import kb_structure_store as HS
    if view == 'tree':
        if page is not None or section_id:
            raise ValueError('树目录使用 node_id 展开，不混用页或旧章节定位')
        result = HS.outline(kb_id,doc_id,parse_id=parse_id,structure_id=structure_id,index_run_id=index_run_id,
                            node_id=node_id,offset=offset,limit=limit,depth=depth)
        doc, _ = DS.resolve_version(kb_id,doc_id,result['parse_id'])
        return {**result,'filename':doc['filename'],'source_kind':'pdf_page' if doc['file_type']=='pdf' else 'text_span'}
    if view not in ('blocks', 'sections') or node_id:
        raise ValueError('view 必须为 blocks/sections/tree；node_id 仅用于 tree')
    snap = HS.snapshot(kb_id,doc_id,parse_id=parse_id,structure_id=structure_id,index_run_id=index_run_id)
    parse_id = snap['parse_id']
    doc, version = DS.resolve_version(kb_id, doc_id, parse_id)
    if page is not None and doc['file_type']!='pdf':
        raise ValueError('文本文件没有物理页；请使用节点、块或字符范围')
    section_nodes = {}
    if snap['structure_id'] and (view == 'sections' or section_id):
        nodes, _ = HS.load(snap['structure_id'])
        if nodes and nodes[0]['attributes'].get('review_range_version'):
            selected = [n for n in nodes if n['kind'] == 'section' and (page is None or n['page'] <= page <= n['end_page'])]
            if section_id:
                scope = HS.reviewed_section(snap['structure_id'], section_id)
                known = {n['node_id']: n for n in nodes}
                selected = [n for n in selected if n['node_id'] == scope['node_id'] or HS.descendant(n, scope['node_id'], known)]
            entries = [{'section_id': n['node_id'], 'node_id': n['node_id'], 'kind': 'section', 'title': n['title'],
                        'page': n['page'], 'end_page': n['end_page'], 'preview': n['preview'],
                        'role': 'section_heading', 'review_status': n['attributes']['review_status'],
                        'declared_level': n['attributes']['declared_level'],
                        'read_args': {'kb_id':kb_id,'targets':[{'doc_id':doc_id,'structure_id':snap['structure_id'],'mode':'node','node_id':n['node_id']}]}}
                       for n in selected]
            return {'doc_id':doc_id,'filename':doc['filename'],**snap,'source_kind':'pdf_page', 'view':'sections',
                    'blocks':entries[offset:offset+limit],'total_blocks':len(entries),'offset':offset,
                    'next_offset':offset+limit if offset+limit<len(entries) else None,
                    'notice':'范围级章节导航；模型推断未作语义核验，请按 node_id 读取。'}
    if view == 'sections' and snap['structure_id']:
        nodes, _ = HS.load(snap['structure_id'])
        section_nodes = {n['attributes']['heading_block']: n for n in nodes
                         if n['kind'] == 'section' and n['attributes'].get('heading_block')}
    with KS._transaction() as conn:
        condition, params = 'structure_id=? AND parse_id=?', [snap['structure_id'], version['parse_id']]
        if page is not None:
            condition += ' AND page=?'
            params.append(page)
        if section_id:
            condition += ' AND (section_id=? OR block_id=? OR EXISTS(SELECT 1 FROM json_each(structure_json,\'$.section_path\') WHERE value=?))'
            params.extend([section_id] * 3)
        if view == 'sections':
            from rag.document_layout import HEADINGS
            condition += " AND json_extract(structure_json,'$.index_excluded') IS NULL"
            items = [dict(r) for r in conn.execute(f'SELECT block_id,page,kind,section_id,substr(text,1,100) AS preview FROM kb_resolved_blocks WHERE {condition} ORDER BY page,ordinal', params)]
            entries = [r for r in items if r['block_id'] in section_nodes] if section_nodes else [r for r in items if r['kind'] in HEADINGS]
            if not entries:
                entries = list({r['page']: r for r in reversed(items)}.values())[::-1]
            rows = [{**r, 'end_page': section_nodes[r['block_id']]['end_page'] if r['block_id'] in section_nodes
                     else entries[i+1]['page'] if i+1 < len(entries) else page or version['page_count']}
                    for i, r in enumerate(entries)]
            total, rows = len(rows), rows[offset:offset+limit]
        else:
            total = conn.execute(f'SELECT count(*) FROM kb_resolved_blocks WHERE {condition}', params).fetchone()[0]
            rows = conn.execute(f'SELECT block_id,page,kind,section_id,json_extract(structure_json,\'$.formula_label\') AS formula_number,length(text) AS length,substr(text,1,100) AS preview FROM kb_resolved_blocks WHERE {condition} ORDER BY page,ordinal LIMIT ? OFFSET ?',
                                (*params, limit, offset)).fetchall()
    return {'doc_id': doc_id, 'filename': doc['filename'], **snap,
            'source_kind':'pdf_page' if doc['file_type']=='pdf' else 'text_span',
            'page_count': version['page_count'], 'parsed_pages': version['completed_pages'],
            'view': view, 'blocks': [dict(r) for r in rows], 'total_blocks': total,
            'offset': offset, 'next_offset': offset + len(rows) if offset + len(rows) < total else None,
            'notice': '目录预览不是全文；PDF 的 OCR 为派生结果，文本文件按原文字符定位。'}


def read(kb_id, doc_id, parse_id=None, page=None, block_id=None, offset=0, end=None, row_start=None, row_end=None,
         section_id=None, formula_number=None, include_related=False, structure_id=None, index_run_id=None,
         node_id=None, cursor=0, block_offset=0):
    from storage import kb_structure_store as HS
    snap = HS.snapshot(kb_id,doc_id,parse_id=parse_id,structure_id=structure_id,index_run_id=index_run_id)
    parse_id = snap['parse_id']
    if section_id and snap['structure_id']:
        section = HS.reviewed_section(snap['structure_id'], section_id)
        if section:
            if node_id or include_related:
                raise ValueError('范围级章节不能混用节点或关联展开')
            sources = section['sources']
            blocks = {b['block_id']:b for b in DS.blocks(parse_id, structure_id=snap['structure_id'])}
            if formula_number is not None:
                nodes, _ = HS.load(snap['structure_id'])
                known = {n['node_id']:n for n in nodes}
                matches = [n for n in nodes if n['kind']=='formula' and n['attributes']['number']==str(formula_number)
                           and HS.descendant(n,section['node_id'],known) and (page is None or n['page']==page)]
                if len(matches)!=1:
                    raise KS.KBConflict('章节内公式不存在或不唯一')
                sources = matches[0]['sources']
            if block_id:
                if block_id not in blocks:
                    raise KS.KBNotFound('指定页块不存在')
                if formula_number is not None or row_end is not None and row_start is None:
                    raise ValueError('块与公式不能混用；row_end 需要 row_start')
                requested_start = offset
                requested_end = len(blocks[block_id]['text']) if end is None else end
                if row_start is not None:
                    if offset or end is not None:
                        raise ValueError('行定位与字符定位不能混用')
                    ranges = blocks[block_id]['structure'].get('row_ranges', [])
                    row_end = row_start+1 if row_end is None else row_end
                    if not 0 <= row_start < row_end <= len(ranges):
                        raise ValueError('表格行范围超出边界')
                    requested_start, requested_end = ranges[row_start][0], ranges[row_end-1][1]
                from rag.structure_projection import merged
                spans = merged([s for s in sources if s['block_id']==block_id])
                if not any(s['start']<=requested_start<=requested_end<=s['end'] for s in spans):
                    raise ValueError('指定字符范围超出章节，不会自动扩大读取范围')
                if row_start is not None:
                    return read(kb_id,doc_id,**snap,block_id=block_id,row_start=row_start,row_end=row_end,page=page)
                return read(kb_id,doc_id,**snap,block_id=block_id,offset=offset,end=requested_end,page=page)
            if offset or end is not None or row_start is not None or row_end is not None:
                raise ValueError('章节读取请使用 node_id 续读；字符/表格行定位需指定块')
            if page is None and formula_number is None:
                return read(kb_id,doc_id,**snap,node_id=section['node_id'],cursor=cursor,block_offset=block_offset)
            parts = [read(kb_id,doc_id,**snap,block_id=s['block_id'],offset=s['start'],end=s['end'])
                     for s in sources if page is None or blocks[s['block_id']]['page']==page]
            if not parts:
                raise KS.KBNotFound('章节内没有符合范围的正文')
            return {**parts[0],'content':'\n'.join(p['content'] for p in parts),'segments':[s for p in parts for s in p['segments']],
                    'locations':[s for p in parts for s in p['locations']],'next_read':None}
    if node_id:
        if any((block_id,page,section_id,formula_number,include_related,offset,end is not None,row_start is not None,row_end is not None)):
            raise ValueError('节点读取不能混用其他定位方式')
        from rag.hierarchical_retrieval import bundles
        parts = bundles(kb_id,doc_id,snap,node_id,sequential=True,cursor=cursor,block_offset=block_offset)
        if not parts:
            return {'kb_id':kb_id,'doc_id':doc_id,**snap,'node_id':node_id,'content':'','segments':[],
                    'locations':[],'next_read':None,'read_kind':'read','warnings':[],'complete':True}
        return {**parts[0], 'content':'\n'.join(p['content'] for p in parts),
                'segments':[s for p in parts for s in p['segments']],
                'locations':[s for p in parts for s in p['locations']], 'next_read':None,
                'node_source_starts':[p['node_source_start'] for p in parts], 'complete':True}
    doc, version = DS.resolve_version(kb_id, doc_id, parse_id)
    if doc['file_type']!='pdf' and (page is not None or formula_number is not None):
        raise ValueError('文本文件不支持物理页或 PDF 公式编号定位；请使用节点、块或字符范围')
    if sum(bool(v) for v in (block_id, formula_number)) > 1:
        raise ValueError('block_id 与 formula_number 不能混用')
    if include_related and (not block_id or offset or end is not None or row_start is not None or row_end is not None):
        raise ValueError('关联展开必须指定块，不能与字符或行范围混用')
    if row_end is not None and row_start is None:
        raise ValueError('row_end 需要 row_start')
    if row_start is not None and (offset or end is not None):
        raise ValueError('行定位与字符定位不能混用')
    if not block_id:
        if offset or end is not None or row_start is not None or row_end is not None:
            raise ValueError('字符或表格行范围必须同时指定 block_id；page 单独使用时按页读取')
        with KS._transaction() as conn:
            condition, params = 'structure_id=? AND parse_id=?', [snap['structure_id'], version['parse_id']]
            if page is not None:
                condition += ' AND page=?'
                params.append(page)
            if section_id:
                condition += ' AND (section_id=? OR block_id=? OR EXISTS(SELECT 1 FROM json_each(structure_json,\'$.section_path\') WHERE value=?))'
                params.extend([section_id] * 3)
            if formula_number is not None:
                condition += " AND kind='display_formula' AND json_extract(structure_json,'$.formula_label')=?"
                params.append(str(formula_number))
                formulas = conn.execute(f'SELECT block_id,structure_json FROM kb_resolved_blocks WHERE {condition}', params).fetchall()
                if not formulas:
                    # 旧发布版本未持久化公式关系，按原坐标关联；不改写旧解析档案。
                    labels = conn.execute("SELECT block_id,page,bbox_json,section_id FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND kind='formula_number' AND trim(text, ' ()（）' || char(9) || char(10) || char(13))=?",
                                          (snap['structure_id'], version['parse_id'], str(formula_number))).fetchall()
                    for label in labels:
                        if (page is not None and label['page'] != page) or (section_id and label['section_id'] != section_id):
                            continue
                        box = json.loads(label['bbox_json'])
                        matches = []
                        for body in conn.execute("SELECT block_id,bbox_json FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND page=? AND kind='display_formula'", (snap['structure_id'], version['parse_id'], label['page'])):
                            a = json.loads(body['bbox_json'])
                            if a and box and abs((box[1]+box[3])-(a[1]+a[3])) <= max(box[3]-box[1],a[3]-a[1]):
                                matches.append(body['block_id'])
                        if len(matches) == 1:
                            formulas.append({'block_id':matches[0], 'structure_json':json.dumps({'formula_members':[matches[0],label['block_id']]})})
                if len(formulas) > 1:
                    raise KS.KBConflict('公式编号不唯一，请用 page 或 section_id 限定：' + ','.join(r['block_id'] for r in formulas[:10]))
                ids = json.loads(formulas[0]['structure_json'])['formula_members'] if formulas else []
            elif page is not None or section_id:
                ids = [r[0] for r in conn.execute(f'SELECT block_id FROM kb_resolved_blocks WHERE {condition} ORDER BY page,ordinal', params)]
            else:
                raise ValueError('必须指定页、块、章节或公式编号')
        if not ids:
            raise KS.KBNotFound('指定公式编号未在该文档范围内找到' if formula_number is not None else '指定页不存在或没有正文块')
        parts = [read(kb_id, doc_id, **snap, block_id=bid) for bid in ids]
        return {**parts[0], 'content': '\n'.join(p['content'] for p in parts),
                'segments': [s for p in parts for s in p['segments']],
                'locations': [s for p in parts for s in p['locations']], 'next_read': parts[-1]['next_read'],
                'warnings': list(dict.fromkeys(w for p in parts for w in p['warnings']))}
    with KS._transaction() as conn:
        if block_id:
            block = conn.execute('SELECT * FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND block_id=?', (snap['structure_id'], version['parse_id'], block_id)).fetchone()
        else:
            block = conn.execute('SELECT * FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND page=? ORDER BY ordinal LIMIT 1', (snap['structure_id'], version['parse_id'], page)).fetchone()
        if not block or (page is not None and block['page'] != page):
            raise KS.KBNotFound('指定页块不存在；空白页没有正文块')
        structure = json.loads(block['structure_json'])
        if section_id and section_id not in {block['section_id'], block['block_id'], *structure.get('section_path', [])}:
            raise KS.KBNotFound('指定块不属于该章节')
        text = block['text']
        if row_start is not None:
            ranges = structure.get('row_ranges', [])
            row_end = row_end if row_end is not None else row_start + 1
            if not 0 <= row_start < row_end <= len(ranges):
                raise ReadRangeError('表格行范围超出边界', kb_id, doc_id, snap, block['block_id'], len(text), len(ranges))
            offset, end = ranges[row_start][0], ranges[row_end - 1][1]
        end = len(text) if end is None else end
        if not 0 <= offset <= end <= len(text):
            raise ReadRangeError('字符范围超出边界', kb_id, doc_id, snap, block['block_id'], len(text))
        if block['page'] is None:
            next_block = conn.execute('SELECT block_id,page FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND ordinal>? ORDER BY ordinal LIMIT 1',
                                      (snap['structure_id'],version['parse_id'],block['ordinal'])).fetchone()
        else:
            next_block = conn.execute('SELECT block_id,page FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND (page>? OR (page=? AND ordinal>?)) ORDER BY page,ordinal LIMIT 1',
                                      (snap['structure_id'],version['parse_id'], block['page'], block['page'], block['ordinal'])).fetchone()
        p = conn.execute('SELECT warnings_json,truncated,printed_page FROM kb_pages WHERE parse_id=? AND page=?', (version['parse_id'], block['page'])).fetchone()
        p = p or {'warnings_json':'[]','truncated':0,'printed_page':None}
        from rag.document_layout import STATEMENT, statement_context
        related = structure.get('related_blocks', structure.get('formula_members', []))
        if (include_related or STATEMENT.match(text)) and not structure.get('review_range_version'):
            candidates = conn.execute("SELECT block_id,kind,text FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND section_id=? AND json_extract(structure_json,'$.index_excluded') IS NULL ORDER BY page,ordinal",
                                      (snap['structure_id'],version['parse_id'], block['section_id'])).fetchall()
            related = statement_context(candidates, block_id) or related
    location = {'block_id': block['block_id'], 'page': block['page'], 'start': offset, 'end': end,
                'block_length': len(text), 'bbox': json.loads(block['bbox_json']), 'row_start': row_start, 'row_end': row_end,
                'printed_page': p['printed_page']}
    if structure.get('location_kind')=='text_span':
        location['location_kind']='text_span'
    continuation = ({'block_id': block['block_id'], 'offset': end} if end < len(text)
                    else {'block_id': next_block['block_id'], 'offset': 0} if next_block else None)
    result = {'kb_id': kb_id, 'doc_id': doc_id, 'source': doc['filename'], **snap,
            'content': text[offset:end], 'locations': [location],
            'segments': [{**location, 'text': text[offset:end]}], 'read_kind': 'read',
            'warnings': json.loads(p['warnings_json']), 'parser_truncated': p['truncated'],
            'structure': structure, 'related_blocks': related,
            'next_read': {'kb_id': kb_id, 'doc_id': doc_id, **{k:v for k,v in snap.items() if v}, **continuation} if continuation else None}
    if include_related:
        if offset or end != len(text) or row_start is not None:
            raise ValueError('关联展开不能与字符或行范围混用')
        others = [read(kb_id, doc_id, **snap, block_id=bid) for bid in result['related_blocks'] if bid != block_id]
        parts = sorted([result, *others], key=lambda r: (r['locations'][0]['page'] or 0,
            r['structure']['source_start'] if r['locations'][0]['page'] is None else
            r['structure'].get('logical_order', int(r['locations'][0]['block_id'].split('B')[-1]))))
        result = {**result, 'content': '\n'.join(p['content'] for p in parts),
                  'segments': [s for p in parts for s in p['segments']], 'locations': [s for p in parts for s in p['locations']],
                  'warnings': list(dict.fromkeys(w for p in parts for w in p['warnings'])), 'next_read': parts[-1]['next_read']}
    return TS.annotate(result)


def hydrate(kb_id, hits, snapshots=None):
    output = []
    with KS._transaction() as conn:
        KS._owned_kb(conn, kb_id, CHAT_USER_ID, active=True)
        for hit in hits:
            doc = conn.execute("SELECT * FROM kb_docs WHERE kb_id=? AND doc_id=? AND deleted_at='' AND status='indexed' AND sync_pending=0",
                               (kb_id, hit['doc_id'])).fetchone()
            if not doc:
                continue
            snap = (snapshots or {}).get(hit['doc_id']) or {'parse_id':doc['published_parse_id'],
                'structure_id':doc['published_structure_id'],'index_run_id':doc['published_run_id']}
            if hit.get('index_run_id','') != snap['index_run_id']:
                continue
            if not doc['published_parse_id']:
                output.append({**hit, 'legacy': True, 'read_kind': 'preview', 'locations': [],
                               'notice': '旧索引没有可靠页块来源'})
                continue
            rows = conn.execute('''SELECT s.*,b.text,b.page,b.bbox_json,b.kind,b.section_id,b.parent_id,b.structure_json,
                p.printed_page,p.warnings_json,c.kind AS chunk_kind,c.node_id,c.text AS index_text FROM kb_chunk_sources s
                JOIN kb_index_runs r ON r.run_id=s.run_id
                JOIN kb_resolved_blocks b ON b.structure_id=r.structure_id AND b.parse_id=s.parse_id AND b.block_id=s.block_id
                LEFT JOIN kb_pages p ON p.parse_id=b.parse_id AND p.page=b.page
                JOIN kb_chunks c ON c.run_id=s.run_id AND c.chunk_idx=s.chunk_idx
                WHERE s.run_id=? AND s.chunk_idx=? ORDER BY b.page,b.ordinal,s.start''',
                                (hit['index_run_id'], hit['chunk_idx'])).fetchall()
            if not rows:
                continue
            segments = [{'block_id': r['block_id'], 'page': r['page'], 'start': r['start'], 'end': r['end'],
                         'block_length': len(r['text']), 'bbox': json.loads(r['bbox_json']),
                         'printed_page': r['printed_page'],
                         **({'location_kind':'text_span'} if r['page'] is None else {}),
                         'row_start': r['row_start'], 'row_end': r['row_end'], 'text': r['text'][r['start']:r['end']]} for r in rows]
            primary = next((r for r in rows if r['kind'] == r['chunk_kind'] and r['block_id'] not in (r['section_id'], r['parent_id'])), rows[0])
            output.append({**hit, 'kb_id': kb_id, **snap, 'source': doc['filename'],
                           'node_id': rows[0]['node_id'], 'navigation': rows[0]['chunk_kind']=='navigation',
                           'content': rows[0]['index_text'] if rows[0]['chunk_kind']=='navigation' else '\n'.join(s['text'] for s in segments), 'segments': segments,
                           'warnings': list(dict.fromkeys(w for r in rows for w in json.loads(r['warnings_json'] or '[]'))),
                           'related_blocks': json.loads(primary['structure_json']).get('related_blocks', []),
                           'context_source_ids': list(dict.fromkeys(i for r in rows for i in json.loads(r['structure_json']).get('context_blocks', []))),
                           'locations': [{k: v for k, v in s.items() if k != 'text'} for s in segments],
                           'read_kind': 'preview', 'read_args': {'kb_id': kb_id, 'doc_id': hit['doc_id'],
                               **{k:v for k,v in snap.items() if v}, 'block_id': primary['block_id'], 'offset': primary['start']}})
    return [TS.annotate(row) for row in output]


def search(kb_id, query, *, doc_id=None, page=None, section_id=None, node_id=None, snapshots=None):
    from storage import kb_structure_store as HS
    from rag import hierarchical_retrieval as HR
    snapshots = snapshots if snapshots is not None else {}
    trace = {}
    with KS._transaction() as conn:
        KS._owned_kb(conn,kb_id,CHAT_USER_ID,active=True)
        doc_ids = [r[0] for r in conn.execute("SELECT doc_id FROM kb_docs WHERE kb_id=? AND deleted_at='' AND status='indexed' AND sync_pending=0 AND published_parse_id<>''",(kb_id,))]
    for did in doc_ids:
        if doc_id and did != doc_id:
            continue
        snapshots[did] = HS.snapshot(kb_id,did,**{k:v for k,v in snapshots.get(did,{}).items() if v})
    if node_id and not doc_id:
        raise ValueError('node_id 必须同时指定文档')
    exact_ids = {value for value, *_ in identifiers(query)}
    numbers = formula_numbers(query)
    if section_id and not doc_id:
        raise ValueError('section_id 必须同时指定 doc_id')
    if section_id:
        section = HS.reviewed_section(snapshots[doc_id]['structure_id'], section_id)
        if section:
            if node_id and node_id != section['node_id']:
                raise ValueError('章节与节点定位不能冲突')
            node_id, section_id = section['node_id'], None
    with KS._transaction() as conn:
        KS._owned_kb(conn, kb_id, CHAT_USER_ID, active=True)
        if doc_id:
            DS.owned(conn, kb_id, doc_id)
        legacy = {r[0] for r in conn.execute("SELECT doc_id FROM kb_docs WHERE kb_id=? AND deleted_at='' AND status='indexed' AND sync_pending=0 AND published_parse_id=''", (kb_id,))}
        if doc_id:
            legacy &= {doc_id}
        scope = "d.kb_id=? AND d.deleted_at='' AND d.status='indexed' AND d.sync_pending=0"
        overrides = [(did,s['index_run_id']) for did,s in snapshots.items() if s.get('index_run_id')]
        run_expr = ('CASE d.doc_id ' + ' '.join('WHEN ? THEN ?' for _ in overrides) + ' ELSE d.published_run_id END') if overrides else 'd.published_run_id'
        params = [v for pair in overrides for v in pair] + [kb_id]
        if doc_id:
            scope += ' AND d.doc_id=?'
            params.append(doc_id)
        if page is not None:
            scope += ' AND b.page=?'
            params.append(page)
        if section_id:
            scope += ' AND (b.section_id=? OR b.block_id=? OR EXISTS(SELECT 1 FROM json_each(b.structure_json,\'$.section_path\') WHERE value=?))'
            params.extend([section_id] * 3)
        if node_id:
            scope += ''' AND EXISTS(SELECT 1 FROM kb_node_sources ns WHERE ns.structure_id=? AND ns.node_id=?
                        AND ns.block_id=b.block_id AND ns.start<s.end AND ns.end>s.start)'''
            params.extend([snapshots[doc_id]['structure_id'],node_id])
        base = f'''SELECT DISTINCT d.doc_id,d.filename AS source,c.run_id AS index_run_id,
                  c.chunk_idx,c.kind AS chunk_kind,c.text AS content FROM kb_docs d
                  JOIN kb_chunks c ON c.run_id={run_expr}
                  JOIN kb_chunk_sources s ON s.run_id=c.run_id AND s.chunk_idx=c.chunk_idx
                  JOIN kb_index_runs r ON r.run_id=s.run_id
                  JOIN kb_resolved_blocks b ON b.structure_id=r.structure_id AND b.parse_id=s.parse_id AND b.block_id=s.block_id '''
        exact = []
        metadata = []
        formulas = []
        for number in numbers:
            sql = base + f''' WHERE {scope} AND c.kind='display_formula' AND
                (json_extract(b.structure_json,'$.formula_label')=? OR
                 (b.kind='formula_number' AND trim(b.text, ' ()（）' || char(9) || char(10) || char(13))=?))
                ORDER BY d.doc_id,b.page,b.ordinal LIMIT ?'''
            formulas.extend({**dict(r), 'formula_number': number} for r in conn.execute(sql, (*params, number, number, C.FUSION_K)))
        if exact_ids:
            placeholders = ','.join('?' for _ in exact_ids)
            # 先取含编号的块或显式章节子块；实际证据再按记录/表格行范围约束。
            sql = base + f'''JOIN kb_identifiers i ON i.parse_id=b.parse_id AND
                (i.block_id=b.block_id OR i.block_id=b.section_id OR i.block_id=b.parent_id)
                WHERE {scope} AND i.value IN ({placeholders})
                ORDER BY CASE WHEN s.start<=i.start AND s.end>i.start THEN 0 ELSE 1 END LIMIT ?'''
            exact = [dict(r) for r in conn.execute(sql, (*params, *sorted(exact_ids), C.FUSION_K))]
            if not exact and not legacy:
                record_stage(trace, 'exact', [], snapshots)
                return [], {'exact_identifiers': sorted(exact_ids), 'outcome': 'identifier_not_matched',
                            'retrieval_trace': trace,
                            'notice': '当前已发布结构中未精确命中；不以相似编号取证，不证明原 PDF 中不存在。'}
        tokens = list(dict.fromkeys(t.casefold() for t in jieba.cut_for_search(query) if len(t.strip()) >= 2))[:12]
        literal = []
        if tokens and not exact_ids:
            conditions = ' OR '.join('(instr(lower(b.text),?)>0 OR instr(lower(d.filename),?)>0)' for _ in tokens)
            values = [x for token in tokens for x in (token.lower(), token.lower())]
            score = ' + '.join('(instr(lower(c.text),?)>0)' for _ in tokens)
            literal_sql = f'''WITH candidates AS ({base} WHERE {scope} AND ({conditions})),
                scored AS (SELECT *, {score.replace('c.text', 'content')} AS literal_score FROM candidates),
                ranked AS (SELECT *,row_number() OVER (PARTITION BY doc_id ORDER BY literal_score DESC,chunk_idx) AS doc_rank FROM scored)
                SELECT * FROM ranked ORDER BY doc_rank,literal_score DESC,doc_id LIMIT ?'''
            literal = [dict(r) for r in conn.execute(literal_sql, (*params, *values, *tokens, C.LITERAL_K))]
        if not exact_ids:
            meta_sql = f'''WITH candidates AS ({base} WHERE {scope} AND c.kind IN
                ('metadata','toc','footnote','reference','reference_content','section_title','paragraph_title')),
                scored AS (SELECT *,row_number() OVER (PARTITION BY doc_id ORDER BY (chunk_kind='metadata') DESC,
                  {' + '.join('(instr(lower(content),?)>0)' for _ in tokens) or '0'} DESC,chunk_idx) AS doc_rank FROM candidates)
                SELECT * FROM scored ORDER BY doc_rank,doc_id LIMIT ?'''
            metadata = [dict(r) for r in conn.execute(meta_sql, (*params, *tokens, C.METADATA_K))]
    vector_hits = []
    if not numbers and (not exact_ids or (not exact and legacy)):
        vector_hits = V.search_memories(query_vector=embed_query(query, INSTRUCT_KB), query_text=query,
            top_k=C.RECALL_K, user_id=CHAT_USER_ID, memory_type=MEMORY_TYPE_KB_CHUNK, kb_id=kb_id, doc_id=doc_id, page=page, section_id=section_id,
            dense_k=C.DENSE_K, sparse_k=C.SPARSE_K, snapshots=snapshots, node_id=node_id)
        if exact_ids:
            vector_hits = [h for h in vector_hits if h.get('doc_id') in legacy
                           and exact_ids.intersection(value for value, *_ in identifiers(h.get('content', '')))]
    fused, scores = {}, defaultdict(float)
    for name, route in (('exact', exact), ('formula', formulas), ('literal', literal),
                        ('hybrid_rrf', vector_hits), ('metadata', metadata)):
        record_stage(trace, name, route, snapshots)
    for route in ((formulas,) if numbers else (exact, literal, vector_hits, metadata)):
        for rank, hit in enumerate(route):
            key = (hit['doc_id'], hit.get('index_run_id', ''), hit.get('chunk_idx', hit.get('chunk_id')))
            scores[key] += 1 / (60 + rank + 1)
            fused[key] = {**hit, 'chunk_idx': key[2]}
    ranked = [fused[k] for k in sorted(fused, key=lambda k: scores[k], reverse=True)]
    record_stage(trace, 'fusion', ranked, snapshots)
    limited = diversify(ranked, C.FUSION_K)
    record_stage(trace, 'fusion_selected', limited, snapshots)
    admitted = hydrate(kb_id, limited, snapshots)
    record_stage(trace, 'hydrated', admitted, snapshots)
    if C.RETRIEVAL_MODE=='flat':
        admitted = [h for h in admitted if not h.get('navigation')]
    if page is not None:
        admitted = [h for h in admitted if any(p['page'] == page for p in h['locations'])]
    # 先轮转文档，再精排，避免同一文档的重复内容挤掉其他文档的全部候选。
    diversified = diversify(admitted, C.RERANK_K)
    record_stage(trace, 'rerank_input', diversified, snapshots)
    diagnostics = {}
    results = diversified if numbers else rerank_chunks(query, diversified, diagnostics=diagnostics, top_n=C.RERANK_K)
    ranked_candidates = results
    record_stage(trace, 'rerank_output', results, snapshots)
    before_threshold = len(results)
    if not exact_ids and not numbers:
        results = [h for h in results if h.get('rerank_score') is None or h['rerank_score'] >= C.RERANK_MIN]
    threshold_filtered = before_threshold - len(results)
    record_stage(trace, 'threshold_passed', results, snapshots)
    vector_nodes = {(h['doc_id'],h['node_id']):h.get('rerank_score') for h in results if h.get('navigation')}
    results = [h for h in results if not h.get('navigation')]
    results = hydrate(kb_id, results, snapshots)
    if exact_ids:
        results = restrict_identifiers(results, exact_ids)
    before_dedup = len(results)
    results = deduplicate_ranges(results)
    record_stage(trace, 'flat_evidence', results, snapshots)
    duplicate_filtered = before_dedup - len(results)
    navigation = []
    if C.RETRIEVAL_MODE != 'flat' and not exact_ids and not numbers:
        structured, navigation = HR.route(kb_id,query,snapshots,doc_id=doc_id,node_id=node_id,page=page,
                                         section_id=section_id,vector_nodes=vector_nodes,expand=C.RETRIEVAL_MODE=='hybrid_structure')
        record_stage(trace, 'structure_expanded', structured, snapshots)
        if C.RETRIEVAL_MODE == 'hybrid_structure':
            core = [r for r in structured if r.get('bundle_role')=='core'][:C.EVIDENCE_K]
            details = [r for r in structured if r.get('bundle_role')!='core']
            results = deduplicate_ranges([*core, *results[:2], *details, *results[2:]])
        elif navigation:
            routed = {}
            for selection in navigation:
                nodes,_ = HS.load(snapshots[selection['doc_id']]['structure_id'])
                node = next(n for n in nodes if n['node_id']==selection['node_id'])
                routed.setdefault(selection['doc_id'],set()).update(s['block_id'] for s in node['sources'])
            results.sort(key=lambda r:not any(s['block_id'] in routed.get(r['doc_id'],set()) for s in r.get('locations',[])))
    record_stage(trace, 'structure_merged', results, snapshots)
    if page is not None or section_id or node_id:
        allowed = None
        if node_id:
            nodes,_ = HS.load(snapshots[doc_id]['structure_id'])
            node = next((n for n in nodes if n['node_id']==node_id),None)
            if node is None:
                raise KS.KBNotFound('指定节点不存在于当前文档结构版本')
            allowed = node['sources']
        elif section_id:
            allowed = [{'block_id':b['block_id'],'start':0,'end':len(b['text'])} for b in DS.blocks(snapshots[doc_id]['parse_id'], structure_id=snapshots[doc_id]['structure_id'])
                       if b['block_id']==section_id or b['section_id']==section_id or section_id in b['structure'].get('section_path',[])]
        scoped = []
        for hit in results:
            parts = []
            for part in hit.get('segments',[]):
                if page is not None and part['page']!=page:
                    continue
                spans = [part] if allowed is None else [s for s in allowed if s['block_id']==part['block_id']]
                for span in spans:
                    a,b = max(part['start'],span['start']),min(part['end'],span['end'])
                    if a<b:
                        parts.append({**part,'start':a,'end':b,'text':part['text'][a-part['start']:b-part['start']]})
            changed = parts!=hit.get('segments',[])
            if not parts or changed and hit.get('atomic'):
                continue
            if changed:
                hit = {**hit,'content':'\n'.join(p['text'] for p in parts),'segments':parts,
                       'locations':[{k:v for k,v in p.items() if k!='text'} for p in parts],
                       'required_ranges':[], 'scope_limited':True}
            scoped.append(hit)
        results = scoped
    record_stage(trace, 'scope_filtered', results, snapshots)
    results = results[:max(C.EVIDENCE_K,C.ROUTE_NODE_TOP_K*2) if navigation and C.RETRIEVAL_MODE=='hybrid_structure' else C.EVIDENCE_K]
    record_stage(trace, 'returned', results, snapshots)
    meta = {'outcome': 'matched' if results else 'no_match', 'exact_identifiers': sorted(exact_ids),
            'formula_numbers': numbers, 'missing_formula_numbers': [n for n in numbers if not any(h.get('formula_number') == n for h in results)],
            'navigation':navigation,'retrieval_mode':C.RETRIEVAL_MODE,
            'retrieval_trace': trace,
            'candidate_counts': {'exact': len(exact), 'formula': len(formulas), 'literal': len(literal), 'metadata': len(metadata), 'hybrid': len(vector_hits), 'admitted': len(admitted)},
            'filtered_counts': {'threshold': threshold_filtered, 'contained_evidence': duplicate_filtered},
            'candidate_ranks': [{'doc_id': h['doc_id'], 'chunk_idx': h.get('chunk_idx'), 'rank': i + 1,
                                 'rerank_score': h.get('rerank_score')} for i, h in enumerate(ranked_candidates)],
            'score_source': 'formula_location' if numbers else 'rerank' if diagnostics.get('rerank_status') == 'success' else 'rrf_rank_only', **diagnostics}
    get_logger('kb').info('kb_structured_search', data={'kb_id': kb_id, **meta, 'returned': len(results)})
    return results, meta


def restrict_identifiers(hits, targets):
    """编号约束还要约束实际证据范围，不能返回同一大块里的其他对象参数。"""
    for hit in hits:
        if hit.get('legacy'):
            continue
        selected = []
        has_target = False
        with KS._transaction() as conn:
            for segment in hit.get('segments', []):
                block = conn.execute('SELECT text,section_id,parent_id,structure_json FROM kb_resolved_blocks WHERE structure_id=? AND parse_id=? AND block_id=?',
                                     (hit.get('structure_id',''), hit['parse_id'], segment['block_id'])).fetchone()
                entries = identifiers(block['text'])
                heading_ids = {r[0] for r in conn.execute('SELECT value FROM kb_identifiers WHERE parse_id=? AND block_id=?',
                                                         (hit['parse_id'], block['section_id']))} if block['section_id'] != segment['block_id'] else set()
                record_ids = {r[0] for r in conn.execute('SELECT value FROM kb_identifiers WHERE parse_id=? AND block_id=?',
                                                        (hit['parse_id'], block['parent_id']))} if block['parent_id'] != segment['block_id'] else set()
                if segment['block_id'] in hit.get('context_source_ids', []):
                    selected.append(segment)
                    continue
                if targets & (heading_ids | record_ids) or (segment.get('row_start') == 0 and not any(
                        value in targets and segment['start'] <= start < segment['end'] for value, _, start, _ in entries)):
                    selected.append(segment)
                    has_target |= bool(targets & (heading_ids | record_ids))
                    continue
                structure = json.loads(block['structure_json'])
                row_ranges = structure.get('row_ranges', [])
                if segment.get('row_start') is not None and any(
                        targets.intersection(value for value, *_ in identifiers(c['text']))
                        and c['row'] < segment['row_start'] < c['row'] + c['rowspan']
                        for c in structure.get('cells', [])):
                    selected.append(segment)
                    has_target = True
                    continue
                kept_ranges = set()
                for i, (value, _, start, _) in enumerate(entries):
                    if value not in targets:
                        continue
                    if row_ranges:
                        start, stop = next((a, b) for a, b in row_ranges if a <= start < b)
                    else:
                        line_start = block['text'].rfind('\n', 0, start) + 1
                        prefix = block['text'][line_start:start].strip()
                        stop = len(block['text'])
                        for _, _, following, _ in entries[i + 1:]:
                            next_line = block['text'].rfind('\n', 0, following) + 1
                            if next_line > line_start and block['text'][next_line:following].strip() == prefix:
                                stop = next_line
                                break
                        start = line_start
                    begin, end = max(start, segment['start']), min(stop, segment['end'])
                    if begin < end and (begin, end) not in kept_ranges:
                        selected.append({**segment, 'start': begin, 'end': end, 'text': block['text'][begin:end]})
                        kept_ranges.add((begin, end))
                        has_target = True
        if not has_target:
            selected = []
        hit['segments'] = selected
        hit['content'] = '\n'.join(s['text'] for s in selected)
        hit['locations'] = [{k: v for k, v in s.items() if k != 'text'} for s in selected]
        if selected:
            primary = next((s for s in selected if s['block_id'] == hit.get('read_args', {}).get('block_id')), selected[0])
            hit['read_args'] = {'kb_id': hit['kb_id'], 'doc_id': hit['doc_id'],
                                **{k:hit[k] for k in ('parse_id','structure_id','index_run_id') if hit.get(k)},
                                'block_id': primary['block_id'], 'offset': primary['start'], 'end': primary['end']}
    return [hit for hit in hits if hit.get('content')]
