"""章节归属的唯一投影；原文与块编号不随结构版本变化。"""

import re
import statistics
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from rag.document_features import normalized

VERSION = 5
NUMBER = re.compile(r'^\s*(\d+(?:\.\d+)*)(?:[.、．\s]|$)')


def heading_level(text):
    """从标题编号或明确章节名称推断层级，并返回判断依据。"""
    match = NUMBER.match(re.sub(r'^\s*§\s*', '', text))
    if match:
        return match[1].count('.') + 1, 'numbering'
    if re.match(r'^\s*(?:第[一二三四五六七八九十百\d]+[篇部章]|PART\b|CHAPTER\b|[IVXLCDM]+\.)', text, re.I):
        return 1, 'numbering'
    if re.match(r'^\s*(?:第[一二三四五六七八九十百\d]+节|[一二三四五六七八九十]+、)', text):
        return 2, 'numbering'
    if re.fullmatch(r'\s*(?:摘要|摘\s*要|参考文献|结论|致谢|abstract|references|conclusions?|acknowledg[e]?ments?)\s*', text, re.I):
        return 1, 'explicit_section_name'
    return None, 'layout'


def marker(text):
    """解析章节标号的体系、编号和标题，无法识别时返回空值。"""
    text = unicodedata.normalize('NFKC', text).strip()
    patterns = [
        ('part', r'^(?:第([一二三四五六七八九十百\d]+)[篇部]|PART\s+([IVXLCDM]+|[A-Z]|\d+)\b)[\s:：.、]*(.*)$'),
        ('chapter', r'^(?:第([一二三四五六七八九十百\d]+)章|CHAPTER\s+([IVXLCDM]+|[A-Z]|\d+)\b)[\s:：.、]*(.*)$'),
        ('section', r'^(?:第([一二三四五六七八九十百\d]+)节|SECTION\s+([IVXLCDM]+|[A-Z]|\d+)\b)[\s:：.、]*(.*)$'),
        ('appendix', r'^(?:附录|APPENDIX)\s*([A-Z]|\d+|[一二三四五六七八九十]+)[\s:：.、]+(.*)$'),
        ('decimal', r'^§?\s*(\d+(?:\.\d+)*)(?:[.、．]\s*|\s+)(\S.*)$'),
        ('cjk', r'^([一二三四五六七八九十百]+)、\s*(\S.*)$'),
        ('cjk_paren', r'^\(([一二三四五六七八九十百]+)\)\s*(\S.*)$'),
        ('arabic_paren', r'^\((\d+)\)\s*(\S.*)$'),
        ('roman', r'^([IVXLCDM]+)\.\s+(\S.*)$'),
        ('alpha', r'^([A-Z])\.\s+(\S.*)$'),
    ]
    for family, pattern in patterns:
        found = re.match(pattern, text, re.I if family in ('part','chapter','section','appendix') else 0)
        if found:
            groups = found.groups()
            number = next(v for v in groups[:-1] if v)
            return {'family': family, 'number': number, 'parts': number.split('.') if family == 'decimal' else [number],
                    'title': groups[-1]}
    return None


def match_score(mark, block):
    """结合标题相似度及页内位置，计算目录项与候选标题块的匹配分数。"""
    text, title = block['text'], mark['title']
    a, b = marker(text), marker(title)
    one, two = normalized(a['title'] if a else text), normalized(b['title'] if b else title)
    if not one or not two:
        return 0
    similarity = SequenceMatcher(None, one, two).ratio()
    distance = abs(mark['page']-block['page']) if mark.get('page') else 0
    if similarity < .85 or distance > 1 or (distance and similarity < .96):
        return 0
    if a and b and a['family'] == b['family'] and a['number'] != b['number']:
        return 0
    position = block['structure'].get('heading_features', {})
    y = mark.get('y')
    return similarity - .12*distance - (.20*abs(position['y']-y) if y is not None and 'y' in position else 0)


def align(marks, candidates):
    # 保持目录顺序的一对一匹配，允许跳过无正文锚点的目录项。
    """在顺序约束下选择目录项与候选标题的最佳对应关系。"""
    states = {-1: (0, [])}
    for mark in marks:
        previous, updated = states, dict(states)
        for index, block in candidates:
            score = match_score(mark, block)
            if score <= 0:
                continue
            best = max((value for position,value in previous.items() if position < index), key=lambda v:v[0], default=None)
            if best and best[0]+score > updated.get(index,(-1,[]))[0]:
                updated[index] = (best[0]+score, [*best[1],(index,mark,score)])
        states, high = {}, -1
        for index, value in sorted(updated.items()):
            if value[0] > high:
                states[index], high = value, value[0]
    return {i: {**m,'score':score} for i,m,score in max(states.values(),key=lambda v:v[0])[1]}


def printed_toc(blocks, pages):
    """从印刷目录提取标题和目标页信息，并记录目录来源块。"""
    labels = {}
    for page in pages:
        if page.get('printed_page'):
            labels.setdefault(str(page['printed_page']).casefold(), []).append(page['page'])
    toc_pages = {b['page'] for b in blocks if re.fullmatch(r'\s*(?:目\s*录|contents|table of contents)\s*',b['text'],re.I)}
    excluded_pages = {b['page'] for b in blocks if re.fullmatch(r'\s*(?:图目录|表目录|list of (?:figures|tables))\s*',b['text'],re.I)}
    marks, sources = [], set()
    for block in blocks:
        if block['page'] in excluded_pages or (block['kind'] not in ('content','toc') and block['page'] not in toc_pages):
            continue
        offset = 0
        for line in block['text'].splitlines(keepends=True):
            match = re.match(r'^\s*(.+?)(?:\.{2,}|…+|\s{2,}|\t+)\s*(\d+|[ivxlcdm]+)\s*$',line,re.I)
            if match:
                positions = labels.get(match[2].casefold(), [])
                marks.append({'title':match[1].strip(),'page':positions[0] if len(positions)==1 else None,
                              'basis':'printed_toc','source':{'block_id':block['block_id'],'start':offset,'end':offset+len(line)}})
                sources.add(block['block_id'])
            offset += len(line)
    return marks, sources


def assign(blocks, bookmarks=(), *, pages=()):
    """结合书签、印刷目录和版式线索确定章节归属，写入块的结构投影。"""
    from rag.document_layout import HEADINGS, MARGINS, STATEMENT
    from rag.document_hierarchy import PROOF
    toc, toc_blocks = printed_toc(blocks, pages)
    candidates = []
    for index, block in enumerate(blocks):
        s = block['structure']
        s.pop('section_heading', None)
        s['section_path'] = []
        s['hierarchy_version'] = VERSION
        block['section_id'] = ''
        text = block['text']
        if (s.get('index_excluded') or block['kind'] in MARGINS or block['block_id'] in toc_blocks
                or block['kind'] in ('table','image','display_formula','formula_number','reference','reference_content')
                or not text.strip() or STATEMENT.match(text) or PROOF.match(text)):
            continue
        if re.fullmatch(r'\s*(?:目\s*录|图目录|表目录|contents|table of contents|list of (?:figures|tables))\s*',text,re.I):
            continue
        if block['kind'] in HEADINGS or (len(text.strip()) <= 160 and text.count('\n') <= 2 and not re.search(r'[.。！？!?;；]\s*$',text)):
            candidates.append((index,block))
    marks = [{'level':m[0],'title':m[1],'page':m[2],'basis':'bookmark',
              **(m[3] if len(m)>3 and isinstance(m[3],dict) else {})} for m in bookmarks if len(m)>=3 and 0<m[0]<=20 and m[2]>0]
    bookmark_matches, toc_matches = align(marks,candidates), align(toc,candidates)
    anchors = Counter()
    for _, block in candidates:
        number = marker(block['text'])
        if block['kind'] in HEADINGS and number and (block['kind']!='doc_title' or number['family'] in ('part','chapter','section','appendix')):
            anchors[number['family']] += 1
    headings, sizes = [], []
    named_families = {'part','chapter','section','appendix'}
    for index, block in candidates:
        number = marker(block['text'])
        match = bookmark_matches.get(index) or toc_matches.get(index)
        style = block['structure'].get('heading_features',{})
        inferred = (number and (anchors[number['family']] >= 2 or
                    number['family'] in named_families and any(anchors[f] for f in named_families))
                    and len(block['text']) <= 80 and block['text'].count('\n') == 0)
        if inferred and block['kind']=='doc_title' and number['family']=='decimal':
            inferred = any(other['kind'] in HEADINGS and other['kind']!='doc_title' and
                           (n:=marker(other['text'])) and n['family']=='decimal' and
                           abs(int(n['parts'][0])-int(number['parts'][0]))<=1 for _,other in candidates)
        detected = block['kind'] in HEADINGS and block['kind']!='doc_title'
        if not detected and not match and not inferred:
            continue
        level, basis = heading_level(block['text'])
        if number:
            level, basis = None, 'numbering'
        if match:
            level, basis = match.get('level'), match['basis']
        box = block.get('bbox')
        size = style.get('size',style.get('height', (box[3]-box[1])/max(1,block['text'].count('\n')+1) if box else 0))
        if size:
            sizes.append(size)
        evidence = [basis]
        if detected:
            evidence.append('layout_heading')
        elif inferred:
            evidence.append('document_numbering_run')
        if match and match.get('source'):
            evidence.append('toc_body_alignment')
        h = {'index':index,'block_id':block['block_id'],'level':level,'basis':basis,'size':size,
             'numbering':number,'evidence':evidence,'candidate_origin':block['kind'],
             'font_style': [style.get('bold'),style.get('italic')] if style.get('source')=='native_text_aligned' else None,
             'anchor_status':'matched' if match else 'detected' if detected else 'inferred'}
        if match:
            h['alignment'] = match
        if index in bookmark_matches and index in toc_matches:
            h['evidence'].append('printed_toc')
        headings.append(h)
    families = list(dict.fromkeys(h['numbering']['family'] for h in headings if h['numbering'] and h['numbering']['family']!='appendix'))
    for before, after in [('part','chapter'),('chapter','section'),('cjk','cjk_paren'),('decimal','arabic_paren')]:
        if before in families and after in families and families.index(before)>families.index(after):
            families.remove(before)
            families.insert(families.index(after),before)
    bases, next_level = {}, 1
    for family in families:
        anchors = [h['level']-len(h['numbering']['parts'])+1 for h in headings if h['numbering'] and h['numbering']['family']==family and h['level'] is not None]
        bases[family] = max(1,round(statistics.median(anchors))) if anchors else next_level
        next_level = bases[family]+max(len(h['numbering']['parts']) for h in headings if h['numbering'] and h['numbering']['family']==family)
    for h in headings:
        if h['level'] is None and h['numbering']:
            n=h['numbering']
            h['level']=1 if n['family']=='appendix' else bases[n['family']]+len(n['parts'])-1
    clusters = []
    for size in sorted(sizes, reverse=True):
        if not any(abs(size - v) <= .12 * max(size, v) for v in clusters):
            clusters.append(size)
    for h in headings:
        if h['level'] is None:
            compatible = {a['level'] for a in headings if a['level'] is not None and a['size'] and h['size']
                          and (not a['font_style'] or not h['font_style'] or a['font_style']==h['font_style'])
                          and abs(a['size']-h['size']) <= .08*max(a['size'],h['size'])}
            h['level'] = next(iter(compatible)) if len(compatible)==1 else min(range(len(clusters)),key=lambda j:abs(clusters[j]-h['size']))+1 if h['size'] and clusters else 1
            h['basis'] = 'anchored_style' if len(compatible)==1 else 'layout'
    stack = []
    for i, h in enumerate(headings):
        h['declared_level'] = h['level']
        h['issues'] = []
        h['parent_status'] = 'inferred' if h['basis'] in ('layout', 'anchored_style') or h['anchor_status']=='inferred' else 'supported'
        while stack and stack[-1]['level'] >= h['level']:
            stack.pop()
        number = h['numbering']
        if number and number['family']=='decimal' and len(number['parts'])>1:
            expected = next((j for j,p in enumerate(stack) if p['numbering'] and
                            p['numbering']['family']=='decimal' and p['numbering']['parts']==number['parts'][:-1]), None)
            if expected is not None:
                stack = stack[:expected+1]
                h['evidence'].append('number_prefix_parent')
            else:
                h['issues'].append('missing_numbered_parent')
                h['parent_status'] = 'unresolved'
                first_decimal = next((j for j,p in enumerate(stack) if p['numbering'] and p['numbering']['family']=='decimal'), len(stack))
                stack = stack[:first_decimal]
        if h['level'] > (stack[-1]['level'] if stack else 0)+1:
            h['issues'].append('missing_intermediate_level')
            h['parent_status'] = 'unresolved'
        if h['basis']=='layout':
            h['issues'].append('unanchored_style')
        if h['basis']=='bookmark' and 'missing_numbered_parent' in h['issues']:
            h['issues'].append('bookmark_numbering_conflict')
        h['parent_heading'] = stack[-1]['block_id'] if stack else ''
        if stack and stack[-1]['parent_status']=='unresolved':
            h['issues'].append('ancestor_unresolved')
            h['parent_status'] = 'unresolved'
        stack.append(h)
        h['path'] = [p['block_id'] for p in stack]
    for i, h in enumerate(headings):
        following = next((v for v in headings[i + 1:] if h['block_id'] not in v['path']), None)
        h['end_heading'] = following['block_id'] if following else ''
        h['boundary_status'] = 'review' if h['parent_status']!='supported' or (following and following['parent_status']!='supported') else 'bounded'
        blocks[h['index']]['structure']['section_heading'] = {k: v for k, v in h.items() if k not in ('index', 'size', 'path')}
    by_start = {h['index']: h for h in headings}
    path = []
    for index, block in enumerate(blocks):
        if index in by_start:
            path = by_start[index]['path']
        if block['kind'] not in MARGINS and not block['structure'].get('index_excluded'):
            block['section_id'] = path[-1] if path else ''
            block['structure']['section_path'] = list(path)
    by_id = {b['block_id']: b for b in blocks}
    for block in blocks:
        parent = by_id.get(block.get('parent_id'))
        if parent and parent['section_id'] != block['section_id']:
            block['parent_id'] = ''
            block['structure'].pop('record_relation', None)
        for key in ('paragraph_id', 'table_id', 'continuation_of'):
            origin = by_id.get(block['structure'].get(key))
            if origin and origin['section_id'] != block['section_id']:
                block['structure'].pop(key, None)
                block['structure'].pop('related_blocks', None)
    return blocks
