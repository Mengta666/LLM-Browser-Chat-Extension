"""任务内证据、可复用成果与执行绑定；页面失败不撤回已取得的资料。"""

import copy
import json
import re


class ArtifactError(ValueError):
    def __init__(self, code, message, *, hint, **details):
        super().__init__(message)
        self.feedback = {'code': code, 'error': message, 'hint': hint, **details}


def _resolve_part(sources, part, position):
    hint = '复用已有来源，修正此片段；整段只传 citation，局部传 quote，重复引文需明确 offset/end。'
    if not isinstance(part, dict) or set(part) - {'citation', 'quote', 'offset', 'end'}:
        raise ArtifactError('invalid_part', '来源片段参数无效', hint=hint, part=position)
    citation = part.get('citation')
    if type(citation) is not int or citation not in sources:
        raise ArtifactError('source_unavailable', '片段来源不存在或已失效',
                            hint='仅补取这个来源，不重复检索其他已取得资料。', part=position,
                            citation=citation if type(citation) is int else None)
    source = sources[citation]
    text = source['content']
    quote = part.get('quote')
    if 'quote' in part and (not isinstance(quote, str) or not quote):
        raise ArtifactError('invalid_quote', 'quote 必须为非空原文', hint=hint, part=position, citation=citation)
    if 'offset' in part or 'end' in part:
        start, end = part.get('offset'), part.get('end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ArtifactError('invalid_range', '片段范围必须满足 0 <= offset < end <= 来源长度',
                                hint=hint, part=position, citation=citation, source_length=len(text))
        if quote is not None and text[start:end] != quote:
            raise ArtifactError('quote_mismatch', '指定范围与引文不一致', hint=hint, part=position, citation=citation)
    elif quote is not None:
        matches = []
        start = text.find(quote)
        while start >= 0 and len(matches) < 9:
            matches.append([start, start + len(quote)])
            start = text.find(quote, start + 1)
        if not matches:
            raise ArtifactError('quote_mismatch', '引文不在此来源中', hint=hint, part=position, citation=citation)
        if len(matches) > 1:
            raise ArtifactError('ambiguous_quote', '同一来源中引文出现多次，未自动选择',
                                hint='按上下文选择候选范围，将其 offset/end 与 quote 一起提交；范围是此来源正文的 Python 字符范围。',
                                part=position, citation=citation, candidates=matches[:8], more_matches=len(matches) > 8)
        start, end = matches[0]
    else:
        start, end = 0, len(text)
    selected = text[start:end]
    if not selected.strip():
        raise ArtifactError('empty_part', '所选片段为空', hint=hint, part=position, citation=citation)
    if re.fullmatch(r'[A-Za-z0-9_.+%-]+', selected) and (
        start > 0 and re.match(r'[A-Za-z0-9_.]', text[start-1]) or
        end < len(text) and re.match(r'[A-Za-z0-9_.]', text[end])
    ):
        raise ArtifactError('partial_identifier', '片段截断了原始数字或标识符',
                            hint='选择完整数字/标识符，不能通过缩小范围改变原值。', part=position, citation=citation)
    return selected, {'citation': citation, 'source_range': [start, end],
                      'source_version': {k: source.get(k, '') for k in
                          ('kb_id', 'doc_id', 'parse_id', 'index_run_id', 'structure_id')},
                      'locations': copy.deepcopy(source.get('locations', []))}


def initial_state():
    return {'sources': [], 'budget': None, 'records': [], 'seen': {},
            'artifacts': {}, 'bindings': {}, 'receipts': [], 'next_artifact': 1,
            'next_citation': 1, 'invalidations': [], 'error': ''}


def save_artifact(session, *, kind, subject, purpose, content=None, citations=(), dependencies=(),
                  artifact_id=None, origin=None, missing=(), source_parts=None, separator=None,
                  field_ref=None, field_edits=None, length_repair_id=None, field_intent=None, page=None):
    state = session.task_state
    sources = {s['index']: s for s in state['sources']}
    if kind not in ('extraction', 'comparison', 'draft', 'answer'):
        raise ValueError('kind 必须为 extraction、comparison、draft 或 answer')
    if not all(isinstance(v, str) and v.strip() for v in (subject, purpose)):
        raise ValueError('成果需要明确对象和用途')
    segments = []
    field_basis = None
    if field_ref is not None or field_edits is not None:
        if content is not None or source_parts is not None or dependencies or origin not in (None, 'derived'):
            raise ValueError('字段局部修改使用 field_ref/field_edits，不同时提交 content/source_parts/dependencies')
        from agent.field_reads import compose
        content, dependencies, field_basis = compose(session, field_ref, field_edits)
        origin = 'derived'
    if source_parts is not None:
        if content is not None or dependencies or origin not in (None, 'verbatim', 'assembled'):
            raise ArtifactError('mixed_save_modes', '来源组合不能同时提交 content、dependencies 或整理稿类型',
                                hint='原文摘取/组合只传 source_parts；需要改写则改用 content/citations/origin=derived，不自动转换。')
        if not isinstance(source_parts, list) or not source_parts:
            raise ArtifactError('invalid_parts', 'source_parts 必须为非空片段数组', hint='按最终顺序提供 citation 片段。')
        if separator is None:
            separator = '\n\n'
        if isinstance(separator, str):
            separator = {'none': '', 'newline': '\n', 'blank_line': '\n\n'}.get(separator, separator)
        if separator not in ('', '\n', '\n\n'):
            raise ArtifactError('invalid_separator', '组合仅支持直接连接、换行或空行', hint='separator 使用 none（直接连接）、newline（换行）、blank_line（中间空一行）。新增说明或润色请使用 derived 整理稿。')
        pieces, offset = [], 0
        for position, part in enumerate(source_parts, 1):
            selected, segment = _resolve_part(sources, part, position)
            if pieces:
                offset += len(separator)
            segment['output_range'] = [offset, offset + len(selected)]
            segments.append(segment)
            pieces.append(selected)
            offset += len(selected)
        resolved = list(dict.fromkeys(p['citation'] for p in segments))
        if citations and list(citations) != resolved:
            raise ArtifactError('citation_mismatch', 'citations 与组合片段来源不一致', hint='组合方式省略 citations，由后端从 source_parts 自动生成。')
        citations = resolved
        content = separator.join(pieces)
        origin = 'verbatim' if len(pieces) == 1 else 'assembled'
    elif separator is not None or origin == 'assembled':
        raise ArtifactError('missing_parts', '组合保存必须提供 source_parts', hint='按顺序提供来源片段，不提交拼接后的 content。')
    origin = origin or 'derived'
    if not isinstance(content, str) or not content.strip():
        raise ValueError('成果需要非空正文或 source_parts')
    if origin not in ('derived', 'verbatim', 'assembled', 'user_input'):
        raise ValueError('成果来源类型无效')
    if not isinstance(missing, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in missing):
        raise ValueError('missing 必须为缺项字符串数组；没有缺项请省略或传 []，不能写“无”')
    missing = list(missing)
    if any(type(ref) is not int or ref not in sources for ref in citations):
        raise ValueError('成果引用必须来自本任务实际取得的正文')
    if len(set(dependencies)) != len(dependencies):
        raise ValueError('成果依赖不能重复')
    parents = []
    for ref in dependencies:
        parent = state['artifacts'].get(ref)
        if not parent or parent['status'] != 'available' or ref == artifact_id:
            raise ValueError(f'成果依赖 {ref} 不存在、已失效或形成自引用；重新取得来源后使用新 citations，不依赖失效成果')
        parents.append(parent)
    if origin == 'user_input' and content not in session.task:
        raise ArtifactError('user_input_mismatch', '用户输入成果必须是任务中的逐字原文',
            hint='先将任务里实际提供的相关资料逐字保存为 origin=user_input 的原成果（不带 field_intent）；'
                 '再以 dependencies=[原A编号]、origin=derived 保存整理后的待填稿并带 field_intent。'
                 '已有原成果直接复用，不重新检索，不用裸 text 绕过。')
    if origin != 'user_input' and not citations and not parents:
        raise ArtifactError('missing_artifact_source', '派生成果必须关联实际来源或已有成果',
            hint='知识库资料用实际 citations 或已有成果 dependencies。资料直接来自用户任务时，'
                 '先逐字保存 origin=user_input 的原成果，再以 dependencies=[原A编号]、origin=derived 保存待填稿。'
                 '不要把整理稿标为用户原文，不重新检索已有资料。')
    length_constraint = None
    if length_repair_id is not None:
        repair = state.get('field_length_repairs', {}).get(length_repair_id) if isinstance(length_repair_id, str) else None
        if not repair:
            raise ValueError('length_repair_id 不对应本任务的字段超限反馈')
        parent = state['artifacts'].get(repair['artifact_id'])
        if (origin != 'derived' or source_parts is not None or field_basis
                or repair['artifact_id'] not in dependencies or subject != repair['subject']
                or not parent or parent.get('field_basis') or missing):
            raise ValueError('限长稿须为同对象的完整 derived 正文，dependencies 引用原超长成果；不能改写受保护的局部字段')
        actual = len(content.encode('utf-16-le')) // 2
        maximum = repair['max_length']
        if actual > maximum:
            raise ArtifactError('field_value_too_long', f'精简稿仍超限：{actual} > {maximum}（UTF-16），未保存、未写入',
                hint='继续精简当前稿，保留原事实与必要信息；复用同一个 length_repair_id 和原成果，不重检索，不截断。',
                length_repair_id=length_repair_id, actual_length=actual, max_length=maximum,
                over_by=actual - maximum, length_unit='utf16')
        length_constraint = {'length_repair_id': length_repair_id, 'source_artifact_id': parent['id'],
            'max_length': maximum, 'actual_length': actual, 'length_unit': 'utf16'}
    if origin == 'verbatim' and source_parts is None:
        pattern = re.escape(content)
        if re.fullmatch(r'[A-Za-z0-9_.+%-]+', content):
            pattern = r'(?<![A-Za-z0-9_.])' + pattern + r'(?![A-Za-z0-9_.])'
        if not any(re.search(pattern, sources[r]['content']) for r in citations):
            raise ArtifactError('verbatim_mismatch', '逐字成果不在所引原文中，或截断了原始数字/标识符',
                                hint='多来源原文请改为 source_parts 按序组合，不再提交拼接全文；局部摘取传 citation/quote。若内容确实经过改写，请明确用 derived。已有资料仍可复用，不需要重新检索。',
                                citations=list(citations))
    if field_intent is None and length_constraint and parent.get('field_intent'):
        previous = parent['field_intent']
        field_intent = {k: previous[k] for k in ('index', 'scope', 'exclude')}
    previous = state['artifacts'].get(artifact_id) if artifact_id else None
    if previous and previous.get('field_basis') and not field_basis:
        if (content != previous['content'] or list(dependencies) != previous['dependencies']
                or list(citations) != previous['citations']):
            raise ValueError('更新局部修改成果必须继续使用 field_ref/field_edits；不能通过 artifact_id 丢弃原文保护范围')
        field_basis = copy.deepcopy(previous['field_basis'])
    if field_intent is not None:
        if not field_basis and any(p.get('field_basis') for p in parents):
            raise ValueError('不能把局部修改成果整体改写为待填稿；请修改替换片段后重新使用 field_ref/field_edits，保留未修改部分')
        from agent.field_content import prepare
        field_intent = prepare(session, content, field_intent, page, subject, field_basis)
    if getattr(session, 'cancel_event', None) is not None and session.cancel_event.is_set():
        raise ArtifactError('cancelled', '任务已停止，未保存成果', hint='停止后不自动恢复。')
    inherited = [r for p in parents for r in p['source_citations']]
    values = {'kind': kind, 'subject': subject, 'purpose': purpose, 'content': content,
              'citations': list(citations), 'source_citations': list(dict.fromkeys([*citations, *inherited])),
              'dependencies': list(dependencies), 'origin': origin, 'missing': missing,
              'segments': segments, 'separator': separator if source_parts is not None else None}
    if field_basis:
        values['field_basis'] = field_basis
    if length_constraint:
        values['length_constraint'] = length_constraint
    if field_intent:
        values['field_intent'] = field_intent
    if artifact_id:
        old = state['artifacts'].get(artifact_id)
        if not old:
            raise ValueError('成果编号不存在')
        # 已有成果保持不可变，修改内容生成新版本；下游不会静默改用新值。
        if all(old.get(k) == v for k, v in values.items()) and old['status'] == 'available':
            return old
    for old in state['artifacts'].values():
        if (old['status'] == 'available' and old.get('supersedes', '') == (artifact_id or '')
                and all(old.get(k) == v for k, v in values.items())):
            return old
    ref = f"A{state['next_artifact']}"
    state['next_artifact'] += 1
    row = {'id': ref, **values, 'status': 'available',
           'supersedes': artifact_id or '', 'created_step': session.current_step}
    state['artifacts'][ref] = row
    return row


def record_tool(session, name, args, text, meta, ledger):
    state = session.task_state
    state['sources'] = list(ledger.sources.values())
    state['next_citation'] = max(state['next_citation'], ledger.next_index)
    key = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
    record = {'name': name, 'arguments': copy.deepcopy(args), 'result': text,
              'outcome': meta.get('outcome'), 'tokens': meta.get('kb_tokens', 0),
              'step': session.current_step, 'status': 'completed'}
    state['records'].append(record)
    # 错误/空结果不永久缓存；版本变化时只剔除依赖相关文档的成功结果。
    if text and meta.get('outcome') in ('matched', 'read', 'outline'):
        state['seen'][key] = record


def _references(value, docs, citations=()):
    if isinstance(value, dict):
        return (isinstance(value.get('doc_id'), str) and value['doc_id'] in docs or
                type(value.get('citation')) is int and value['citation'] in citations or
                type(value.get('citation_index')) is int and value['citation_index'] in citations or
                any(isinstance(value.get(k), list) and any(type(ref) is int and ref in citations for ref in value[k])
                    for k in ('citations', 'source_citations')) or
                any(_references(v, docs, citations) for v in value.values()))
    if isinstance(value, list):
        return bool(value and isinstance(value[0], str) and value[0] in docs) or any(
            _references(v, docs, citations) for v in value)
    return False


def revalidate(session):
    from storage import kb_store as KS, kb_document_store as DS
    state = session.task_state
    if not session.kb_id:
        return True
    candidates = {s['doc_id']: s for s in state['sources'] if s.get('doc_id')}
    budget = state.get('budget') or {}
    for doc_id, version in budget.get('versions', {}).get('_snapshots', {}).items():
        candidates.setdefault(doc_id, version)
    invalid = set()
    with KS._transaction() as conn:
        for doc_id, source in candidates.items():
            try:
                doc = DS.owned(conn, session.kb_id, doc_id)
                if (doc['status'] != 'indexed' or any((doc.get(actual) or '') != (source.get(expected) or '')
                    for actual, expected in (('index_run_id', 'index_run_id'),
                        ('published_parse_id', 'parse_id'), ('published_structure_id', 'structure_id')))):
                    invalid.add(doc_id)
            except (KS.KBNotFound, KS.KBConflict):
                invalid.add(doc_id)
    if not invalid:
        return True
    refs = {s['index'] for s in state['sources'] if s.get('doc_id') in invalid}
    state['sources'] = [s for s in state['sources'] if s.get('doc_id') not in invalid]
    for artifact in state['artifacts'].values():
        if refs.intersection(artifact['source_citations']):
            artifact.update(status='invalid', content='', missing=['来源已撤回或版本改变，需要重新取得相关资料'])
    for binding in state['bindings'].values():
        artifact = state['artifacts'].get(binding['artifact_id'])
        if artifact and artifact['status'] != 'available':
            binding['status'] = 'invalid'
    for record in state['records']:
        try:
            payload = json.loads(record['result'] or '{}')
        except ValueError:
            payload = {}
        if _references(record['arguments'], invalid) or _references(payload, invalid, refs):
            record.update(status='invalid', result='', invalidated_docs=sorted(invalid))
    state['seen'] = {k: r for k, r in state['seen'].items() if r.get('status') != 'invalid'}
    last = state.get('last_result') or {}
    if _references(last, invalid, refs):
        state.pop('last_result', None)
    state['stalled_queries'] = {}
    state['history_facts_invalid_through'] = session.current_step
    state['history_summary_after_invalidation'] = False
    session.last_memory = ''
    session.last_evaluation = ''
    # 保存累计计数与不相关覆盖；视图和派生压缩可重建，不再整体重置预算。
    for key, value in list(budget.items()):
        if isinstance(value, list):
            budget[key] = [v for v in value if not _references(v, invalid, refs)]
    versions = budget.get('versions', {})
    for doc_id in invalid:
        versions.pop(doc_id, None)
        versions.get('_snapshots', {}).pop(doc_id, None)
    for key in ('evidence', 'outline_sources'):
        budget[key] = {k: v for k, v in budget.get(key, {}).items()
                       if not _references(v, invalid, refs)}
    for key in ('workspace', 'workspace_compaction', 'answer_draft'):
        budget.pop(key, None)
    state['invalidations'].append({'docs': sorted(invalid), 'step': session.current_step})
    return False


def history_for_context(session):
    from agent.state import HistoryItem
    cutoff = session.task_state.get('history_facts_invalid_through')
    if cutoff is None:
        return session.history_items
    # 旧自由文本没有逐项来源依赖，失效后仅投影动作线索，档案不改写。
    return [HistoryItem(step=i.step, action=i.action.split(' ',1)[0],
                       result='来源变更前的步骤，旧记忆/结论待重新核实')
            if i.step <= cutoff and not (i.step == -1 and session.task_state.get('history_summary_after_invalidation'))
            else i for i in session.history_items]


def record_receipt(session, action, result):
    state = session.task_state
    if any(r['action_id'] == action.action_id for r in state['receipts']):
        return
    binding = state['bindings'].get(action.action_id)
    from agent.materials import verification_matches
    verified = bool(binding and binding['status'] != 'invalid' and verification_matches(action, result))
    receipt = {'action_id': action.action_id, 'action_type': action.type,
               'artifact_id': binding['artifact_id'] if binding else '',
               'execution_state': result.execution_state, 'success': result.success,
               'stale': result.stale, 'verified': verified, 'details': result.details, 'error': result.error}
    state['receipts'].append(receipt)
    if result.success and not result.stale and result.execution_state == 'completed':
        state['stalled_queries'] = {}
    if binding and binding['status'] != 'invalid':
        binding['status'] = 'verified' if verified else 'needs_reobservation'


def public_state(session):
    from agent.field_review import public as public_reviews
    state = session.task_state
    return {'version': 2, 'source_count': len(state['sources']),
            'artifacts': [{k: v for k, v in a.items() if k != 'content'} for a in state['artifacts'].values()],
            'bindings': list(state['bindings'].values()), 'receipts': state['receipts'][-12:],
            'tool_count': sum(r['name'].startswith('kb_') and r['status'] != 'reused' for r in state['records']),
            'reuse_count': sum(r['name'].startswith('kb_') and r['status'] == 'reused' for r in state['records']),
            'phase':state.get('phase','observing'), 'last_tool':(state.get('last_result') or {}).get('tool',''),
            'last_outcome':((state.get('last_result') or {}).get('result') or {}).get('outcome',''),
            'archive_reads':(state.get('budget') or {}).get('archive_reads',0),
            'invalidations': state['invalidations'], 'error': state['error'],
            'pending_saves': [{k: failure.get(k) for k in ('repair_id', 'subject', 'purpose', 'code', 'error', 'hint', 'part', 'attempts')}
                for failure in sorted(state.get('artifact_errors', {}).values(), key=lambda e:e['step'])[-4:]],
            'pending_length_repairs': [r for r in state.get('field_length_repairs', {}).values() if r['status'] != 'resolved'],
            'pending_content_repairs': [r for r in state.get('field_content_repairs', {}).values() if r['status'] != 'resolved'],
            'field_reviews': public_reviews(session),
            'context':state.get('context',{}),
            'evidence_view':{k:v for k,v in (state.get('budget') or {}).get('workspace',{}).items()
                             if k not in ('packs',)}}
