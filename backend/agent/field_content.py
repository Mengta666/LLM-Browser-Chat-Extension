"""字段待填稿的用途绑定与可定位重复检查；不修改归档原文。"""
import copy
import hashlib
import json
import re


class FieldContentRequired(ValueError):
    def __init__(self, feedback):
        """携带字段整理所需的结构化反馈，供决策层解释未执行填写的原因。"""
        super().__init__(feedback['error'])
        self.feedback = feedback


def target(page, element):
    """提取标签页、文档、节点和字段描述，构成待填稿绑定的目标身份。"""
    return {'tab_id': page.tab_id, 'document_epoch': page.document_epoch,
            'frame_id': element.get('frame_id', ''),
            'node_id': element.get('backend_node_id', element.get('id')),
            'descriptor': {k: element.get(k, '') for k in
                           ('tag', 'type', 'name', 'html_id', 'aria_label', 'placeholder', 'form_context')}}


def target_key(subject, identity):
    """根据字段身份生成稳定的修稿记录键，避免更换成果编号绕过同一字段的限制。"""
    return 'C' + hashlib.sha256(json.dumps(identity, sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()[:12]


def duplicates(content, field_basis=None):
    # 先按段落/行检测，再补充完整句子；只忽略列表标记与空白，不折叠数字或否定词。
    """定位重复段落或句子的字符范围；局部修改时仅报告涉及修改区域的重复。"""
    units = {}
    for match in re.finditer(r'[^\r\n]+', content):
        line = match.group()
        body = re.sub(r'^\s*(?:[-*•·]\s+|\d+[.)、]\s*)', '', line)
        start = match.start() + len(line) - len(body)
        parts = [(start, match.end(), body)]
        parts.extend((start + m.start(), start + m.end(), m.group())
                     for m in re.finditer(r'[^。！？!?]+[。！？!?]|[^.!?]+\.(?=\s|$)', body))
        for left, right, text in parts:
            left += len(text) - len(text.lstrip())
            right -= len(text) - len(text.rstrip())
            normalized = re.sub(r'\s+', '', text).strip()
            if len(normalized) < 20:
                continue
            units.setdefault(normalized, set()).add((left, right))
    groups = []
    for text, spans in sorted(units.items(), key=lambda item: -len(item[0])):
        spans = sorted(spans)
        if len(spans) < 2:
            continue
        if any(all(any(a <= left and right <= b for a, b in g['ranges']) for left, right in spans)
               for g in groups):
            continue
        # 局部修改仅检查与替换区域相交的重复，保留区域中原有的重复不归入本次修稿问题。
        if field_basis and not any(left < edit['end'] and right > edit['start']
                                   for left, right in spans for edit in field_basis.get('output_edits', [])):
            continue
        groups.append({'id': 'D' + hashlib.sha256(text.encode()).hexdigest()[:12],
                       'ranges': [list(span) for span in spans], 'preview': content[spans[0][0]:spans[0][1]][:240],
                       'contexts': [content[max(0, a-60):min(len(content), b+40)] for a, b in spans[:4]]})
    return groups


def prepare(session, content, intent, page, subject, field_basis=None):
    """校验待填稿的目标、用途和重复内容，返回与当前字段绑定的填写意图。"""
    from agent.task_workspace import ArtifactError
    if (not isinstance(intent, dict) or set(intent) - {'index', 'scope', 'exclude', 'keep_repetitions'}
            or type(intent.get('index')) is not int or not isinstance(intent.get('scope'), str)
            or not intent['scope'].strip() or len(intent['scope']) > 400):
        raise ValueError('field_intent 需要当前 index 和明确 scope（此字段所需内容）；可选 exclude、keep_repetitions')
    element = next((e for e in page.interactive_elements if e.get('id') == intent['index']), None) if page else None
    if (not element or not element.get('enabled', True) or element.get('read_only')
            or element.get('type') in ('password', 'hidden', 'file')
            or not (element.get('editable') or element.get('tag') in ('textarea', 'input', 'select')
                    or element.get('role') in ('textbox', 'combobox'))):
        raise ValueError('field_intent.index 必须指向本次观察中可填写的字段')
    excluded = intent.get('exclude', [])
    if (not isinstance(excluded, list) or len(excluded) > 12
            or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in excluded)):
        raise ValueError('field_intent.exclude 是本字段不需要的内容说明数组，不是删除关键词规则')
    identity = target(page, element)
    key = target_key(subject, identity)
    label = element.get('aria_label') or element.get('placeholder') or element.get('text') or element.get('name') or '当前字段'
    base = {'content_repair_id': key, 'target_identity': identity, 'subject': subject,
            'field_label': label, 'index': intent['index']}
    previous = session.task_state.get('field_content_repairs', {}).get(key, {})
    if previous.get('status') == 'blocked':
        raise ArtifactError('field_content_blocked', '此字段连续修稿未通过，未继续保存或写入；请报告未完成，可继续其他已授权字段',
                            hint='不要更换成果编号绕过检查，也不要重复检索已有资料。', **base)
    repeated = duplicates(content, field_basis)
    keep = intent.get('keep_repetitions', [])
    if (not isinstance(keep, list) or len(keep) > len(repeated)
            or any(not isinstance(v, dict) or set(v) != {'id', 'reason'}
                   or not isinstance(v['id'], str)
                   or v['id'] not in {g['id'] for g in repeated}
                   or not isinstance(v['reason'], str) or not v['reason'].strip() or len(v['reason']) > 400 for v in keep)
            or len({v['id'] for v in keep}) != len(keep)):
        raise ValueError('keep_repetitions 只能引用此次检查返回的重复 id，并逐项说明不同对象/原样复制等保留原因')
    unresolved = [g for g in repeated if g['id'] not in {v['id'] for v in keep}]
    if unresolved:
        raise ArtifactError('duplicate_field_content', '待填稿存在重复的完整段落或句子，尚未保存、未写入',
            hint='根据重复位置修稿并重存，原始资料不变，不重新检索。若不同对象确需重复或用户要求逐字保留，'
                 '用 field_intent.keep_repetitions=[{id:返回的D编号,reason:具体保留理由}] 明确说明；不能一律删除。',
            duplicates=unresolved[:8], duplicate_count=len(unresolved), **base)
    return {**base, 'scope': intent['scope'].strip(), 'exclude': copy.deepcopy(excluded),
            'keep_repetitions': copy.deepcopy(keep), 'check': 'deterministic_only'}


def check(session, page, element, artifact):
    """确认叙述性成果已按当前字段整理，否则抛出可用于修稿的反馈。"""
    text = artifact['content']
    intent = artifact.get('field_intent')
    narrative = (element.get('tag') == 'textarea' or element.get('editor_type') in ('contenteditable', 'codemirror5')
                 or '\n' in text or len(text) > 120)
    if element.get('tag') == 'select' or element.get('type', '') not in ('', 'text', 'search'):
        return
    if not narrative and not intent:
        return
    identity = target(page, element)
    key = target_key(artifact['subject'], identity)
    blocked = session.task_state.get('field_content_repairs', {}).get(key, {}).get('status') == 'blocked'
    if intent and intent['target_identity'] == identity and not blocked:
        return
    label = element.get('aria_label') or element.get('placeholder') or element.get('text') or element.get('name') or '当前字段'
    feedback = {'code': 'field_content_blocked' if blocked else 'field_draft_required',
        'error': '此字段连续修稿未通过，未写入' if blocked else '原成果尚未按当前字段整理或目标已变化，未写入',
        'content_repair_id': key, 'artifact_id': artifact['id'], 'subject': artifact['subject'],
        'field_label': label, 'index': element['id'], 'target_identity': identity,
        'duplicates': duplicates(text, artifact.get('field_basis'))[:8],
        'hint': '结合用户要求、目标字段及相邻控件，复用原成果保存待填稿：save_task_result(kind=draft,subject=原对象,'
                'purpose=字段用途,content=整理后正文,origin=derived,dependencies=[原A编号],'
                'field_intent={index:当前控件,scope:本字段需要什么,exclude:[不属于此字段的内容]})。'
                '去重、筛选后再限长；名称/日期是否保留按实际页面判断，不按关键词删除。'
                '仅局部修改则继续 field_ref/field_edits，并增加 field_intent，不能改写保留部分。'
                '失败不重查资料；保存成功后用新成果填写并回读。'}
    raise FieldContentRequired(feedback)


def record_error(session, feedback, *, revision=False):
    """累计字段修稿失败次数，更新阻塞状态并保存下一步所需的反馈。"""
    state = session.task_state
    feedback = copy.deepcopy(feedback)
    key = feedback['content_repair_id']
    previous = state.setdefault('field_content_repairs', {}).get(key, {})
    attempts = previous.get('attempts', 0) + 1
    revisions = previous.get('revisions', 0) + int(revision)
    blocked = previous.get('status') == 'blocked' or revisions >= 3 or attempts >= 4
    feedback.update(attempts=attempts, revisions=revisions, status='blocked' if blocked else 'pending')
    if blocked:
        feedback['hint'] = '此字段连续修稿未通过，不再写入。报告此字段未完成；可继续其他已授权字段，不重查已有资料。'
    state['field_content_repairs'][key] = feedback
    state['error'] = feedback['error']
    state['phase'] = 'content_repair'
    state['last_result'] = {'tool': 'prepare_field_content', 'result': feedback}
    if feedback.get('artifact_id'):
        state['focus_artifacts'] = [feedback['artifact_id']]
    return feedback
