"""保留解析文本、表格关系及字符位置，生成有来源的检索单元。"""

import hashlib
import json
import os
import re
from html.parser import HTMLParser

import requests

from rag import document_config as C
from rag.document_layout import table_structure

IDENTIFIER = re.compile(r"(?<![\w-])[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+(?![\w-])")
HEADINGS = {'title', 'doc_title', 'paragraph_title', 'section_title'}


def identifiers(text):
    return [(m.group().casefold(), m.group(), m.start(), m.end()) for m in IDENTIFIER.finditer(text)
            if any(c.isdigit() for c in m.group()) and any(c.isalpha() for c in m.group())]


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.ignore += 1
        if tag == 'tr':
            self.row = []
        if tag in ('td', 'th') and self.row is not None:
            attrs = dict(attrs)
            self.cell = {'text': '', 'header': tag == 'th',
                         'rowspan': attrs.get('rowspan', '1'), 'colspan': attrs.get('colspan', '1')}
        if tag == 'br' and self.cell is not None:
            self.cell['text'] += '\n'

    def handle_data(self, data):
        if self.cell is not None and not self.ignore:
            self.cell['text'] += data

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignore = max(0, self.ignore - 1)
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(self.cell)
            self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def normalize_page(result):
    page, blocks, found = result['page_number'], [], []
    section, anchor, labels, families = '', '', set(), set()
    for i, item in enumerate(result['blocks']):
        block_id = f"P{page}B{i + 1}"
        kind, text = str(item.get('type', 'text')), str(item.get('text', ''))
        structure = {'reading_order': item.get('reading_order'), 'group_id': item.get('group_id')}
        if kind == 'table' and '<table' in text.lower():
            parser = TableParser()
            parser.feed(text)
            if parser.rows:
                structure['rows'] = parser.rows
                structure.update(table_structure(parser.rows))
                parts, ranges, pos = [], [], 0
                for row in parser.rows:
                    value = ' | '.join(c['text'].strip().replace('\n', ' ') for c in row)
                    parts.append(value)
                    ranges.append([pos, pos + len(value)])
                    pos += len(value) + 1
                text = '\n'.join(parts)
                structure['row_ranges'] = ranges
        if kind in HEADINGS:
            section = block_id
            anchor, labels, families = '', set(), set()
        entries = identifiers(text)
        fields = set()
        for _, _, start, _ in entries:
            line_start = max(text.rfind('\n', 0, start), text.rfind('；', 0, start), text.rfind(';', 0, start)) + 1
            match = re.fullmatch(r'\s*([^:：\n]{1,40})[:：]\s*', text[line_start:start])
            if match:
                fields.add(match[1].strip().casefold())
        current_families = {re.sub(r'\d+', '#', value) for value, *_ in entries}
        if kind == 'table':
            anchor, labels, families = '', set(), set()
        elif kind not in HEADINGS and fields and (not anchor or fields & labels or current_families & families):
            # 只将显式字段记录与同章节的后续块关联；同字段/编号族的新记录终止旧范围。
            anchor, labels, families = block_id, fields, current_families
        block = {'block_id': block_id, 'page': page, 'ordinal': i, 'kind': kind, 'text': text,
                 'bbox': item.get('bbox'), 'section_id': section, 'parent_id': anchor, 'structure': structure}
        if anchor:
            structure['record_relation'] = 'same_page_section_after_labeled_identifier'
        if blocks and kind == 'text':
            previous = blocks[-1]
            a, b = previous['bbox'], block['bbox']
            if (previous['kind'] == 'text' and previous['section_id'] == section
                    and previous.get('parent_id') == anchor and a and b
                    and not re.search(r'[。！？.!?:：;；]\s*$', previous['text'])
                    and abs(a[0] - b[0]) <= max(8, .05 * (a[2] - a[0]))
                    and 0 <= b[1] - a[3] <= max(a[3] - a[1], b[3] - b[1]) * 1.5):
                structure['paragraph_id'] = previous['structure'].get('paragraph_id', previous['block_id'])
                previous['structure']['paragraph_id'] = structure['paragraph_id']
        blocks.append(block)
        found.extend((block_id, *value) for value in identifiers(text))
    sections = {}
    for block in blocks:
        if block['section_id'] and block['kind'] == 'text':
            entries = identifiers(block['text'])
            if entries and not block['text'][:entries[0][2]].strip():
                sections.setdefault(block['section_id'], {}).setdefault(entries[0][0], []).append(block)
    for section, entries in sections.items():
        # 同节反复以唯一完整编号起头，建立记录范围；一旦出现另一个起头编号就不推断归属。
        if len(entries) != 1:
            continue
        anchors = next(iter(entries.values()))
        if len(anchors) < 2:
            continue
        first = anchors[0]
        for block in blocks:
            if (block['section_id'] == section and block['ordinal'] >= first['ordinal']
                    and block['kind'] == 'text' and not block['parent_id']):
                block['parent_id'] = first['block_id']
                block['structure']['record_relation'] = 'unique_repeated_leading_identifier_in_section'
    return blocks, found


class EmbeddingCounter:
    def __init__(self, check=None):
        base = os.getenv('EMBEDDING_BASE_URL', '').rstrip('/')
        self.url = C.TOKENIZER_URL or (base.removesuffix('/v1') + '/tokenize' if base else '')
        self.model = os.getenv('EMBEDDING_MODEL', '')
        self.cache = {}
        self.session = requests.Session()
        self.session.trust_env = False
        self.check = check

    def count(self, text):
        if self.check:
            self.check()
        key = hashlib.sha256(text.encode()).digest()
        if key not in self.cache:
            if not self.url or not self.model:
                raise ValueError('embedding_tokenizer_not_configured')
            response = self.session.post(self.url, json={'model': self.model, 'prompt': text, 'add_special_tokens': True},
                                         headers={'Authorization': 'Bearer ' + os.getenv('EMBEDDING_API_KEY', '')},
                                         timeout=(5, 20), allow_redirects=False)
            response.raise_for_status()
            count = response.json().get('count')
            if type(count) is not int or count < 0:
                raise ValueError('invalid_embedding_token_count')
            self.cache[key] = count
            if len(self.cache) > 2048:
                self.cache.pop(next(iter(self.cache)))
        return self.cache[key]


def build_chunks(blocks, filename, counter):
    by_id = {b['block_id']: b for b in blocks}
    formula_links = {}
    for number in (b for b in blocks if b['kind'] == 'formula_number'):
        a = number['bbox']
        if not a:
            continue
        matches = [b for b in blocks if b['kind'] == 'display_formula' and b['page'] == number['page']
                   and b['bbox'] and abs((a[1] + a[3]) - (b['bbox'][1] + b['bbox'][3]))
                   <= max(a[3] - a[1], b['bbox'][3] - b['bbox'][1])]
        if len(matches) == 1:
            formula_links[number['block_id']] = matches[0]['block_id']
    groups = {}
    for block in blocks:
        key = formula_links.get(block['block_id'], block['structure'].get('paragraph_id', block['block_id']))
        groups.setdefault(key, []).append(block)
    for members in groups.values():
        formulas = [b for b in members if b['kind'] == 'display_formula']
        if formulas and len(members) > 1:
            first = min(members, key=lambda b: b['ordinal'])
            previous = next((b for b in blocks if b['page'] == first['page'] and b['ordinal'] == first['ordinal'] - 1), None)
            if previous and previous['kind'] == 'text' and previous['section_id'] == first['section_id'] and len(previous['text']) <= 300:
                members.append(previous)
            members.sort(key=lambda b: b['ordinal'])
    # 标题与其下的首页信息形成元信息入口；保留各原块来源，不猜测作者字段值。
    metadata = []
    for members in groups.values():
        first = members[0]
        if first['kind'] == 'doc_title':
            metadata.append(list(members))
        elif (metadata and len(metadata[-1]) < 4 and first['kind'] == 'text'
              and first['page'] == metadata[-1][0]['page'] and first['section_id'] == metadata[-1][0]['block_id']):
            metadata[-1].extend(members)
    chunks = []
    for group in metadata:
        if group[0]['page'] == 1:
            group.extend(b for b in blocks if b['page'] == 1 and b['kind'] in ('header', 'footer') and b['text'].strip())
    for members in [*groups.values(), *metadata]:
        block = next((b for b in members if b['kind'] == 'display_formula'), members[0])
        if not block['text'].strip() or block['structure'].get('index_excluded') or block['kind'] in ('header', 'footer', 'page_number', 'number'):
            continue
        spans, pos = [], 0
        for member in members:
            spans.append((member, pos, pos + len(member['text'])))
            pos += len(member['text']) + 1
        block = {**block, 'text': '\n'.join(member['text'] for member in members)}
        if any(members is group for group in metadata):
            block['kind'] = 'metadata'
        heading = by_id.get(block.get('section_id'), {})
        record = by_id.get(block.get('parent_id'), {})
        member_ids = {member['block_id'] for member in members}
        prefix = f"文档：{filename}\n物理页：{block['page']}\n"
        if block['kind'] == 'image':
            prefix += '图片区域的 OCR 文字，不代表箭头或空间关系：\n'
        if block['kind'] == 'metadata':
            prefix += '首页标题及其下的文档元信息：\n'
        if heading and heading['block_id'] not in member_ids:
            prefix += f"章节：{heading['text']}\n"
        if record and record['block_id'] not in member_ids:
            prefix += f"记录入口：{record['text']}\n"
        context_blocks = [by_id[i] for i in block['structure'].get('context_blocks', []) if i in by_id and i not in member_ids]
        for extra in context_blocks:
            prefix += f"表格说明：{extra['text']}\n"
        structure = block['structure']
        row_ranges = structure.get('row_ranges', [])
        if row_ranges:
            header_end = row_ranges[max(structure.get('header_rows', [0]))][1]
            header = block['text'][:header_end]
            units = [(a, b, i, i + 1) for i, (a, b) in enumerate(row_ranges)]
        else:
            header, units = '', [(0, len(block['text']), None, None)]
            if block['kind'] in ('reference', 'reference_content') or re.search(r'参考文献|references', heading.get('text', ''), re.I):
                starts = [m.start() for m in re.finditer(r'(?m)^\s*(?:\[\d{1,4}\]|\d{1,4}[.、])\s*\S', block['text'])]
                if starts:
                    starts[0] = 0
                    units = [(a, b, None, None) for a, b in zip(starts, starts[1:] + [len(block['text'])])]
        for start, end, row_start, row_end in units:
            context = prefix + (f"表格首行：{header}\n" if header and row_start else '')
            inherited_rows = sorted({c['row'] for c in structure.get('cells', []) if row_start is not None
                                     and c['row'] < row_start < c['row'] + c['rowspan'] and c['row'] not in structure['header_rows']})
            for ri in inherited_rows:
                a, b = row_ranges[ri]
                context += f"合并单元格来源行：{block['text'][a:b]}\n"
            if counter.count(context) >= C.CHUNK_TOKENS // 2:
                # 检索前缀不是原文，不允许它挤走整段证据；来源仍保存完整章节关系。
                context = f"物理页：{block['page']}\n"
            offset = start
            while offset < end:
                lo, hi = offset, min(end, offset + C.CHUNK_TOKENS * 8)
                if counter.count(context + block['text'][offset:hi]) <= C.CHUNK_TOKENS:
                    lo = hi
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if counter.count(context + block['text'][offset:mid]) <= C.CHUNK_TOKENS:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo == offset:
                    raise ValueError('embedding_chunk_budget_too_small')
                sources = [{'block_id': member['block_id'], 'start': max(offset, begin) - begin,
                            'end': min(lo, stop) - begin, 'row_start': row_start, 'row_end': row_end}
                           for member, begin, stop in spans if max(offset, begin) < min(lo, stop)]
                if header and row_start and '表格首行' in context:
                    sources.append({'block_id': block['block_id'], 'start': 0, 'end': header_end,
                                    'row_start': 0, 'row_end': max(structure.get('header_rows', [0])) + 1})
                if '合并单元格来源行：' in context:
                    sources.extend({'block_id': block['block_id'], 'start': row_ranges[ri][0], 'end': row_ranges[ri][1],
                                    'row_start': ri, 'row_end': ri + 1} for ri in inherited_rows)
                if heading and heading['block_id'] not in member_ids and '章节：' in context:
                    sources.append({'block_id': heading['block_id'], 'start': 0, 'end': len(heading['text'])})
                if record and record['block_id'] not in member_ids and '记录入口：' in context:
                    sources.append({'block_id': record['block_id'], 'start': 0, 'end': len(record['text'])})
                if '表格说明：' in context:
                    sources.extend({'block_id': extra['block_id'], 'start': 0, 'end': len(extra['text'])} for extra in context_blocks)
                text = context + block['text'][offset:lo]
                chunks.append({'kind': block['kind'], 'text': text, 'token_count': counter.count(text), 'sources': sources})
                offset = lo
    return chunks


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
