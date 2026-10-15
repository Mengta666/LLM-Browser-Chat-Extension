"""把已完成的结构建议投影到不可变页块范围，不改写 OCR 正文。"""
from copy import deepcopy

VERSION = 1


def contains(sources, atom):
    return any(s['block_id'] == atom['block']['block_id'] and s['start'] <= atom['start'] < atom['end'] <= s['end']
               for s in sources)


def boundaries(review):
    result = {}
    for sources in [review['unassigned_sources'], *[n['sources'] for n in review['nodes']],
                    *[n['heading_sources'] for n in review['nodes']], *[m['sources'] for m in review['materials']]]:
        for s in sources:
            result.setdefault(s['block_id'], set()).update((s['start'], s['end']))
    return result


def merged(sources):
    result = []
    for s in sources:
        if result and result[-1]['block_id'] == s['block_id'] and result[-1]['end'] == s['start']:
            result[-1]['end'] = s['end']
        else:
            result.append({k: s[k] for k in ('block_id', 'start', 'end')})
    return result


def validate_review(review, blocks):
    from rag.structure_review_protocol import VERSION as protocol_version
    if review.get('schema_version') != protocol_version or review.get('body_ownership_version') != 2:
        raise ValueError('publication_review_version_unsupported')
    known = {b['block_id']: b for b in blocks}
    partitions = [review['unassigned_sources'], *[n['direct_sources'] for n in review['nodes']],
                  *[m['sources'] for m in review['materials'] if m['ownership'] == 'exclusive']]
    coverage = {bid: [] for bid in known}
    for sources in partitions:
        for s in sources:
            if s['block_id'] not in known or not 0 <= s['start'] < s['end'] <= len(known[s['block_id']]['text']):
                raise ValueError('publication_invalid_source')
            coverage[s['block_id']].append((s['start'], s['end']))
    for bid, spans in coverage.items():
        end = 0
        for start, stop in sorted(spans):
            if start != end:
                raise ValueError('publication_source_gap_or_overlap')
            end = stop
        if end != len(known[bid]['text']):
            raise ValueError('publication_source_not_covered')
    paths = {}
    for n in review['nodes']:
        if n['parent_anchor'] and n['parent_anchor'] not in paths:
            raise ValueError('publication_invalid_parent')
        paths[n['heading_anchor']] = [*paths.get(n['parent_anchor'], []), n['heading_anchor']]
    order = {b['block_id']: i for i, b in enumerate(blocks)}
    for n in review['nodes']:
        expected = [s for child in review['nodes'] if n['heading_anchor'] in paths[child['heading_anchor']]
                    for s in child['direct_sources']]
        expected.sort(key=lambda s: (order[s['block_id']], s['start']))
        if merged(expected) != merged(n['sources']):
            raise ValueError('publication_section_coverage_mismatch')
        for s in n['heading_sources']:
            if not any(p['block_id'] == s['block_id'] and p['start'] <= s['start'] < s['end'] <= p['end']
                       for p in n['direct_sources']):
                raise ValueError('publication_heading_outside_section')


def build(blocks, review, *, title, check=lambda: None):
    from rag import document_hierarchy as H, document_config as C
    validate_review(review, blocks)
    nodes, relations, quality = H.build(blocks, title=title, review=review, max_chars=C.UNIT_MAX_CHARS,
        preview_chars=C.NODE_PREVIEW_CHARS, proof_passage_chars=C.PROOF_PASSAGE_CHARS, check=check)
    projected = deepcopy(blocks)
    for block in projected:
        structure = block['structure']
        for key in ('section_heading', 'section_path', 'index_excluded', 'hierarchy_version'):
            structure.pop(key, None)
        ranges = []
        for node in nodes:
            if node['kind'] != 'section' and node['basis'] != 'model_review':
                continue
            for s in node['sources']:
                if s['block_id'] == block['block_id']:
                    ranges.append({'start': s['start'], 'end': s['end'], 'node_id': node['node_id'],
                                   'role': node['attributes']['role'], 'status': node['attributes']['review_status']})
        structure.update(review_range_version=VERSION, section_ranges=ranges)
        block['section_id'] = ''
        block['parent_id'] = ''
    quality.update(review_range_version=VERSION, sections=len(review['nodes']),
                   unresolved_hierarchy=sum(n['status'] == 'unresolved' for n in review['nodes']),
                   material_roles={r: sum(m['role'] == r for m in review['materials']) for r in {m['role'] for m in review['materials']}},
                   semantic_verified=False)
    return projected, nodes, relations, quality


def chunks(blocks, review, nodes, filename, counter):
    from rag.document_structure import build_chunks
    known = {b['block_id']: b for b in blocks}
    sections = {n['attributes']['heading_anchor']: n for n in nodes if n['kind'] == 'section'}
    groups = [(n['direct_sources'], sections[n['heading_anchor']]) for n in review['nodes']]
    groups += [(review['unassigned_sources'], nodes[0])]
    groups += [(m['sources'], next(n for n in nodes if n['basis'] == 'model_review' and n['kind'] == m['role']
                                 and n['sources'] == m['sources'])) for m in review['materials']
               if m['ownership'] == 'exclusive' and m['role'] not in ('header', 'footer')]
    result = []
    for sources, owner in groups:
        views, mapping = [], {}
        for ordinal, s in enumerate(merged(sources)):
            b = deepcopy(known[s['block_id']])
            vid = f"{s['block_id']}@{s['start']}:{s['end']}"
            mapping[vid] = s
            b.update(block_id=vid, text=b['text'][s['start']:s['end']], ordinal=ordinal, section_id='', parent_id='')
            structure = b['structure']
            # 旧规则的块级关系不能跨越模型确认的字符分区。
            for key in ('section_heading', 'section_path', 'index_excluded', 'paragraph_id', 'formula_members',
                        'context_blocks', 'related_blocks', 'hierarchy_version'):
                structure.pop(key, None)
            structure['node_id'] = owner['node_id']
            if b['kind'] in ('header', 'footer', 'page_number', 'number', 'doc_title'):
                b['kind'] = 'text'
            if s['start'] or s['end'] != len(known[s['block_id']]['text']):
                # 局部表格不能伪造完整行号或跨分区表头；仍按原字符范围索引。
                for key in ('row_ranges', 'header_rows', 'cells'):
                    structure.pop(key, None)
            views.append(b)
        for b in views:
            original = known[mapping[b['block_id']]['block_id']]['structure']
            for key in ('formula_members', 'context_blocks'):
                if original.get(key):
                    b['structure'][key] = [vid for vid, s in mapping.items() if s['block_id'] in original[key]]
        for chunk in build_chunks(views, filename + ' / ' + owner['title'][:240], counter):
            converted = []
            for s in chunk['sources']:
                origin = mapping[s['block_id']]
                converted.append({**s, 'block_id': origin['block_id'], 'start': origin['start']+s['start'],
                                  'end': origin['start']+s['end']})
            chunk['sources'] = converted
            result.append(chunk)
    return result
