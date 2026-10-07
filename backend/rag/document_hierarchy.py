"""可重建的文档结构；所有正文范围始终指向不可变解析块。"""

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher

from rag.document_layout import HEADINGS, MARGINS, STATEMENT

VERSION = 3
PROOF = re.compile(r'(?:^|(?<=[。；;\n]))\s*(?:(?:现在|下面|以下|接着|再|首先|其次)\s*)?'
                   r'(?:证明(?:\s|[：:。]|命题|定理|引理|第|不等式|公式|[（(])|证(?:\s+|[：:。．.])|'
                   r'Proof\b|We\s+(?:now\s+)?prove\b)', re.I)
CONDITION = re.compile(r'若|如果|假设|设\s|满足|当|条件|对于|属于|\b(?:if|assum|provided|where|let|for any)\b|∈|\\in\b', re.I)
TARGET = re.compile(r'(?:不等式|公式|等式|式|equation|inequality|eq\.?)\s*[（(]?\s*(\d+(?:\.\d+)*[a-z]?)', re.I)
NUMBER = re.compile(r'^\s*(\d+(?:\.\d+)*)(?:[.、．\s]|$)')
PROOF_END = re.compile(r'(?:证毕|证明完毕|(?:由此|由是|故|即|遂)得证)[。．.]?|'
                       r'\b(?:Q\.?E\.?D\.?|this completes the proof|the proof is complete)\b[.]?|[□∎]', re.I)
STATEMENT_REF = re.compile(r'(?:命题|定理|引理|推论|proposition|theorem|lemma|corollary)\s*'
                           r'(?:[IVXLCDMⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+|\d+(?:\.\d+)*|[一二三四五六七八九十百]+)', re.I)


def statement_key(text):
    text = unicodedata.normalize('NFKC',text)
    match = re.search(r'(命题|定理|引理|推论|proposition|theorem|lemma|corollary)\s*([IVXLCDM]+|\d+(?:\.\d+)*|[一二三四五六七八九十百]+)',text,re.I)
    if not match:
        return None
    kind, number = match[1].casefold(),match[2].upper()
    kind = {'命题':'proposition','定理':'theorem','引理':'lemma','推论':'corollary'}.get(kind,kind)
    if re.fullmatch('[IVXLCDM]+',number):
        values = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
        number = str(sum(-values[c] if i+1<len(number) and values[c]<values[number[i+1]] else values[c] for i,c in enumerate(number)))
    elif not number[0].isdigit():
        total, digit = 0,0
        for c in number:
            if c in '十百':
                total += (digit or 1)*{'十':10,'百':100}[c]
                digit = 0
            else:
                digit = '一二三四五六七八九'.index(c)+1
        number = str(total+digit)
    return kind,number


def heading_level(text):
    match = NUMBER.match(re.sub(r'^\s*§\s*', '', text))
    if match:
        return match[1].count('.') + 1, 'numbering'
    if re.match(r'^\s*(?:第[一二三四五六七八九十百\d]+[篇部章]|PART\b|CHAPTER\b|[IVXLCDM]+\.)', text, re.I):
        return 1, 'numbering'
    if re.match(r'^\s*(?:第[一二三四五六七八九十百\d]+节|[一二三四五六七八九十]+、)', text):
        return 2, 'numbering'
    if re.fullmatch(r'\s*(?:摘要|摘\s*要|参考文献|结论|致谢|abstract|references|conclusions?|acknowledg[e]?ments?)\s*',text,re.I):
        return 1, 'explicit_section_name'
    return None, 'layout'


def build(blocks, *, title, bookmarks=(), max_chars=4000, preview_chars=120, proof_passage_chars=600, check=lambda: None):
    usable = [b for b in blocks if b['kind'] not in MARGINS and not b.get('structure', {}).get('index_excluded') and b['text'].strip()]
    if not usable:
        raise ValueError('no_structure_content')
    atoms = []
    for block in usable:
        check()
        starts = sorted({0, *[m.start() for m in PROOF.finditer(block['text']) if block['text'][:m.start()].strip()],
                         *[m.end() for m in PROOF_END.finditer(block['text']) if block['text'][m.end():].strip()]})
        for start, end in zip(starts, starts[1:] + [len(block['text'])]):
            atoms.append({'block': block, 'start': start, 'end': end, 'text': block['text'][start:end]})
    nodes, relations = [], []
    def add(kind, parent, start, end, label, basis, **attrs):
        node_id = 'N' + hashlib.sha256(json.dumps([kind, parent, atoms[start]['block']['block_id'],
            atoms[start]['start'], end, label], ensure_ascii=False).encode()).hexdigest()[:16]
        source = [{'block_id': a['block']['block_id'], 'start': a['start'], 'end': a['end']} for a in atoms[start:end]]
        parent_node = next((n for n in nodes if n['node_id'] == parent), None)
        node = {'node_id': node_id, 'parent_id': parent, 'kind': kind, 'ordinal': len(nodes),
                'depth': parent_node['depth'] + 1 if parent_node else 0, 'title': label,
                'preview': ' '.join(a['text'].strip() for a in atoms[start:end])[:preview_chars],
                'basis': basis, 'start_order': start, 'end_order': end,
                'page': atoms[start]['block']['page'], 'end_page': atoms[end-1]['block']['page'],
                'attributes': attrs, 'sources': source}
        nodes.append(node)
        return node
    root = add('document', '', 0, len(atoms), title, 'document')
    headings = []
    sizes = []
    for i, a in enumerate(atoms):
        block, text = a['block'], a['text']
        if a['start'] or block['kind'] not in HEADINGS or block['kind']=='doc_title' or STATEMENT.match(text) or PROOF.match(text):
            continue
        level, basis = heading_level(text)
        for mark in bookmarks:
            if mark[2] == block['page'] and SequenceMatcher(None, re.sub(r'\s+', '', mark[1]).casefold(), re.sub(r'\s+', '', text).casefold()).ratio() >= .85:
                level, basis = mark[0], 'bookmark'
                break
        box = block.get('bbox')
        size = (box[3] - box[1]) / max(1, text.count('\n') + 1) if box else 0
        if size:
            sizes.append(size)
        headings.append([i, level, basis, size])
    clusters = []
    for size in sorted(sizes, reverse=True):
        if not any(abs(size - v) <= .12 * max(size, v) for v in clusters):
            clusters.append(size)
    for item in headings:
        if item[1] is None:
            item[1] = min(range(len(clusters)), key=lambda j: abs(clusters[j] - item[3])) + 1 if item[3] and clusters else 1
    levels = {v: i + 1 for i, v in enumerate(sorted({h[1] for h in headings}))}
    stack, sections = [], {}
    for hi, (start, level, basis, _) in enumerate(headings):
        level = levels[level]
        while stack and stack[-1][0] >= level:
            stack.pop()
        end = next((h[0] for h in headings[hi+1:] if levels[h[1]] <= level), len(atoms))
        node = add('section', stack[-1][1]['node_id'] if stack else root['node_id'], start, end,
                   atoms[start]['text'].strip()[:240], basis, heading_block=atoms[start]['block']['block_id'])
        stack.append((level, node))
        sections[start] = node
    def parent_at(i):
        matching = [n for n in sections.values() if n['start_order'] <= i < n['end_order']]
        return max(matching, key=lambda n: n['depth']) if matching else root
    units, i, last_statement = [], 0, None
    while i < len(atoms):
        check()
        if i in sections:
            last_statement = None
            i += 1
            continue
        a = atoms[i]
        parent = parent_at(i)
        is_statement = bool(STATEMENT.match(a['text']))
        is_proof = bool(PROOF.match(a['text']))
        kind = 'statement' if is_statement else 'proof' if is_proof else 'table' if a['block']['kind'] == 'table' else 'paragraph'
        j, length = i + 1, len(a['text'])
        while j < len(atoms) and j not in sections and parent_at(j)['node_id'] == parent['node_id']:
            if kind == 'proof' and PROOF_END.search(atoms[j-1]['text']):
                break
            following = atoms[j]
            if STATEMENT.match(following['text']) or PROOF.match(following['text']) or following['block']['kind'] == 'table' or kind == 'table':
                break
            if kind == 'paragraph' and length + len(following['text']) > max_chars:
                break
            length += len(following['text'])
            j += 1
        unit = add(kind, parent['node_id'], i, j, a['text'].strip().split('\n')[0][:100],
                   'explicit_marker' if kind in ('statement', 'proof') else 'reading_order',
                   boundary='explicit' if kind in ('statement', 'proof', 'table') else 'bounded_paragraphs')
        units.append(unit)
        if is_statement:
            last_statement = unit
        proof_target = last_statement
        if is_proof and (named := statement_key(a['text'][:300])):
            matching = [n for n in units if n['kind']=='statement' and n['parent_id']==parent['node_id'] and statement_key(n['title'])==named]
            proof_target = matching[0] if len(matching)==1 else None
        if is_proof and proof_target:
            relations.append({'source_id': unit['node_id'], 'target_id': proof_target['node_id'],
                              'kind': 'explains', 'basis': 'following_proof_marker', 'evidence': unit['sources'][:1]})
            unit['attributes']['statement_id'] = proof_target['node_id']
        i = j
    for proof in [n for n in units if n['kind']=='proof']:
        start, length = proof['start_order'], 0
        for i in range(start,proof['end_order']):
            atom = atoms[i]
            # 只在实质性叙述之前分段；短连接语、紧随的公式和编号留在同一段。
            boundary = (i>start and length>=proof_passage_chars and atom['block']['kind'] not in ('display_formula','formula_number')
                        and len(atom['text'].strip())>=12 and not PROOF_END.search(atom['text']))
            if boundary:
                add('paragraph',proof['node_id'],start,i,atoms[start]['text'].strip().split('\n')[0][:100],'proof_passage')
                start, length = i,0
            length += len(atom['text'])
        add('paragraph',proof['node_id'],start,proof['end_order'],atoms[start]['text'].strip().split('\n')[0][:100],'proof_passage')
        narrative = [a for a in atoms[proof['start_order']:proof['end_order']]
                     if a['block']['kind'] not in ('display_formula','formula_number','table','image')
                     and len(re.sub(r'\$.*?\$', '', a['text'], flags=re.S).strip())>=8]
        if len(narrative)>=2 and len(narrative)<len(proof['sources']):
            guide = add('paragraph',proof['node_id'],proof['start_order'],proof['end_order'],
                        '证明叙述导览（公式需另读）','proof_route',full_node_id=proof['node_id'])
            guide['sources'] = [{'block_id':a['block']['block_id'],'start':a['start'],'end':a['end']} for a in narrative]
            guide['preview'] = ' '.join(a['text'].strip() for a in narrative)[:preview_chars]
    formulas = []
    for i, a in enumerate(atoms):
        s = a['block'].get('structure', {})
        if a['block']['kind'] != 'display_formula' or not s.get('formula_label'):
            continue
        owner = next((n for n in units if n['start_order'] <= i < n['end_order']), parent_at(i))
        formula = add('formula', owner['node_id'], i, i+1, '公式 (' + str(s['formula_label']) + ')', 'formula_label', number=str(s['formula_label']))
        members = set(s.get('formula_members', []))
        formula['sources'] = [{'block_id': b['block_id'], 'start': 0, 'end': len(b['text'])} for b in usable if b['block_id'] in members] or formula['sources']
        formula['end_page'] = max(b['page'] for b in usable if b['block_id'] in {p['block_id'] for p in formula['sources']})
        formulas.append(formula)
        if owner['kind'] == 'statement':
            conditions = [p for p in owner['sources'] if any(b['block_id'] == p['block_id'] and b['kind'] not in ('display_formula', 'formula_number')
                          and CONDITION.search(b['text'][p['start']:p['end']]) for b in usable)
                          and next(k for k, at in enumerate(atoms) if at['block']['block_id'] == p['block_id']) < i]
            if conditions:
                relations.append({'source_id': owner['node_id'], 'target_id': formula['node_id'], 'kind': 'condition_of',
                                  'basis': 'preceding_statement_condition', 'evidence': conditions})
    for unit in units:
        first = atoms[unit['start_order']]
        start_text = first['text']
        marker = PROOF.match(start_text) if unit['kind']=='proof' else None
        goal_end = len(start_text)
        if marker:
            stop = re.search(r'。|[.．](?:\s|$)|[，,;；]|\b(?:using|by|from|assume)\b|根据|利用|由',start_text[marker.end():],re.I)
            if stop:
                goal_end = marker.end()+stop.start()
        targets = list(TARGET.finditer(start_text[:goal_end])) if marker else []
        for match in targets:
            number = match[1]
            matching = [f for f in formulas if f['attributes']['number'] == number and
                        parent_at(f['start_order'])['node_id'] == unit['parent_id']]
            if len(matching) == 1:
                relations.append({'source_id': unit['node_id'], 'target_id': matching[0]['node_id'],
                                  'kind': 'proof_of', 'basis': 'explicit_proof_target', 'evidence':
                                  [{'block_id':first['block']['block_id'],'start':first['start']+match.start(),'end':first['start']+match.end()}]})
        references = {}
        for atom in atoms[unit['start_order']:unit['end_order']]:
            for mention in STATEMENT_REF.finditer(atom['text']):
                key = statement_key(mention[0])
                matching = [n for n in units if n['kind']=='statement' and statement_key(n['title'])==key]
                if len(matching)==1 and matching[0]['node_id']!=unit['node_id']:
                    relations.append({'source_id':unit['node_id'],'target_id':matching[0]['node_id'],
                        'kind':'refers_to','basis':'literal_statement_reference',
                        'evidence':[{'block_id':atom['block']['block_id'],'start':atom['start'],'end':atom['end']}]})
            for match in TARGET.finditer(atom['text']):
                references.setdefault(match[1],[]).append({'block_id':atom['block']['block_id'],
                    'start':atom['start']+match.start(),'end':atom['start']+match.end()})
        for number, evidence in references.items():
            matching = [f for f in formulas if f['attributes']['number'] == number and f['parent_id'] != unit['node_id']]
            if len(matching) == 1 and not any(r['source_id'] == unit['node_id'] and r['target_id'] == matching[0]['node_id'] for r in relations):
                relations.append({'source_id': unit['node_id'], 'target_id': matching[0]['node_id'], 'kind': 'refers_to',
                                  'basis': 'literal_reference', 'evidence': evidence})
    unique = {}
    for relation in relations:
        key = relation['source_id'],relation['target_id'],relation['kind']
        if key not in unique:
            unique[key] = relation
        else:
            unique[key]['evidence'].extend(s for s in relation['evidence'] if s not in unique[key]['evidence'])
    relations = list(unique.values())
    validate(nodes, relations, blocks)
    return nodes, relations, {'nodes': len(nodes), 'sections': len(sections), 'units': len(units),
        'source_blocks': len(usable), 'unresolved_hierarchy': sum(h[2] == 'layout' for h in headings),
        'semantic_verified': False, 'warnings': ['结构为导航推断，关联不等于语义核验；无明确边界的内容保留段落组。']}


def validate(nodes, relations, blocks):
    source = {b['block_id']: b for b in blocks}
    known = {n['node_id']: n for n in nodes}
    if len(known) != len(nodes):
        raise ValueError('duplicate_structure_node')
    for node in nodes:
        if node['parent_id']:
            parent = known[node['parent_id']]
            if not parent['start_order'] <= node['start_order'] < node['end_order'] <= parent['end_order']:
                raise ValueError('invalid_structure_containment')
        seen, current = set(), node
        while current['parent_id']:
            if current['node_id'] in seen:
                raise ValueError('cyclic_structure')
            seen.add(current['node_id'])
            current = known[current['parent_id']]
        for span in node['sources']:
            if not 0 <= span['start'] < span['end'] <= len(source[span['block_id']]['text']):
                raise ValueError('invalid_structure_source')
    for relation in relations:
        if relation['source_id'] not in known or relation['target_id'] not in known or not relation['evidence']:
            raise ValueError('invalid_structure_relation')
        for span in relation['evidence']:
            if not 0 <= span['start'] < span['end'] <= len(source[span['block_id']]['text']):
                raise ValueError('invalid_relation_source')
