"""结构版本独立于 OCR 原文；只有绑定已发布索引的版本可以被读取。"""

import hashlib
import json
from uuid import uuid4

from storage import kb_store as KS, kb_document_store as DS


def prepare(kb_id, doc_id, parse_id, lease, config):
    """按解析版本和配置指纹复用或创建独立结构版本。"""
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    with KS._transaction() as conn:
        DS.current(conn, kb_id, doc_id, parse_id, lease)
        found = conn.execute('SELECT * FROM kb_structure_versions WHERE parse_id=? AND fingerprint=?', (parse_id, fingerprint)).fetchone()
        if found:
            return dict(found)
        structure_id = 'sv_' + uuid4().hex
        conn.execute('''INSERT INTO kb_structure_versions(structure_id,parse_id,fingerprint,config_json,status,created_at)
                        VALUES(?,?,?,?,'building',?)''', (structure_id, parse_id, fingerprint, json.dumps(config), DS.now()))
        return {'structure_id': structure_id, 'status': 'building'}


def save(kb_id, doc_id, parse_id, lease, structure_id, nodes, relations, quality, blocks=None):
    """校验结构树和来源范围，在有效解析租约下保存节点、关系和可选块投影。"""
    from rag.document_hierarchy import validate
    original = DS.blocks(parse_id)
    if blocks is not None:
        if ({b['block_id']: (b['text'], b['page'], b['bbox']) for b in blocks}
                != {b['block_id']: (b['text'], b['page'], b['bbox']) for b in original}
                or len(blocks) != len(original)):
            raise ValueError('structure_projection_changed_original')
    validate(nodes, relations, blocks if blocks is not None else original)
    with KS._transaction() as conn:
        DS.current(conn, kb_id, doc_id, parse_id, lease)
        version = conn.execute('SELECT * FROM kb_structure_versions WHERE structure_id=? AND parse_id=?', (structure_id, parse_id)).fetchone()
        if not version:
            raise KS.KBConflict('结构任务不存在')
        if version['status'] in ('ready', 'published'):
            return
        if json.loads(version['config_json']).get('projection_version') and blocks is None:
            raise ValueError('missing_structure_projection')
        if blocks is not None:
            conn.executemany('INSERT INTO kb_structure_blocks VALUES(?,?,?,?,?,?)',
                [(structure_id,b['block_id'],b['ordinal'],b['section_id'],b['parent_id'],
                  json.dumps(b['structure'],ensure_ascii=False)) for b in blocks])
        for node in nodes:
            conn.execute('INSERT INTO kb_document_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (structure_id, *[node[k] for k in ('node_id','parent_id','kind','ordinal','depth','title','preview','basis','start_order','end_order','page','end_page')],
                 json.dumps(node['attributes'], ensure_ascii=False)))
            conn.executemany('INSERT INTO kb_node_sources VALUES(?,?,?,?,?,?)',
                [(structure_id,node['node_id'],i,s['block_id'],s['start'],s['end']) for i,s in enumerate(node['sources'])])
        conn.executemany('INSERT INTO kb_node_relations VALUES(?,?,?,?,?,?)', [(structure_id,r['source_id'],r['target_id'],r['kind'],r['basis'],json.dumps(r['evidence'])) for r in relations])
        conn.execute("UPDATE kb_structure_versions SET status='ready',checkpoint_json=?,quality_json=? WHERE structure_id=?",
            (json.dumps({'complete': True, 'nodes': len(nodes), 'relations': len(relations)}),json.dumps(quality,ensure_ascii=False),structure_id))


def snapshot(kb_id, doc_id, *, parse_id=None, structure_id=None, index_run_id=None):
    """解析并固定可读取的解析、结构和索引版本组合。"""
    with KS._transaction() as conn:
        doc = DS.owned(conn, kb_id, doc_id)
        where, params = "doc_id=? AND r.status='published'", [doc_id]
        if index_run_id:
            where += ' AND r.run_id=?'
            params.append(index_run_id)
        elif not parse_id and not structure_id:
            where += ' AND r.run_id=?'
            params.append(doc['published_run_id'])
        if parse_id:
            where += ' AND r.parse_id=?'
            params.append(parse_id)
        if structure_id:
            where += ' AND r.structure_id=?'
            params.append(structure_id)
        row = conn.execute(f'''SELECT r.run_id AS index_run_id,r.parse_id,r.structure_id FROM kb_index_runs r
                            JOIN kb_parse_versions p ON p.parse_id=r.parse_id WHERE {where}
                            ORDER BY r.created_at DESC LIMIT 1''', params).fetchone()
        if not row:
            if not any((parse_id,structure_id,index_run_id,doc['published_parse_id'])):
                return {'parse_id': '', 'structure_id': '', 'index_run_id': doc['published_run_id']}
            raise KS.KBNotFound('指定的解析/结构/索引组合未发布或不属于此文档')
        return dict(row)


def load(structure_id):
    """加载结构节点及关系，并恢复节点的原文来源范围。"""
    with KS._transaction() as conn:
        rows = [dict(r) for r in conn.execute('SELECT * FROM kb_document_nodes WHERE structure_id=? ORDER BY ordinal', (structure_id,))]
        sources = [dict(r) for r in conn.execute('SELECT * FROM kb_node_sources WHERE structure_id=? ORDER BY node_id,ordinal', (structure_id,))]
        relations = [dict(r) for r in conn.execute('SELECT * FROM kb_node_relations WHERE structure_id=?', (structure_id,))]
    grouped = {}
    for source in sources:
        grouped.setdefault(source['node_id'], []).append({k: source[k] for k in ('block_id','start','end')})
    for row in rows:
        row['attributes'] = json.loads(row.pop('attributes_json'))
        row['sources'] = grouped.get(row['node_id'], [])
    for relation in relations:
        relation['evidence'] = json.loads(relation.pop('evidence_json'))
    return rows, relations


def descendant(node, root_id, known):
    """沿父链判断节点是否属于指定根节点的后代。"""
    while node['parent_id']:
        if node['parent_id'] == root_id:
            return True
        node = known[node['parent_id']]
    return False


def reviewed_section(structure_id, section_id):
    """在已审阅结构中定位章节，区分未启用审阅投影和目标不存在。"""
    nodes, _ = load(structure_id)
    if not nodes or not nodes[0]['attributes'].get('review_range_version'):
        return None
    matches = [n for n in nodes if n['kind'] == 'section' and (n['node_id'] == section_id
               or n['attributes']['heading_anchor'] == section_id)]
    if not matches:
        raise KS.KBNotFound('范围级章节请使用当前目录返回的 section_id/node_id；旧章节引用需同时指定旧结构版本')
    return matches[0]


def outline(kb_id, doc_id, *, parse_id=None, structure_id=None, index_run_id=None, node_id=None, offset=0, limit=20, depth=1):
    """按节点深度及分页范围返回结构目录，附带原文位置而不冒充正文。"""
    snap = snapshot(kb_id, doc_id, parse_id=parse_id, structure_id=structure_id, index_run_id=index_run_id)
    if not snap['structure_id']:
        raise KS.KBConflict('此文档尚无层级结构，请重建索引；旧页块目录仍可读取')
    nodes, _ = load(snap['structure_id'])
    root = next((n for n in nodes if n['node_id'] == node_id), None) if node_id else nodes[0]
    if not root:
        raise KS.KBNotFound('节点不存在于当前结构版本')
    known = {n['node_id']: n for n in nodes}
    selected = [n for n in nodes if descendant(n, root['node_id'], known)
                and root['depth'] < n['depth'] <= root['depth'] + depth]
    if nodes[0]['attributes'].get('review_range_version'):
        selected.sort(key=lambda n: (n['start_order'], n['depth'], n['ordinal']))
    cards = []
    text_starts=None
    if root['page'] is None:
        with KS._transaction() as conn:
            raw=conn.execute('SELECT line_starts_json FROM kb_text_sources WHERE parse_id=?',(snap['parse_id'],)).fetchone()
        text_starts=json.loads(raw[0]) if raw else None
    for n in selected[offset:offset+limit]:
        cards.append({k: n[k] for k in ('node_id','parent_id','kind','depth','title','page','end_page','basis')})
        cards[-1].update(children=sum(c['parent_id'] == n['node_id'] for c in nodes), preview=n['preview'],
                          preview_kind='literal_navigation', read_args={'kb_id':kb_id,'targets':[{'doc_id':doc_id,
                          'structure_id':snap['structure_id'],'mode':'node','node_id':n['node_id']}]})
        if n['kind']=='section' and 'parent_status' in n['attributes']:
            cards[-1].update({k:n['attributes'][k] for k in ('parent_status','boundary_status','issues','declared_level')})
        cards[-1].update({k:n['attributes'][k] for k in ('role','review_status','declared_level','ownership','reason') if k in n['attributes']})
        if text_starts is not None:
            from storage.kb_text_store import location
            cards[-1].update(location(text_starts,{'source_start':0},n['start_order'],n['end_order']))
    return {'doc_id':doc_id, **snap, 'view':'tree','node_id':root['node_id'],'title':root['title'],
            'blocks':cards,'total_blocks':len(selected),'offset':offset,
            'next_offset':offset+len(cards) if offset+len(cards)<len(selected) else None,
            'notice':'结构导航不是正文；节点范围已定位，不表示已阅读全文或语义核验。'}


def navigation_chunks(structure_id, counter, token_limit):
    """把结构路径和预览转换为额度内的导航索引片段。"""
    nodes, _ = load(structure_id)
    known = {n['node_id']: n for n in nodes}
    chunks = []
    for node in nodes:
        if node['kind'] in ('paragraph', 'header', 'footer'):
            continue
        path, parent = [], node
        while parent:
            path.append(parent['title'])
            parent = known.get(parent['parent_id'])
        text = ' > '.join(reversed(path)) + '\n' + node['kind'] + '\n' + node['preview']
        while text and counter(text) > token_limit:
            text = text[:max(0,len(text)*3//4)]
        if text:
            chunks.append({'kind':'navigation','text':text,'token_count':counter(text),
                           'node_id':node['node_id'],'sources':node['sources'][:1]})
    return chunks
