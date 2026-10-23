"""跨页关系只补充来源关系，不改写任何已保存块正文。"""

import re
from collections import Counter, defaultdict

MARGINS = {'header', 'header_image', 'footer', 'footer_image', 'page_number', 'number'}
HEADINGS = {'title', 'doc_title', 'paragraph_title', 'section_title'}
STATEMENT = re.compile(r'^\s*(?:命题|定理|引理|推论|Proposition|Theorem|Lemma|Corollary)\s*'
                       r'(?:[IVXLCDMⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+|\d+(?:\.\d+)*|[一二三四五六七八九十百]+)'
                       r'(?=[\s.．、:：(（\[]|$)', re.I)


def statement_context(blocks, block_id):
    """提取包含目标块的陈述上下文，供公式或证明关联使用。"""
    current = []
    for block in blocks:
        if block['kind'] in MARGINS:
            continue
        if STATEMENT.match(block['text']) or block['kind'] in HEADINGS:
            if block_id in current:
                return current
            current = []
        if STATEMENT.match(block['text']) or current:
            current.append(block['block_id'])
    return current if block_id in current else []


def printed_page(blocks):
    """从页块中识别唯一可信的印刷页码，出现歧义时不作判断。"""
    values = set()
    for b in blocks:
        if b.get('type', b.get('kind')) not in ('page_number', 'number'):
            continue
        text = b['text'].strip().strip('-—– ')
        match = re.fullmatch(r'(?:(?:第|[Pp]age|[Pp]\.)\s*)?(\d{1,5}|[ivxlcdmIVXLCDM]{1,8})(?:\s*页)?', text)
        if match:
            values.add(match[1])
    return next(iter(values)) if len(values) == 1 else None


def table_structure(rows):
    """展开表格单元格的行列跨度，返回列数、表头和单元格结构。"""
    occupied, cells, columns = {}, [], 0
    for ri, row in enumerate(rows):
        ci = 0
        for original in row:
            while (ri, ci) in occupied:
                ci += 1
            spans = []
            for key in ('rowspan', 'colspan'):
                value = str(original.get(key, '1'))
                span = int(value) if value.isdigit() else 1
                if not 1 <= span <= 1000:
                    raise ValueError('table_span_out_of_range')
                spans.append(span)
            rs, cs = spans
            if ci + cs > 1000:
                raise ValueError('table_columns_out_of_range')
            cell = {**original, 'row': ri, 'column': ci, 'rowspan': rs, 'colspan': cs}
            cells.append(cell)
            for r in range(ri, min(len(rows), ri + rs)):
                for c in range(ci, ci + cs):
                    if (r, c) in occupied:
                        raise ValueError('overlapping_table_cells')
                    occupied[r, c] = len(cells) - 1
            ci += cs
            columns = max(columns, ci)
    headers = []
    for ri, row in enumerate(rows):
        if row and all(c.get('header') for c in row):
            headers.append(ri)
        else:
            break
    source = 'explicit_th' if headers else 'leading_rows_context'
    if not headers and rows:
        stop = 1
        for ri in range(min(8, len(rows))):
            if ri >= stop:
                break
            stop = min(8, len(rows), max([stop, *[c['row'] + c['rowspan'] for c in cells if c['row'] == ri]]))
        headers = list(range(stop))
    return {'cells': cells, 'column_count': columns, 'header_rows': headers, 'header_source': source,
            'cell_coordinates': 'not_provided'}


def link_document(blocks, pages, bookmarks=(), features=None):
    """补充阅读顺序、章节及跨页表格等结构关联，同时保留原块正文。"""
    from rag.document_features import enrich, reading_order
    blocks = reading_order(enrich(blocks, features or {}), pages)
    dimensions = {p['page']: (p['width'], p['height']) for p in pages}
    by_page = defaultdict(list)
    for b in blocks:
        by_page[b['page']].append(b)
    repeated = Counter()
    for number, items in by_page.items():
        height = dimensions[number][1]
        repeated.update({re.sub(r'\s+', '', b['text']) for b in items if b['bbox']
                         and (b['bbox'][3] < height * .12 or b['bbox'][1] > height * .88)
                         and 2 <= len(b['text']) <= 120})
    for number, items in sorted(by_page.items()):
        height = dimensions[number][1]
        for b in items:
            s = b['structure']
            box = b['bbox']
            if b['kind'] in MARGINS or (box and repeated[re.sub(r'\s+', '', b['text'])] >= 3
                                      and (box[3] < height * .12 or box[1] > height * .88)):
                s['index_excluded'] = 'repeated_margin'
    from rag.document_sections import assign
    assign(blocks, bookmarks, pages=pages)
    previous = None
    for number, items in sorted(by_page.items()):
        width, height = dimensions[number]
        for b in items:
            s, box = b['structure'], b['bbox']
            if b['kind'] in ('inline_formula', 'formula') and box:
                for text in items:
                    a = text['bbox']
                    if text['kind'] != 'text' or not a:
                        continue
                    normalized = re.sub(r'[\s$]', '', b['text'])
                    if (normalized and normalized in re.sub(r'[\s$]', '', text['text'])
                            and a[0] <= box[0] <= box[2] <= a[2] and a[1] <= box[1] <= box[3] <= a[3]):
                        s['index_excluded'] = 'duplicate_inline_formula'
                        s['duplicate_of'] = text['block_id']
                        s['section_path'] = []
                        b['section_id'] = ''
                        break
            if b['kind'] == 'table':
                previous_table = max((v['ordinal'] for v in items if v['ordinal'] < b['ordinal'] and v['kind'] == 'table'), default=-1)
                before = [v for v in items if previous_table < v['ordinal'] < b['ordinal'] and v['kind'] in ('text', 'table_title', 'table_caption')
                          and v['section_id'] == b['section_id']][-3:]
                captions = [v for v in before if re.search(r'单位\s*[:：为是]|[Uu]nit(?:s)?\s*[:：]|[Cc]urrency\s*[:：]', v['text'])]
                if captions:
                    s['context_blocks'] = [v['block_id'] for v in captions]
            if b['kind'] == 'formula_number' and box:
                label = re.fullmatch(r'\s*[（(]?\s*([0-9]+(?:[.\-][0-9]+)*[a-z]?)\s*[)）]?\s*', b['text'])
                matches = [v for v in items if v['kind'] == 'display_formula' and v['bbox']
                           and abs((box[1] + box[3]) - (v['bbox'][1] + v['bbox'][3]))
                           <= max(box[3] - box[1], v['bbox'][3] - v['bbox'][1])]
                if label and len(matches) == 1:
                    formula = matches[0]
                    members = [formula['block_id'], b['block_id']]
                    for target in (b, formula):
                        target['structure'].update(formula_label=label[1], formula_members=members)
        body = [b for b in items if b['text'].strip() and not b['structure'].get('index_excluded')]
        if previous and number == previous[0] + 1 and body:
            old = previous[1]
            if old:
                a, b = old[-1], body[0]
                aw, ah = dimensions[a['page']]
                aa, bb = a['bbox'], b['bbox']
                aligned = aa and bb and abs(aa[0] / aw - bb[0] / width) < .04 and abs(aa[2] / aw - bb[2] / width) < .08
                if aligned and aa[3] > .72 * ah and bb[1] < .28 * height and a['section_id'] == b['section_id']:
                    if (a['kind'] == b['kind'] == 'text' and not re.search(r'[。！？.!?:：;；]\s*$', a['text'])
                            and not re.match(r'\s*(?:\d+[.、]|[A-Za-z0-9]+[-_][A-Za-z0-9]+\s*[:：])', b['text'])
                            and not b['parent_id']):
                        root = a['structure'].get('paragraph_id', a['block_id'])
                        a['structure']['paragraph_id'] = b['structure']['paragraph_id'] = root
                        b['structure']['continuation_of'] = a['block_id']
                        b['parent_id'] = a['parent_id']
                    if a['kind'] == b['kind'] == 'table':
                        sa, sb = a['structure'], b['structure']
                        rows_a, rows_b = sa.get('rows', []), sb.get('rows', [])
                        if rows_a and rows_b and sa.get('column_count') == sb.get('column_count'):
                            ha = [' '.join(c['text'].split()) for c in rows_a[0]]
                            hb = [' '.join(c['text'].split()) for c in rows_b[0]]
                            if ha == hb:
                                root = sa.get('table_id', a['block_id'])
                                sa['table_id'] = sb['table_id'] = root
                                sb['continuation_of'] = a['block_id']
                                sb['context_blocks'] = list(dict.fromkeys([*sa.get('context_blocks', []), *sb.get('context_blocks', [])]))
        previous = number, body
    groups = defaultdict(list)
    for b in blocks:
        for kind in ('paragraph_id', 'table_id'):
            if b['structure'].get(kind):
                groups[kind, b['structure'][kind]].append(b['block_id'])
    for b in blocks:
        for kind in ('paragraph_id', 'table_id'):
            if b['structure'].get(kind):
                b['structure']['related_blocks'] = groups[kind, b['structure'][kind]]
    return blocks
