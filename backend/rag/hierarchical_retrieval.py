"""结构导航与有来源的证据包；显式顺序读取不经过相关性排序。"""

import re

import jieba

from rag import document_config as C
from rag.document_hierarchy import statement_key
from storage import kb_store as KS, kb_structure_store as HS


def intentions(query):
    """识别查询中的证明解释和全文概览意图，供结构候选排序使用。"""
    return {'proof': bool(re.search(r'证明|推导|推理|论证|解释|如何得到|为什么|\bproof|\bprove|\bderiv|\bexplain|\bwhy|\bargument', query, re.I)),
            'overview': bool(re.search(r'概述|概括|总结|主要内容|核心结果|讲了什么|贡献|\boverview|\bsummari|main results?|contributions?',query,re.I))}


def path_for(node, known):
    """沿父节点回溯并返回从文档根到当前节点的标题路径。"""
    result = []
    while node:
        result.append(node['title'])
        node = known.get(node['parent_id'])
    return list(reversed(result))


def bundles(kb_id, doc_id, snapshot, node_id, *, query='', sequential=False, cursor=0, block_offset=0):
    """为结构节点组装可追溯证据包，顺序读取时严格按原文游标推进。"""
    from rag.document_retrieval import read
    nodes, relations = HS.load(snapshot['structure_id'])
    known = {n['node_id']: n for n in nodes}
    node = known.get(node_id)
    if node is None:
        raise KS.KBNotFound('节点不存在于固定结构版本')
    intent = intentions(query)
    targets = [node]
    context_refs = []
    if not sequential and node['kind'] in ('document','section','statement'):
        descendants = [n for n in nodes if HS.descendant(n, node_id, known)]
        targets = [n for n in descendants if n['kind'] == 'formula' and known[n['parent_id']]['kind']=='statement']
        if intent['proof']:
            statements = {node_id, *[n['node_id'] for n in descendants if n['kind'] == 'statement']}
            proofs = {r['source_id'] for r in relations if r['kind'] in ('explains','proof_of') and r['target_id'] in statements | {n['node_id'] for n in targets}}
            targets = [n for n in nodes if n['node_id'] in proofs] + targets
        if not targets:
            targets = [n for n in descendants if n['kind'] in ('statement','paragraph','table','code','list','quote','html','metadata')] or [node]
        statement_ids = {node_id, *[n['node_id'] for n in descendants if n['kind']=='statement']}
        context_refs = [r for r in relations if r['target_id'] in statement_ids and r['basis']=='literal_statement_reference'
                        and known[r['source_id']]['kind']!='proof']
    output = []
    cache = {}
    def segment(span):
        """缓存原文块读取结果，再按给定范围提取带定位的片段。"""
        bid = span['block_id']
        if bid not in cache:
            cache[bid] = read(kb_id,doc_id,**snapshot,block_id=bid)
        original = cache[bid]
        return {**original['segments'][0], **span, 'text': original['content'][span['start']:span['end']]}
    for target in targets:
        sources = target['sources']
        read_units = []
        guide = next((n for n in nodes if n['parent_id']==target['node_id'] and n['basis']=='proof_route'),None)
        owner = known.get(target['parent_id']) if target['basis'] in ('proof_route','proof_passage') else target
        route_view = target['basis']=='proof_route' or bool(guide and not sequential)
        if sequential:
            if cursor < 0 or cursor > len(sources) or block_offset < 0:
                raise ValueError('节点续读位置超出范围')
            if cursor == len(sources):
                if block_offset:
                    raise ValueError('节点已读完，不能指定字符偏移')
                continue
            if block_offset > sources[cursor]['end'] - sources[cursor]['start']:
                raise ValueError('节点内字符偏移超出范围')
            groups = [[{**span,'start':span['start'] + (block_offset if i == cursor else 0)}] for i,span in enumerate(sources) if i >= cursor]
        elif target['kind'] == 'formula':
            # 公式与适用条件打包为同一证据单元，避免截取公式后丢失成立前提。
            conditions = [s for r in relations if r['target_id'] == target['node_id'] and r['kind'] == 'condition_of' for s in r['evidence']]
            groups = [[*conditions, *sources]]
        elif target['kind'] == 'proof' and guide:
            groups = [guide['sources']]
            read_units = [{'sources':guide['sources'],'target':{'doc_id':doc_id,'structure_id':snapshot['structure_id'],
                           'mode':'node','node_id':guide['node_id']}}]
        elif target['kind'] == 'proof':
            terms = [t.casefold() for t in jieba.cut_for_search(query) if len(t.strip()) > 1]
            passages = [n for n in nodes if n['parent_id']==target['node_id'] and n['basis']=='proof_passage']
            passages = passages or [{'sources':[s]} for s in sources]
            middle = list(range(1,max(1,len(passages)-1)))
            chosen = []
            for half in (middle[:(len(middle)+1)//2],middle[(len(middle)+1)//2:]):
                if half:
                    chosen.append(max(half,key=lambda i: (
                        sum(t in ' '.join(segment(s)['text'] for s in passages[i]['sources']).casefold() for t in terms)
                        + 2*bool(re.search(r'得到|代入|由此|利用|引理|恒等式|therefore|substitut|identity|lemma|hence|yields',
                                          ' '.join(segment(s)['text'] for s in passages[i]['sources']),re.I)),
                        -abs(i-sum(half)/len(half)))))
            anchor = sorted(set([0,len(passages)-1,*chosen]))
            groups = [[s for i in anchor for s in passages[i]['sources']],
                      *[p['sources'] for i,p in enumerate(passages) if i not in anchor]]
            read_units = [{'sources':passages[i]['sources'], 'target':{'doc_id':doc_id,'structure_id':snapshot['structure_id'],
                           'mode':'node','node_id':passages[i]['node_id']}} for i in anchor if passages[i].get('node_id')]
        else:
            groups = [[s] for s in sources]
        for gi, group in enumerate(groups):
            unique = {(s['block_id'],s['start'],s['end']):s for s in group if s['end']>s['start']}
            segments = [segment(s) for s in unique.values()]
            if not segments:
                continue
            row = {'kb_id':kb_id,'doc_id':doc_id,**snapshot,'source':path_for(target,known)[0],
                'content':'\n'.join(s['text'] for s in segments),'segments':segments,
                'locations':[{k:v for k,v in s.items() if k!='text'} for s in segments],
                'node_id':target['node_id'],'node_kind':target['kind'],'node_path':path_for(target,known),
                'bundle_id':owner['node_id'], 'question':query, 'atomic':target['kind']=='formula',
                'bundle_role':'core' if gi==0 and not (intent['proof'] and target['kind']=='formula') else 'detail',
                'read_kind':'read' if sequential else 'preview',
                'warnings':list(dict.fromkeys(w for s in segments for w in cache[s['block_id']]['warnings'])),
                'read_args':{'kb_id':kb_id,'targets':[{'doc_id':doc_id,'structure_id':snapshot['structure_id'],'mode':'node','node_id':target['node_id']}]}}
            if route_view:
                row.update(evidence_role='proof_route',scope_limited=True,sampled=True,unit_source_count=len(owner['sources']))
                row['full_read'] = {'kb_id':kb_id,'targets':[{'doc_id':doc_id,'structure_id':snapshot['structure_id'],
                                    'mode':'node','node_id':owner['node_id']}]}
                row['warnings'].append('extractive_proof_route_full_formulas_omitted')
            required = group if not sequential and row['bundle_role']=='core' and target['kind'] in ('formula','proof','statement') else []
            row['required_ranges'] = [{k:s[k] for k in ('block_id','start','end')} for s in required]
            if gi==0 and read_units:
                row['read_units'] = read_units
            if sequential:
                index = cursor + gi
                row['node_cursor'] = index
                row['node_source_start'] = sources[index]['start']
                if index+1 < len(sources):
                    row['next_read'] = {'kb_id':kb_id,'targets':[{'doc_id':doc_id,'structure_id':snapshot['structure_id'],
                        'mode':'node','node_id':node_id,'cursor':index+1}]}
            else:
                row['sampled'] = route_view or len(unique)<len(sources)
                row['unit_source_count'] = len(sources)
                if target['kind']=='formula':
                    row['read_args'] = {'kb_id':kb_id,'targets':[{'doc_id':doc_id,'parse_id':snapshot['parse_id'],
                        'mode':'formula','formula_number':target['attributes']['number'],'page':target['page']}]}
                elif gi==0 and read_units:
                    row['read_args'] = {'kb_id':kb_id,'targets':[read_units[0]['target']]}
                else:
                    row['read_args'] = {'kb_id':kb_id,'targets':[{'doc_id':doc_id,'parse_id':snapshot['parse_id'],
                        'mode':'range','block_id':s['block_id'],'offset':s['start'],'end':s['end']} for s in group[:C.READ_MAX_TARGETS]]}
            output.append(row)
    contextual = []
    for relation in context_refs:
        source = known[relation['source_id']]
        spans = list({(s['block_id'],s['start'],s['end']):s for s in relation['evidence']}.values())
        segments = [segment(s) for s in spans]
        contextual.append({'kb_id':kb_id,'doc_id':doc_id,**snapshot,'source':path_for(node,known)[0],
            'content':'\n'.join(s['text'] for s in segments),'segments':segments,
            'locations':[{k:v for k,v in s.items() if k!='text'} for s in segments],
            'node_id':source['node_id'],'node_kind':source['kind'],'node_path':path_for(source,known),
            'bundle_id':source['node_id']+':ref:'+relation['target_id'], 'bundle_role':'core','evidence_role':'cross_reference',
            'question':query,'atomic':True,'read_kind':'preview','required_ranges':spans,
            'warnings':list(dict.fromkeys(w for s in segments for w in cache[s['block_id']]['warnings'])),
            'read_args':{'kb_id':kb_id,'targets':[{'doc_id':doc_id,'parse_id':snapshot['parse_id'],'mode':'range',
                'block_id':s['block_id'],'offset':s['start'],'end':s['end']} for s in spans[:C.READ_MAX_TARGETS]]}})
    from storage.kb_text_store import annotate
    return [annotate(row) for row in contextual + output]


def route(kb_id, query, snapshots, *, doc_id=None, node_id=None, page=None, section_id=None, vector_nodes=(), expand=True):
    """根据查询、显式范围和向量命中选择结构节点，并交错输出各节点证据。"""
    intent = intentions(query)
    terms = list(dict.fromkeys(t.casefold() for t in jieba.cut_for_search(query) if len(t.strip())>1))
    label = statement_key(query)
    ranked, structured = [], {}
    for did, snap in snapshots.items():
        if not snap.get('structure_id') or (doc_id and did != doc_id):
            continue
        nodes, _ = HS.load(snap['structure_id'])
        known = {n['node_id']:n for n in nodes}
        scope = known.get(node_id) if node_id else None
        if node_id and not scope:
            raise KS.KBNotFound('指定节点不存在于当前结构版本')
        explicit = [n for n in nodes if label and n['kind']=='statement' and statement_key(n['title'])==label]
        for n in nodes:
            if n['basis']=='proof_route':
                continue
            if n['kind'] in ('document','header','footer') or (scope and n['node_id'] != scope['node_id'] and not HS.descendant(n, scope['node_id'], known)):
                continue
            if page is not None and (n['page'] is None or not n['page']<=page<=n['end_page']):
                continue
            if section_id and not any(p['block_id']==section_id for p in n['sources']) and not any(
                    a['attributes'].get('heading_block')==section_id and a['start_order']<=n['start_order']<n['end_order']<=a['end_order'] for a in nodes):
                continue
            if explicit and n not in explicit:
                continue
            if intent['overview'] and n['kind']=='formula' and known[n['parent_id']]['kind']!='statement':
                continue
            text = ' '.join(path_for(n,known)[1:]) + ' ' + n['preview']
            score = sum(1 for t in terms if t in text.casefold())
            if (did,n['node_id']) in vector_nodes:
                relevance = vector_nodes.get((did,n['node_id'])) if isinstance(vector_nodes,dict) else None
                score += 6*(relevance if relevance is not None else 1)
            if n in explicit:
                score += 50
            if intent['overview'] and n['kind'] in ('statement','formula'):
                score += 5
            if intent['overview'] and re.search(r'摘要|结论|总结|abstract|conclusion|summary',' '.join(path_for(n,known)[1:]),re.I):
                score += 8
            if intent['overview'] and re.search(r'新的|本文|主要结果|创新|\bnew\b|\bmain results?\b|\bour\b|\bproposed\b',text,re.I):
                score += 6
            if intent['overview'] and re.search(r'已有|背景|相关工作|\bbackground\b|\brelated work\b',' '.join(path_for(n,known)[1:]),re.I):
                score -= 5
            if score:
                ranked.append((score,did,n))
        structured[did] = snap
    ranked.sort(key=lambda v:(-v[0],v[1],v[2]['ordinal']))
    best_docs = list(dict.fromkeys(did for _,did,_ in ranked))[:C.ROUTE_DOC_TOP_K]
    chosen, groups, covered = [], [], set()
    for score,did,node in ranked:
        if did not in best_docs or (did,node['node_id']) in covered:
            continue
        if not expand:
            chosen.append({'doc_id':did,'node_id':node['node_id'],'kind':node['kind'],'score':score})
            if len(chosen)>=C.ROUTE_NODE_TOP_K:
                break
            continue
        rows = bundles(kb_id,did,structured[did],node['node_id'],query=query)
        rows = [r for r in rows if (did,r['node_id']) not in covered]
        if not rows:
            continue
        groups.append(rows)
        covered.update((did,r['node_id']) for r in rows)
        chosen.append({'doc_id':did,'node_id':node['node_id'],'kind':node['kind'],'score':score})
        if len(chosen)>=C.ROUTE_NODE_TOP_K:
            break
    ordered = []
    # 各命中节点轮流贡献证据，防止一个长节点先耗尽下游的上下文预算。
    while any(groups):
        ordered.extend(g.pop(0) for g in groups if g)
    return ordered, chosen
