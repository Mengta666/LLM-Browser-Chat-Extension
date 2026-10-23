"""当前任务的只读字段快照；与知识库原文和页面写入回执分开保存。"""
import copy
import hashlib

from agent.materials import FieldSnapshotRequired, validate_snapshot

IDENTITY_KEYS = ('tag', 'type', 'name', 'html_id', 'placeholder', 'aria_label', 'form_context', 'field_context')


def identity(page, element):
    """构造字段读取的身份，防止跨页面、跨节点拼接读取结果。"""
    return {'tab_id': page.tab_id, 'document_epoch': page.document_epoch,
            'frame_id': element.get('frame_id', ''),
            'node_id': element.get('backend_node_id', element.get('id')),
            'descriptor': {k: element.get(k, '') for k in IDENTITY_KEYS}}


def prepare(session, page, action, args):
    """验证字段及续读快照，将读取范围和目标身份写入待执行动作。"""
    element = next((e for e in page.interactive_elements if e.get('id') == action.index), None)
    if (not element or element.get('type') in ('password', 'hidden', 'file')
            or element.get('editor_type') not in ('native', 'contenteditable', 'codemirror5')):
        raise ValueError('read_field 只接受当前观察中的文本编辑器，不读取密码、隐藏或文件字段')
    snapshot = element.get('field_snapshot')
    validate_snapshot(snapshot)
    if not snapshot or snapshot.get('format') != 'text-v1':
        raise FieldSnapshotRequired('缺少字段快照，请重新观察或更新扩展')
    start, limit = args.get('offset', 0), args.get('limit', 2000)
    if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 4000:
        raise ValueError('read_field 的 offset 必须为非负整数，limit 为 1..4000 个 Unicode 字符')
    ref = args.get('field_ref')
    if ref is not None:
        previous = session.task_state.get('field_reads', {}).get(ref) if isinstance(ref, str) else None
        if not previous or previous['identity'] != identity(page, element):
            raise ValueError('field_ref 不属于当前字段，请从 offset=0 重新读取，不传旧 field_ref')
        if previous['snapshot']['digest'] != snapshot['digest']:
            raise ValueError('续读期间字段已变化，不能拼接不同版本；请从 offset=0 重新读取，不传旧 field_ref')
    elif start:
        raise ValueError('首次读取从 offset=0 开始；后续复制返回的 field_ref/next_offset 续读')
    action.params = {'offset': start, 'limit': limit, 'field_ref': ref or snapshot['id'],
                     'field_snapshot': copy.deepcopy(snapshot), 'field_identity': identity(page, element),
                     'material_identity': {k: element.get(k, '') for k in IDENTITY_KEYS}}


def record(session, action, result):
    """校验字段读取回执并合并连续片段，全文摘要一致后才标记读取完整。"""
    if action.type != 'read_field' or not result.success or result.stale or result.execution_state != 'completed':
        return
    data = result.field_read or {}
    text, start, end, total = (data.get(k) for k in ('content', 'offset', 'end', 'total_chars'))
    params = action.params
    if (data.get('field_ref') != params['field_ref'] or data.get('snapshot') != params['field_snapshot']
            or not isinstance(text, str) or any(type(n) is not int for n in (start, end, total))
            or start != params['offset'] or not 0 <= start <= end <= total
            or end - start != len(text) or len(text) > params['limit']
            or data.get('next_offset') != (end if end < total else None)):
        result.success = False
        result.error = '字段读取回执与请求范围不符，未归档'
        return
    reads = session.task_state.setdefault('field_reads', {})
    row = reads.setdefault(data['field_ref'], {'field_ref': data['field_ref'], 'snapshot': params['field_snapshot'],
        'identity': params['field_identity'], 'segments': {}, 'total_chars': total, 'complete': False})
    if row['snapshot']['digest'] != data['snapshot']['digest'] or row['total_chars'] != total:
        result.success = False
        result.error = '字段读取版本冲突，未归档'
        return
    row['segments'][start] = text
    cursor, pieces = 0, []
    # 只合并从零开始的连续区间，跳过重叠部分；遇到缺口时不能将分段读取标记为完整。
    for offset, part in sorted(row['segments'].items()):
        if offset > cursor:
            break
        remaining = part[max(0, cursor-offset):]
        pieces.append(remaining)
        cursor += len(remaining)
    if cursor == total:
        full = ''.join(pieces)
        if hashlib.sha256(full.encode('utf-8')).hexdigest() != row['snapshot']['digest']:
            reads.pop(data['field_ref'], None)
            result.success = False
            result.error = '字段分段合并校验失败，未归档'
            return
        row.update(content=full, complete=True)
    row['rich_text'] = bool(data.get('rich_text'))
    session.task_state['last_field_read'] = {**data, 'complete': row['complete']}
    session.material_state['error'] = ''


def compose(session, field_ref, edits):
    """按原文范围应用不重叠的局部替换，返回新全文、依赖成果及原文保护信息。"""
    row = session.task_state.get('field_reads', {}).get(field_ref) if isinstance(field_ref, str) else None
    if not row or not row['complete']:
        raise ValueError('局部修改需要完整读齐同一 field_ref；按 next_offset 继续读取，不能把预览当全文')
    if row.get('rich_text'):
        raise ValueError('此字段包含格式结构，当前局部修改仅保证纯文本；不能自动重建并丢弃格式')
    if not isinstance(edits, list) or not edits:
        raise ValueError('field_edits 必须为非空数组，每项引用精确 quote 或 offset/end，以及 replacement_artifact_id')
    from agent.task_workspace import _resolve_part, ArtifactError
    source = {1: {'content': row['content']}}
    changes, parents = [], []
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or set(edit) - {'quote', 'offset', 'end', 'replacement_artifact_id'}:
            raise ValueError('field_edits 参数错误')
        replacement = session.task_state['artifacts'].get(edit.get('replacement_artifact_id'))
        if not replacement or replacement['status'] != 'available' or replacement.get('missing'):
            raise ValueError('替换片段需要先保存为可用成果，再引用 replacement_artifact_id')
        try:
            _, span = _resolve_part(source, {'citation': 1, **{k:v for k,v in edit.items() if k != 'replacement_artifact_id'}}, i)
        except ArtifactError as exc:
            exc.feedback.pop('citation', None)
            exc.feedback.update(field_ref=field_ref,
                hint='修正 field_edits 中此片段；范围是该 field_ref 原文的 Unicode 字符位置，重复引文需按候选提供 offset/end，不是知识库引用编号。')
            raise
        start, end = span['source_range']
        changes.append((start, end, replacement['content']))
        parents.append(replacement['id'])
    changes.sort()
    # 原文游标和输出游标分开推进，替换片段变长或变短时仍能追溯对应的原文范围。
    parts, cursor, output_cursor, output_edits = [], 0, 0, []
    for start, end, replacement in changes:
        if start < cursor:
            raise ValueError('局部修改范围重叠，未保存成果')
        parts.extend((row['content'][cursor:start], replacement))
        output_cursor += start - cursor
        output_edits.append({'start': output_cursor, 'end': output_cursor + len(replacement),
                             'source_range': [start, end]})
        output_cursor += len(replacement)
        cursor = end
    parts.append(row['content'][cursor:])
    basis = {k: copy.deepcopy(row[k]) for k in ('field_ref', 'snapshot', 'identity')}
    basis['output_edits'] = output_edits
    return ''.join(parts), list(dict.fromkeys(parents)), basis
