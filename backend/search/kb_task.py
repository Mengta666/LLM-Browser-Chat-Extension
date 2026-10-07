"""问题需求与原文交付分离；模型的充分性判断不等同于服务端核实。"""

import copy
import hashlib
import json

from rag.document_config import TASK_CITATION_LIMIT


class CitationArgumentError(ValueError):
    def __init__(self, code, message, *, need_id=None):
        super().__init__(message)
        self.code = code
        self.need_id = need_id

NAME = 'kb_update_task'
TOOL = {'type': 'function', 'function': {
    'name': NAME,
    'description': (
        '记录知识库问题需求和回答要点；本工具不检索、不读取正文、不代替最终回答。\n'
        '首次创建：仅依据用户问题拆分最少必要的独立回答需求，填写 question、evidence_kind、status="open"、missing、next_action；'
        '省略 id，由服务端分配 N编号。不要扩大任务、依据搜索结果缩减问题或把查找/读取动作单独建成回答需求。\n'
        'evidence_kind="structure" 仅用于标题、章节层级、页码位置；涉及具体内容、参数、证明、概括正文或混合问题用 "content"。创建后不能更改此类型来绕过正文要求。\n'
        '后续更新：使用已有 id，省略 question，每次显式填写 answer_points 字符串；尚无有依据的要点时填 ""，不可标为 ready。原问题由服务端保留且不能改写。'
        '某项已有足够依据就及时记录，不必等所有需求完成，也无需机械通读无关材料。\n'
        '标为 ready 时必须同时填写：id、status="ready"、missing=""、next_action="answer"、非空 answer_points，'
        '并有支持这些要点的已取得来源；content 必须有正文 citations，structure 可用正文或已返回的 outline_refs。answer_points 是 JSON 参数键，必须实际写进 needs 的对应对象，'
        '不能只在普通回复中说明。写出具体结论及来源对应关系；“已读完/资料齐全/可以回答”不是回答要点。'
        '保留条件、否定和文档归属，不写隐藏思考，不把你的概括冒充原文。ready 仅表示你的充分性判断，不是事实核验。\n'
        'citations 可省略，服务端会使用经 need_ids 关联到该需求的来源；省略不等于填写空数组 []。'
        '同一已读来源可支持多个需求：显式填写本请求已取得的 citations 即可复用其他需求的来源，不必重复读取。'
        '仅在 answer_points 文字中写 [N] 不会建立关联；当前需求尚无关联时不能省略 citations。'
        '显式填写时只选支持要点的已有整数来源编号，不抄全部编号、不编造来源。目录 O编号只填 outline_refs，不混入 citations。'
        '结构需求可省略 outline_refs 继承经 need_ids 关联的目录。目录仅支持实际返回的标题/层级/位置，不能支持未读正文；分页未到末尾时不要声称已列全。\n'
        '材料不足且还可补读：保留 open，missing 写具体缺项并选择 search/continue_reading/related。'
        '无法补齐：标为 unresolved，missing 如实写明缺项，next_action="answer"；不要仅为跳过参数错误假装 ready。\n'
        '收到参数错误后，按错误补齐或修正同一需求对象；尤其缺 answer_points 时必须新增该字段和具体要点，'
        '不要原样重发仅含 ready 的调用。\n'
        '以下为虚构参数示例，实际调用必须替换库号、需求号、内容及来源号，不得把示例当成检索证据。\n'
        '创建示例：{"kb_id":"kb_example","needs":[{"question":"设备工作温度及限制是什么？","evidence_kind":"content","status":"open","missing":"尚未取得相关正文","next_action":"search"}]}\n'
        'ready 示例（假设已取得来源 1）：{"kb_id":"kb_example","needs":[{"id":"N1","status":"ready","missing":"","next_action":"answer","answer_points":"来源 1：设备工作温度为 23.5 °C，禁止带电维护。","citations":[1]}]}\n'
        'unresolved 示例：{"kb_id":"kb_example","needs":[{"id":"N1","status":"unresolved","missing":"尚未取得说明维护限制的正文，不能确认限制","next_action":"answer","answer_points":""}]}'
    ),
    'parameters': {'type': 'object', 'properties': {
        'kb_id': {'type': 'string'},
        'needs': {'type': 'array', 'minItems': 1, 'maxItems': 12, 'items': {
            'type': 'object', 'properties': {
                'id': {'type': 'string', 'description': '首次省略；更新必须使用已有 N编号'},
                'question': {'type': 'string', 'minLength': 1, 'maxLength': 500,
                             'description': '首次创建必填；已有 id 的更新应省略，如携带必须与原问题逐字一致'},
                'evidence_kind': {'type':'string','enum':['content','structure'],
                    'description':'首次创建选择：仅标题、层级、页码用 structure；正文内容、数值、证明或混合需求用 content。更新省略，不可更改。'},
                'outline_refs': {'type':'array','maxItems':TASK_CITATION_LIMIT,'items':{'type':'string','pattern':'^O[1-9][0-9]*$'},
                    'description':'可省略以继承该需求已关联目录。显式填写本请求已返回的 O编号，如 ["O1"]，可跨需求复用；只支持结构信息，不能代替正文 citations。'},
                'status': {'type': 'string', 'enum': ['open', 'ready', 'unresolved'],
                    'description': 'open：待补证据；ready：已有适合该需求类型的来源且提供非空 answer_points、missing=""；unresolved：无法补齐，missing 如实写缺项。目录不能满足 content，已读完不自动等于 ready。'},
                'citations': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': TASK_CITATION_LIMIT,
                    'description': '可省略：继承通过 need_ids 已关联到该需求的来源；不要用 [] 表示继承。显式填写为已有整数编号数组，如 [1,3]，仅选支持要点的代表来源，不抄全部已读编号。'},
                'missing': {'type': 'string', 'maxLength': 500,
                    'description': '必填。ready 必须为 ""；open/unresolved 写尚缺的具体事实或依据，不能用“已读完”替代 answer_points。'},
                'answer_points': {'type': 'string', 'maxLength': 1500,
                    'description': '更新已有 id 时必填字符串。status="ready" 时必须显式传入非空字符串：具体回答要点及其已有来源对应关系，不能省略、留空或仅写“已读完/可以回答”。open/unresolved 尚无要点时填 ""；若已知部分结论则填写有依据的部分。首次创建可省略。保留条件、否定和文档归属，不写隐藏思考。'},
                'next_action': {'type': 'string', 'enum': ['search', 'continue_reading', 'related', 'answer'],
                    'description': '下一步建议，不执行动作。仍需材料选 search/continue_reading/related；已可回答或无法再补齐选 answer。选择 answer 不能替代 ready 所需的 answer_points。'}},
            'required': ['question', 'evidence_kind', 'status', 'missing', 'next_action'], 'additionalProperties': False}}},
        'required': ['kb_id', 'needs'], 'additionalProperties': False}}}

NEED_IDS = {'type': 'array', 'minItems': 1, 'maxItems': 12,
            'items': {'type': 'string', 'pattern': '^N[1-9][0-9]*$'},
            'description': '本次读取/查询服务的任务需求编号，可关联多个；不是来源编号'}


def parse(arguments):
    args = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(args, dict) or set(args) != {'kb_id', 'needs'}:
        raise ValueError('需要 kb_id 和 needs')
    if not isinstance(args['kb_id'], str) or not args['kb_id'].strip():
        raise ValueError('kb_id 必须非空')
    needs = args['needs']
    if not isinstance(needs, list) or not 1 <= len(needs) <= 12:
        raise ValueError('needs 必须有 1～12 项；仅拆分当前问题')
    allowed = set(TOOL['function']['parameters']['properties']['needs']['items']['properties'])
    for need in needs:
        if not isinstance(need, dict) or set(need) - allowed:
            raise ValueError('需求字段不正确')
        keys = ('question', 'missing') if 'question' in need or not need.get('id') else ('missing',)
        for key in keys:
            if not isinstance(need.get(key), str) or len(need[key]) > 500:
                raise ValueError(f'{key} 必须是至多 500 字符的文本')
        if ('question' in need and not need['question'].strip()) or need.get('status') not in ('open', 'ready', 'unresolved'):
            raise ValueError('需求或状态不正确')
        if need.get('next_action') not in ('search', 'continue_reading', 'related', 'answer'):
            raise ValueError('next_action 不正确')
        if not isinstance(need.get('answer_points', ''), str) or len(need.get('answer_points', '')) > 1500:
            raise ValueError('answer_points 必须为至多 1500 字符的回答要点')
        if need.get('evidence_kind', 'content') not in ('content','structure'):
            raise ValueError('evidence_kind 必须为 content/structure')
        outline_refs = need.get('outline_refs', [])
        if (not isinstance(outline_refs,list) or len(outline_refs)>TASK_CITATION_LIMIT or
                any(not isinstance(ref,str) or not ref.startswith('O') or not ref[1:].isdigit() or int(ref[1:])<1 for ref in outline_refs)):
            raise CitationArgumentError('kb_citations_outline_type', 'outline_refs 必须为已有 O编号数组，例如 ["O1"]')
        refs = need.get('citations', [])
        if not isinstance(refs, list) or any(type(n) is not int or n < 1 for n in refs):
            raise CitationArgumentError('kb_citations_type', 'citations 必须为正整数数组，例如 [1, 3]，不能使用字符串或布尔值')
        if len(refs) > TASK_CITATION_LIMIT:
            raise CitationArgumentError('kb_citations_limit',
                f'citations 有 {len(refs)} 项，单次最多 {TASK_CITATION_LIMIT} 项；请选择支持要点的代表编号，或省略 citations 使用该需求已关联的来源；已读正文不会丢失')
        if 'id' in need and (not isinstance(need['id'], str) or not need['id'].startswith('N') or not need['id'][1:].isdigit()):
            raise ValueError('需求编号不正确')
    return args


def update(state, args):
    args = parse(args)
    needs = copy.deepcopy(state.get('needs', []))
    known = {n['id']: n for n in needs}
    evidence = state.get('evidence', {})
    touched = set()
    initial = not needs
    for item in args['needs']:
        nid = item.get('id')
        if initial:
            if not item.get('question'):
                raise ValueError('首次创建需求必须提供 question')
            expected = f'N{len(needs) + 1}'
            if nid and nid != expected:
                raise ValueError('首次需求编号按顺序由服务端分配，不能跳号或重复')
            nid = expected
            need = {'id': nid, 'question': item['question'], 'citations': [], 'evidence_kind':item.get('evidence_kind','content')}
            needs.append(need)
        else:
            if nid not in known or nid in touched:
                raise ValueError('未知或重复的需求编号')
            need = known[nid]
            if 'question' in item and item['question'] != need['question']:
                raise ValueError('不能通过更新改变原需求；只更新状态和缺项')
            if 'evidence_kind' in item and item['evidence_kind'] != need.get('evidence_kind','content'):
                raise ValueError('不能通过更新更改 evidence_kind 来放宽原需求的证据类型')
        touched.add(nid)
        linked = linked_citations(state, need)
        refs = list(dict.fromkeys(item.get('citations', linked)))
        unknown = [ref for ref in refs if str(ref) not in evidence]
        if unknown:
            raise CitationArgumentError('kb_citations_unknown', f'不能引用尚未取得的正文来源：{unknown}；请使用该需求 delivered_citations 中的编号')
        outlines = list(dict.fromkeys(item.get('outline_refs', linked_outlines(state, need))))
        if any(ref not in state.get('outline_sources', {}) for ref in outlines):
            raise CitationArgumentError('kb_citations_outline_unknown', 'outline_refs 包含本请求尚未取得的目录编号')
        if item['status'] == 'ready' and not refs and not (need.get('evidence_kind')=='structure' and outlines):
            if need.get('evidence_kind')=='structure' and state.get('outline_sources'):
                raise CitationArgumentError('kb_citations_unlinked',
                    f'{nid}: 结构需求尚未选择来源；本请求已有目录，请显式填写支持该结构要点的 outline_refs，或使用已有正文 citations，无需仅为关联而重读。', need_id=nid)
            if 'citations' in item:
                raise CitationArgumentError('kb_citations_empty', f'{nid}: ready 需要已取得来源；citations=[] 表示未选择来源。请填写支持要点的编号，或省略该字段继承该需求已关联的来源。')
            if evidence:
                raise CitationArgumentError('kb_citations_unlinked',
                    f'{nid}: ready 需要已取得来源；本请求已有正文，但本次没有为该需求选择来源。请显式填写支持要点的 citations，复用其他需求的来源无需重读；文字中的 [N] 不建立关联。', need_id=nid)
            raise CitationArgumentError('kb_citations_missing',
                f'{nid}: ready 需要已取得来源；本请求尚无正文来源。请读取相关正文并关联 need_ids，或保留 open/unresolved；不要编造引用。', need_id=nid)
        if item['status'] == 'ready' and item['missing'].strip():
            raise ValueError(f'{nid}: ready 的 missing 必须为空；若仍有实质缺项，请保留 open/unresolved 并写明缺项')
        if item['status'] == 'ready' and not item.get('answer_points', '').strip():
            raise ValueError('ready 需要 answer_points：写出与来源对应的简短回答要点，不要只声明已读完')
        # 自动关联由 evidence.need_ids 提供，不把全部已读来源误当模型显式选定的必保引用。
        need.update(status=item['status'], citations=refs if 'citations' in item else need.get('citations', []), missing=item['missing'], next_action=item['next_action'],
                    outline_refs=outlines if 'outline_refs' in item else need.get('outline_refs', []),
                    answer_points=item.get('answer_points', ''), answer_points_kind='model_notes_not_original')
    state['needs'] = needs
    assessed = state.setdefault('assessed_need_ids', [])
    for need in needs:
        if need['status'] in ('ready', 'unresolved') and need['id'] not in assessed:
            assessed.append(need['id'])
    return {'outcome': 'task_updated', 'needs': public(state),
            'notice': '需求与用户原问题均保留；ready 是模型判断，来源取得、当前可见和结论正确是不同状态。'}


def linked_citations(state, need):
    evidence = state.get('evidence', {})
    return list(dict.fromkeys([*[ref for ref in need.get('citations', []) if str(ref) in evidence],
        *[int(ref) for ref, row in evidence.items() if need['id'] in row.get('need_ids', [])]]))


def linked_outlines(state, need):
    sources = state.get('outline_sources', {})
    return list(dict.fromkeys([*[ref for ref in need.get('outline_refs', []) if ref in sources],
        *[ref for ref,row in sources.items() if need['id'] in row.get('need_ids', [])]]))


def outline_reference(state, outline):
    payload = {k:v for k,v in outline.items() if k!='source_ref'}
    for ref, row in state.get('outline_sources', {}).items():
        if row['outline'] == payload:
            return ref
    return f"O{len(state.get('outline_sources', {}))+1}"


def record_outline(state, kb_id, outline, ids):
    ref = outline['source_ref']
    source = state.setdefault('outline_sources', {}).setdefault(ref, {
        'kb_id':kb_id, 'kind':'structure', 'outline':{k:v for k,v in outline.items() if k!='source_ref'}, 'need_ids':[]})
    source['need_ids'] = list(dict.fromkeys([*source['need_ids'], *ids]))
    reconcile_errors(state)


def reconcile_errors(state):
    needs = {n['id']: n for n in state.get('needs', [])}
    state['pending'] = [p for p in state.get('pending', []) if not (
        p.get('tool') == NAME and p.get('error_code') in ('kb_citations_unlinked', 'kb_citations_missing')
        and p.get('need_id') in needs and (linked_citations(state, needs[p['need_id']])
            or needs[p['need_id']].get('evidence_kind')=='structure' and linked_outlines(state, needs[p['need_id']])))]


def progress_snapshot(state):
    from search.kb_coverage import covered_chars, missing
    # 按需求和原文范围累计，而不是按临时引用编号计数；重编号和状态来回切换不能刷进展。
    linked_ranges = state.setdefault('association_ranges', [])
    unlocated = state.setdefault('association_unlocated', [])
    for need in state.get('needs', []):
        for ref in linked_outlines(state, need):
            outline = state['outline_sources'][ref]['outline']
            for block in outline.get('blocks', []):
                identity = [need['id'], outline['doc_id'], outline['parse_id'], outline.get('structure_id'),
                            block.get('node_id') or block.get('block_id') or block.get('section_id'), 'structure']
                if identity not in unlocated:
                    unlocated.append(identity)
        for ref in linked_citations(state, need):
            row = state['evidence'][str(ref)]
            for span in row.get('locations', []):
                location = [json.dumps([need['id'], row.get('doc_id')]), row.get('parse_id'),
                            span['block_id'], span['start'], span['end']]
                linked_ranges.extend(missing(location, linked_ranges))
            if not row.get('locations'):
                identity = [need['id'], row.get('doc_id'), row.get('parse_id'), row.get('legacy_key', str(ref))]
                if identity not in unlocated:
                    unlocated.append(identity)
    return (covered_chars(state.get('ranges', [])), len(state.get('navigation', [])),
            len(state.get('assessed_need_ids', [])), covered_chars(linked_ranges) + len(unlocated))


def record_progress(state, before, *, argument_errors=False):
    progressed = any(new > old for old, new in zip(before, progress_snapshot(state)))
    state['no_progress_rounds'] = 0 if progressed else state.get('no_progress_rounds', 0) + 1
    state['last_stall_reason'] = 'tool_arguments_not_repaired' if argument_errors and not progressed else 'no_progress'


def validate_ids(state, args):
    ids = args.get('need_ids', [])
    if not isinstance(ids, list) or len(ids) > 12 or any(not isinstance(n, str) for n in ids):
        raise ValueError('need_ids 必须为 N编号数组')
    known = {n['id'] for n in state.get('needs', [])}
    if set(ids) - known:
        raise ValueError('need_ids 包含未知需求')
    return (ids or list(known)) if len(known) == 1 else ids


def delivered(state, rows, ids):
    archive = state.setdefault('evidence', {})
    for row in rows:
        citation = row['citation_index']
        entry = archive.setdefault(str(citation), {'doc_id': row.get('doc_id'), 'parse_id': row.get('parse_id'),
            'locations': row.get('locations', []), 'need_ids': [], 'read_kind': row.get('read_kind', 'preview')})
        entry.update({k: v for k, v in {'kb_id': row.get('kb_id'),
                     'document': row.get('source') or row.get('document')}.items() if v is not None})
        if not entry['locations']:
            entry['legacy_key'] = str(row.get('chunk_id') or hashlib.sha256(row.get('content', '').encode('utf-8')).hexdigest())
        for key in ('structure_id', 'index_run_id'):
            versions = entry.setdefault(key + 's', [])
            if row.get(key) and row[key] not in versions:
                versions.append(row[key])
        if row.get('read_kind') == 'read':
            entry['read_kind'] = 'read'
        entry['need_ids'] = list(dict.fromkeys([*entry['need_ids'], *ids]))
    reconcile_errors(state)


def public(state, visible=None, visible_outlines=None):
    if visible is None and 'visible_citations' in state:
        visible = set(state['visible_citations'])
    evidence = state.get('evidence', {})
    if visible_outlines is None:
        visible_outlines = set(state.get('visible_outline_refs', state.get('outline_sources', {})))
    result = []
    for need in state.get('needs', []):
        citations = linked_citations(state, need)
        aliases = []
        if visible is not None:
            from search.kb_coverage import missing
            for ref in citations:
                source = evidence.get(str(ref), {})
                if ref in visible or not source.get('locations'):
                    continue
                spans = [[source.get('doc_id'),source.get('parse_id'),s['block_id'],s['start'],s['end']] for s in source['locations']]
                for replacement in sorted(visible):
                    candidate = evidence.get(str(replacement), {})
                    ranges = [[candidate.get('doc_id'),candidate.get('parse_id'),s['block_id'],s['start'],s['end']]
                              for s in candidate.get('locations', [])]
                    if ranges and all(not missing(s,ranges) for s in spans):
                        aliases.append({'from':ref,'to':replacement})
                        break
        replaced = {a['from'] for a in aliases}
        groups = {}
        for ref in citations:
            source = evidence.get(str(ref), {})
            identity = source.get('doc_id') or ('unlocated', ref), source.get('parse_id')
            group = groups.setdefault(identity, {'kb_id': source.get('kb_id'), 'doc_id': source.get('doc_id'),
                'parse_id': source.get('parse_id'), 'document': source.get('document'), 'citations': []})
            group['citations'].append(ref)
        if visible is not None:
            replacements = {a['from']:a['to'] for a in aliases}
            for group in groups.values():
                group['visible_citations'] = list(dict.fromkeys(replacements.get(ref, ref)
                    for ref in group['citations'] if ref in visible or ref in replaced))
                group['omitted_citations'] = [ref for ref in group['citations'] if ref not in visible and ref not in replaced]
        result.append({**need, 'delivered_citations': citations,
            'delivered_outline_refs': linked_outlines(state, need),
            'visible_outline_refs': [ref for ref in linked_outlines(state, need) if ref in visible_outlines],
            'outline_source_groups': [{'source_ref':ref, 'kind':'structure', **{k:v for k,v in state['outline_sources'][ref]['outline'].items()
                if k in ('doc_id','filename','parse_id','structure_id','index_run_id','view','offset','next_offset','total_blocks')}}
                for ref in linked_outlines(state, need)],
            'source_groups': list(groups.values()),
            **({'visible_citations': list(dict.fromkeys([*[ref for ref in citations if ref in visible], *[a['to'] for a in aliases]])),
                'omitted_citations': [ref for ref in citations if ref not in visible and ref not in replaced],
                'covered_by_visible_source': aliases} if visible is not None else {})})
    return result


def visible_outline_refs(messages):
    refs = set()
    for message in messages:
        text = message.get('content', '')
        if not isinstance(text,str):
            continue
        if text.startswith('[本轮已取得的证据；不是新的用户要求]\n'):
            text = text.split('\n',1)[1]
        elif message['role']!='tool':
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if not isinstance(payload,dict):
            continue
        if (payload.get('outline') or {}).get('source_ref'):
            refs.add(payload['outline']['source_ref'])
        for entry in payload.get('evidence', []):
            if isinstance(entry,dict) and isinstance(entry.get('result'),str):
                refs.update(visible_outline_refs([{'role':'tool','content':entry['result']}]))
    return sorted(refs)


def visible_citations(messages):
    refs = set()
    for message in messages:
        text = message.get('content', '')
        if not isinstance(text, str):
            continue
        if text.startswith('[本轮已取得的证据；不是新的用户要求]\n'):
            text = text.split('\n', 1)[1]
        elif message['role'] != 'tool':
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        groups = [payload, *payload.get('kb_documents', [])]
        for group in groups:
            for row in group.get('evidence', []):
                if isinstance(row, dict) and type(row.get('citation')) is int and row.get('content'):
                    refs.add(row['citation'])
    return sorted(refs)
