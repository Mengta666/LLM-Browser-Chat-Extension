"""从原 PDF 提取结构辅助信息，不替换 OCR 正文。"""

import re
import statistics
import unicodedata
from collections import defaultdict

import fitz


def normalized(text):
    """统一字符形式并移除非文字标记，供 OCR 文本与 PDF 元素对齐。"""
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', text)).casefold()


def pdf_features(pdf, pages):
    """从 PDF 提取字体、位置和书签信息，作为 OCR 结构推断的辅助依据。"""
    dimensions = {p['page']: (p['width'], p['height']) for p in pages}
    result = {}
    for number, page in enumerate(pdf, 1):
        if number not in dimensions:
            continue
        width, height = dimensions[number]
        sx, sy = width / page.rect.width, height / page.rect.height
        spans = []
        for group in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
            for line in group.get('lines', []):
                for span in line.get('spans', []):
                    rect = fitz.Rect(span['bbox']) * page.rotation_matrix
                    if normalized(span['text']):
                        spans.append({'text': span['text'], 'bbox': [rect.x0*sx,rect.y0*sy,rect.x1*sx,rect.y1*sy],
                                      'size': span['size'], 'bold': bool(span['flags'] & 16), 'italic': bool(span['flags'] & 2)})
        result[number] = {'spans': spans, 'scale': sy, 'width': width, 'height': height}
    bookmarks = []
    for level, title, number, destination in pdf.get_toc(simple=False):
        mark = [level, title, number]
        if 0 < number <= len(pdf) and destination.get('to') is not None:
            point = destination['to']
            page = pdf[number-1]
            mark.append({'x': point.x/page.rect.width, 'y': point.y/page.rect.height})
        bookmarks.append(mark)
    return result, bookmarks


def enrich(blocks, features):
    """将匹配到的 PDF 排版特征附加到解析块，不替换已保存的 OCR 正文。"""
    for block in blocks:
        page = features.get(block['page'])
        box = block.get('bbox')
        if not page or not box:
            continue
        style = {'height': (box[3]-box[1])/page['scale']/max(1,block['text'].count('\n')+1),
                 'x': box[0]/page['width'], 'y': box[1]/page['height'],
                 'width': (box[2]-box[0])/page['width'], 'source': 'ocr_geometry'}
        text = normalized(block['text'])
        matched = []
        for span in page['spans']:
            a = span['bbox']
            area = max(0,min(a[2],box[2])-max(a[0],box[0]))*max(0,min(a[3],box[3])-max(a[1],box[1]))
            fragment = normalized(span['text'])
            if fragment and fragment in text and area >= .6*max(1,(a[2]-a[0])*(a[3]-a[1])):
                matched.append(span)
        chars = sum(len(normalized(s['text'])) for s in matched)
        if matched and chars >= .6*max(1,len(text)):
            style.update(size=statistics.median(s['size'] for s in matched),
                         bold=sum(len(normalized(s['text'])) for s in matched if s['bold']) >= .6*chars,
                         italic=sum(len(normalized(s['text'])) for s in matched if s['italic']) >= .6*chars,
                         source='native_text_aligned')
        block['structure']['heading_features'] = style
    return blocks


def reading_order(blocks, pages):
    """结合页面位置和块结构推断阅读顺序，返回重排后的文档块。"""
    from rag.document_layout import MARGINS
    by_page = defaultdict(list)
    for block in blocks:
        by_page[block['page']].append(block)
    dimensions = {p['page']: (p['width'],p['height']) for p in pages}
    output = []
    for number, items in sorted(by_page.items()):
        width, height = dimensions[number]
        body = [b for b in items if b.get('bbox') and b['kind'] not in MARGINS and not b['structure'].get('index_excluded')]
        narrow = [b for b in body if b['kind'] in ('text','title','paragraph_title','section_title','list')
                  and b['bbox'][2]-b['bbox'][0] < .60*width]
        cut = None
        for left_edge in sorted({b['bbox'][2] for b in narrow}):
            # 分隔候选取自实际空白，不把跨栏标题或表格强行塞入某一栏。
            right_start = min((b['bbox'][0] for b in narrow if b['bbox'][0] > left_edge), default=width)
            if not .04*width <= right_start-left_edge <= .4*width:
                continue
            candidate = (left_edge+right_start)/2
            if not .25*width < candidate < .75*width:
                continue
            left = [b for b in narrow if b['bbox'][2] < candidate]
            right = [b for b in narrow if b['bbox'][0] > candidate]
            if len(left) < 3 or len(right) < 3:
                continue
            low = max(min(b['bbox'][1] for b in left),min(b['bbox'][1] for b in right))
            high = min(max(b['bbox'][3] for b in left),max(b['bbox'][3] for b in right))
            crossing = [b for b in narrow if b['bbox'][0] <= candidate <= b['bbox'][2]]
            if any(b['bbox'][1] < high and b['bbox'][3] > low for b in crossing):
                continue
            if high-low > .10*height:
                cut = candidate
                break
        if cut is not None:
            spanning = sorted([b for b in items if not b.get('bbox') or b['bbox'][0] < cut < b['bbox'][2] or b['kind'] in MARGINS],
                              key=lambda b: (b['bbox'][1] if b.get('bbox') else -1,b['ordinal']))
            remaining = [b for b in items if b not in spanning]
            ordered = []
            for boundary in [*spanning,None]:
                stop = boundary['bbox'][1] if boundary and boundary.get('bbox') else -1 if boundary else float('inf')
                band = [b for b in remaining if b['bbox'][1] < stop]
                ordered.extend(sorted(band,key=lambda b:(b['bbox'][0] >= cut,b['bbox'][1],b['ordinal'])))
                remaining = [b for b in remaining if b not in band]
                if boundary:
                    ordered.append(boundary)
            items, basis = ordered, 'two_column_gutter'
        else:
            ranks = [b['structure'].get('reading_order') for b in items]
            reliable = all(type(v) is int and v >= 0 for v in ranks) and len(set(ranks)) == len(ranks)
            items = sorted(items,key=lambda b:b['structure']['reading_order']) if reliable else items
            basis = 'upstream_unique_order' if reliable else 'parser_order'
        for ordinal, block in enumerate(items):
            block['structure']['source_ordinal'] = block['ordinal']
            block['structure']['order_basis'] = basis
            block['structure']['logical_order'] = len(output)
            block['ordinal'] = ordinal
            output.append(block)
    return output
