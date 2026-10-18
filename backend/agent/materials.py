"""自动化表单物料：只读取材、来源约束与任务内字段状态。"""

import copy
import hashlib
import json
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from urllib.parse import urlsplit


OUTPUT_TOKENS = int(os.getenv('AGENT_MATERIAL_OUTPUT_TOKENS', '8192'))
if OUTPUT_TOKENS < 1:
    raise ValueError('AGENT_MATERIAL_OUTPUT_TOKENS 必须为正整数')
FIELD_KEYS = ('tag', 'type', 'role', 'name', 'html_id', 'text', 'aria_label', 'placeholder',
              'frame_id', 'form_context', 'field_context', 'date_format', 'min', 'max',
              'step', 'value_attribute', 'maxlength', 'options')



def initial_state():
    return {'fields': {}, 'next_field': 1, 'next_material': 1, 'error': ''}


def validate_kb(kb_id):
    from storage import kb_store as KS
    from agent.memory.config import CHAT_USER_ID
    with KS._transaction() as conn:
        KS._owned_kb(conn, kb_id, CHAT_USER_ID, active=True)


def field_description(element):
    return {key: element[key] for key in FIELD_KEYS if element.get(key) not in (None, '', [])}


def field_signature(element, page, entity):
    origin = urlsplit(page.url)
    identity = [origin.scheme, origin.netloc, origin.path, entity, field_description(element)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def collect_fields(session, page, args):
    indices = args.get('indices')
    entity = args.get('entity', '')
    if (not isinstance(entity, str) or not entity.strip() or len(entity) > 300 or
            not isinstance(indices, list) or len(indices) != 1 or
            any(type(i) is not int for i in indices) or len(set(indices)) != len(indices)):
        raise ValueError('取材需要明确 entity 及不重复的当前控件 indices')
    if args.get('doc_id') and args.get('doc_name'):
        raise ValueError('doc_id/doc_name 只能选一个')
    entity = entity.strip()
    state = copy.deepcopy(session.material_state)
    result = []
    for index in indices:
        matches = [e for e in page.interactive_elements if e.get('id') == index]
        if len(matches) != 1:
            raise ValueError('取材控件不属于当前观察')
        element = matches[0]
        if (element.get('type') in ('password', 'hidden', 'file', 'submit', 'button', 'checkbox', 'radio') or
                not element.get('enabled', True) or element.get('read_only') or
                not (element.get('editable') or element.get('tag') in ('input', 'textarea', 'select') or
                     element.get('role') in ('textbox', 'combobox'))):
            raise ValueError('取材只接受可填写的文本、日期、数值或选择控件')
        label = element.get('aria_label') or element.get('text') or element.get('placeholder') or element.get('name')
        if not label:
            raise ValueError('字段没有可靠名称，请先定位字段说明')
        signature = field_signature(element, page, entity)
        field = next((f for f in state['fields'].values() if f['signature'] == signature), None)
        if field is None:
            field_id = f"F{state['next_field']}"
            state['next_field'] += 1
            field = {'field_id': field_id, 'signature': signature, 'entity': entity.strip(),
                     'label': label, 'descriptor': field_description(element), 'status': 'pending',
                     'fill_status': 'unfilled', 'sources': [], 'value': '', 'raw_value': ''}
            state['fields'][field_id] = field
        field['index'] = index
        field['observation_id'] = page.observation_id
        result.append(field)
    if len({f['field_id'] for f in result}) != len(result):
        raise ValueError('重复字段缺少分组或记录身份，不能确定对应关系')
    session.material_state = state
    return result




def transform_value(raw, transform, field, quotes):
    if set(transform) - {'kind','from','to'}:
        raise ValueError('转换仅接受 kind、from、to；例如 {"kind":"unit","from":"MPa","to":"kPa"}')
    kind = transform.get('kind', 'identity')
    if kind == 'identity':
        return raw
    descriptor = field['descriptor']
    if kind == 'date':
        fmt = descriptor.get('date_format', '')
        if descriptor.get('type') == 'date':
            fmt = 'YYYY-MM-DD'
        formats = {'YYYY-MM-DD': '%Y-%m-%d', 'yyyy-MM-dd': '%Y-%m-%d',
                   'YYYY/MM/DD': '%Y/%m/%d', 'yyyy/MM/dd': '%Y/%m/%d',
                   'YYYY年MM月DD日': '%Y年%m月%d日'}
        if fmt not in formats:
            raise ValueError('页面未提供可确认的日期格式')
        for source_format in ('%Y-%m-%d', '%Y/%m/%d', '%Y年%m月%d日'):
            try:
                return datetime.strptime(raw, source_format).strftime(formats[fmt])
            except ValueError:
                pass
        raise ValueError('原文日期有歧义或无效')
    if kind == 'unit':
        units = {'Pa': ('pressure', '1'), 'kPa': ('pressure', '1000'), 'MPa': ('pressure', '1000000'),
                 'bar': ('pressure', '100000'), 'mm': ('length', '.001'), 'cm': ('length', '.01'),
                 'm': ('length', '1'), 'g': ('mass', '.001'), 'kg': ('mass', '1'),
                 'min': ('time', '60'), 'h': ('time', '3600'), 's': ('time', '1')}
        source, target = transform.get('from'), transform.get('to')
        target_text = ' '.join(str(descriptor.get(k, '')) for k in
                               ('text', 'aria_label', 'placeholder', 'field_context'))
        has_unit = lambda unit, text: bool(re.search(r'(?<![A-Za-z])' + re.escape(unit) + r'(?![A-Za-z])', text))
        if (source not in units or target not in units or units[source][0] != units[target][0] or
                not has_unit(source, quotes) or not has_unit(target, target_text)):
            raise ValueError('单位不兼容，或原文/页面未明确相应单位')
        number = raw.strip()
        if number.endswith(source):
            number = number[:-len(source)].strip()
        try:
            value = Decimal(number) * Decimal(units[source][1]) / Decimal(units[target][1])
        except InvalidOperation:
            raise ValueError('成果不是可精确转换的数值。请先用原来源保存仅含数值及可选单位的新成果（不要整句），再绑定新成果；不能重复换算整句或模糊截取数字') from None
        if not value.is_finite():
            raise ValueError('数值不是有限数')
        return format(value.normalize(), 'f')
    raise ValueError('不支持该物料转换')






def public_state(session):
    return {'kb_id': session.kb_id, 'error': session.material_state.get('error', ''),
            'fields': [{k: f.get(k) for k in ('field_id', 'material_id', 'entity', 'label', 'status',
                        'raw_value', 'value', 'transform', 'sources', 'reason', 'fill_status')}
                       for f in session.material_state.get('fields', {}).values()]}


class FieldSnapshotRequired(ValueError):
    pass


class FieldLengthExceeded(ValueError):
    def __init__(self, *, artifact_id, subject, label, index, maximum, actual, signature, target_identity):
        super().__init__(f'字段“{label}”最多 {maximum} 个 UTF-16 长度单位，待填成果 {artifact_id} 为 {actual}，超出 {actual - maximum}；未写入')
        self.feedback = {'code': 'field_value_too_long', 'error': str(self),
            'artifact_id': artifact_id, 'subject': subject, 'field_label': label, 'index': index,
            'max_length': maximum, 'actual_length': actual, 'over_by': actual - maximum,
            'length_unit': 'utf16', 'field_signature': signature, 'target_identity': target_identity}


def validate_snapshot(snapshot):
    if snapshot is not None and (not isinstance(snapshot, dict) or snapshot.get('format') != 'text-v1'
            or not isinstance(snapshot.get('id'), str) or not snapshot['id']
            or not isinstance(snapshot.get('digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', snapshot['digest'])
            or type(snapshot.get('length')) is not int or snapshot['length'] < 0):
        raise ValueError('字段快照格式无效，请更新并重新加载扩展')


def bind_action(session, page, action, args):
    before = copy.deepcopy(session.material_state)
    try:
        _bind_action(session, page, action, args)
    except ValueError:
        session.material_state = before
        raise


def _bind_action(session, page, action, args):
    if action.type == 'read_field':
        from agent.field_reads import prepare
        prepare(session, page, action, args)
        return
    element = next((e for e in page.interactive_elements if e.get('id') == action.index), None)
    if action.type in ('clear', 'press_key') and session.task_state.get('field_review_jobs'):
        from agent.field_content import target
        from agent.field_review import ReviewRequired
        focused = element or next((e for e in page.interactive_elements if e.get('focused') or
            e.get('html_id') and page.focused_element == '#'+e['html_id']), None)
        if focused and any(job['target_identity'] == target(page, focused)
                           for job in session.task_state['field_review_jobs'].values()):
            raise ReviewRequired('此字段已有待填稿，不能用清空或按键绕过绑定审核；请使用审核通过的 artifact_id 执行 type，其他字段仍可处理。')
    from agent.field_reads import identity
    read_target = element and any(r['identity'] == identity(page, element)
                                 for r in session.task_state.get('field_reads', {}).values())
    if ((session.task_state['sources'] or session.task_state['artifacts'] or read_target
            or session.task_state.get('field_content_repairs') or session.task_state.get('artifact_errors'))
            and action.type in ('type', 'select')
            and not args.get('artifact_id') and not args.get('material_id')
            and not (element and (element.get('type') == 'search' or element.get('role') == 'searchbox'))):
        raise ValueError('填写需要先保存成果并绑定 artifact_id/subject。多段原文用 source_parts 按序组合，由后端生成全文；不要将拼接全文声明为 verbatim。改写用 content/citations/origin=derived。保存失败请修正保存参数，已有来源保留，不必重查；不能用裸 text/option_text 绕过')
    if args.get('artifact_id'):
        artifact = session.task_state['artifacts'].get(args['artifact_id'])
        if not artifact or artifact['status'] != 'available' or artifact.get('missing'):
            raise ValueError('成果不存在、失效或仍有缺项，不允许用于填写')
        if args.get('subject') != artifact['subject']:
            raise ValueError('填写对象必须与成果 subject 一致，不可跨对象盲目复用')
        if action.type not in ('type', 'select', 'verify_field'):
            raise ValueError('成果只绑定到 type/select/verify_field，不提供点击或提交授权')
        if artifact.get('field_basis'):
            from agent.field_reads import identity
            basis = artifact['field_basis']
            current_digest = (element.get('field_snapshot') or {}).get('digest') if element else None
            if (not element or basis['identity'] != identity(page, element)
                    or action.type != 'verify_field' and basis['snapshot']['digest'] != current_digest
                    and hashlib.sha256(artifact['content'].encode('utf-8')).hexdigest() != current_digest):
                raise ValueError('成果依据的旧字段已经变化；请 read_field 从头读取，不传旧 field_ref，再基于新版本保存局部修改成果，不能覆盖新内容')
        fields = collect_fields(session, page, {'indices':[action.index], 'entity':artifact['subject']})
        field = fields[0]
        sources = {s['index']:s for s in session.task_state['sources']}
        quoted = [sources[r] for r in artifact['source_citations']]
        transform = args.get('transform') or {'kind':'identity'}
        if not isinstance(transform, dict):
            raise ValueError('transform 必须为对象')
        if (field['descriptor'].get('type') == 'date' or field['descriptor'].get('date_format')) and transform.get('kind') == 'identity':
            transform = {'kind':'date'}
        quotes = artifact['content'] if artifact['origin'] == 'user_input' else '\n'.join(s['content'] for s in quoted)
        value = transform_value(artifact['content'], transform, field, quotes)
        field.update(status='available', reason='', artifact_id=artifact['id'], raw_value=artifact['content'], value=value,
            transform=transform, sources=[{'citation':s['index'], 'quote':s['content'],
                **{k:s.get(k) for k in ('kb_id','doc_id','parse_id','index_run_id','structure_id','title','locations')}} for s in quoted])
        if not field.get('material_id'):
            field['material_id'] = f"MAT{session.material_state['next_material']}"
            session.material_state['next_material'] += 1
        args = {**args, 'material_id':field['material_id']}
    fields = list(session.material_state['fields'].values())
    matched = [f for f in fields if element and field_signature(element, page, f['entity']) == f['signature']]
    material_id = args.get('material_id')
    if not material_id:
        if action.type == 'verify_field':
            raise ValueError('只读核实必须绑定有效的 artifact_id/subject 或 material_id')
        if matched and action.type in ('type', 'select', 'clear', 'press_key'):
            raise ValueError('已取材字段只能用 type/select(index,material_id)，不能另编值或先清空')
        return
    matched = [f for f in matched if f.get('material_id') == material_id]
    if action.type not in ('type', 'select', 'verify_field') or len(matched) != 1:
        raise ValueError('物料与当前字段身份不一致；请重新观察或为新字段取材，勿复用旧编号')
    field = matched[0]
    if field['status'] != 'available':
        raise ValueError('该字段物料缺失、冲突或不明确，不允许填写')
    if not element.get('enabled', True) or element.get('read_only'):
        raise ValueError('字段不可编辑或原值未完整回读，不能覆盖')
    snapshot = element.get('field_snapshot')
    validate_snapshot(snapshot)
    if element.get('value_truncated') and action.type != 'verify_field' and not snapshot:
        raise FieldSnapshotRequired('缺少完整原值快照，需要重新观察；若持续出现，请更新并重新加载扩展')
    value = field['value']
    if action.type == 'type' and field.get('artifact_id'):
        from agent.field_content import check
        check(session, page, element, session.task_state['artifacts'][field['artifact_id']])
    selecting = action.type == 'select' or action.type == 'verify_field' and element.get('tag') == 'select'
    option = None
    if selecting:
        if element.get('tag') != 'select':
            raise ValueError('物料选择目前只支持原生下拉，自定义组件需人工确认')
        options = element.get('options', [])
        matching = [(i, o) for i, o in enumerate(options) if isinstance(o, dict) and o.get('text') == value]
        if len(matching) != 1 or matching[0][1].get('disabled'):
            raise ValueError('物料没有唯一可用的下拉选项，不能进行模糊替换')
        option = {'index': matching[0][0], 'text': value, 'value': matching[0][1]['value']}
    elif element.get('tag') == 'select':
        raise ValueError('原生下拉请使用 select 而不是 type')
    elif element.get('maxlength') not in ('', None):
        maximum = int(element['maxlength'])
        actual = len(value.encode('utf-16-le')) // 2
        if maximum >= 0 and actual > maximum:
            artifact = session.task_state['artifacts'].get(field.get('artifact_id'))
            if (action.type == 'type' and maximum > 0 and artifact
                    and not artifact.get('field_basis')
                    and element.get('type', '') in ('', 'text', 'search')
                    and (field.get('transform') or {}).get('kind', 'identity') == 'identity'):
                raise FieldLengthExceeded(artifact_id=artifact['id'], subject=artifact['subject'],
                    label=field['label'], index=action.index, maximum=maximum, actual=actual,
                    signature=field['signature'], target_identity=identity(page, element))
            raise ValueError(f'字段上限 {maximum}，待填长度 {actual}（UTF-16）；不能截断精确值或改写局部修改的保留部分。'
                             '局部修改请只精简替换片段后重新生成 field_edits 成果；只读核实不触发改写。')
    if element.get('type') == 'number':
        try:
            number = Decimal(value)
            if not number.is_finite():
                raise InvalidOperation
            if element.get('min') not in ('', None) and number < Decimal(element['min']):
                raise ValueError('物料低于字段最小值')
            if element.get('max') not in ('', None) and number > Decimal(element['max']):
                raise ValueError('物料超过字段最大值')
            if element.get('step') != 'any':
                try:
                    step = Decimal(element.get('step') or '1')
                    if not step.is_finite() or step <= 0:
                        step = Decimal(1)
                except InvalidOperation:
                    step = Decimal(1)
                base = Decimal(element.get('min') or element.get('value_attribute') or '0')
                if (Fraction(number) - Fraction(base)) % Fraction(step):
                    raise ValueError(f'物料不满足字段步长 step={step}、基准={base}；不可舍入或改变原始数值')
        except InvalidOperation:
            raise ValueError('数值字段需要完整数值，不能携带单位或描述') from None
    artifact = session.task_state['artifacts'].get(field.get('artifact_id'))
    if artifact and artifact.get('field_intent'):
        from agent.field_review import require
        require(session, artifact, page, element)
    prior = str(element.get('value') or '')
    current = str(element.get('selected_text') or '') if selecting else prior
    snapshot_matches = snapshot and hashlib.sha256(value.encode('utf-8')).hexdigest() == snapshot['digest']
    if current == value and not element.get('value_truncated') or snapshot_matches:
        action.type = 'verify_field'
    action.params = {'material_id': material_id, 'field_id': field['field_id'],
                     'material_label': field['label'], 'expected_prior': prior,
                     'material_identity': {k: element.get(k, '') for k in (
                         'tag', 'type', 'name', 'html_id', 'placeholder', 'aria_label', 'form_context', 'field_context')},
                     'confirm_overwrite': action.type != 'verify_field' and bool(prior and current != value),
                     **({'option_text': value, 'expected_option': option} if selecting else {'text': value, 'clear': True})}
    if action.type == 'verify_field' and element.get('value_truncated'):
        action.params['expected_prior_complete'] = False
    if snapshot:
        action.params['field_snapshot'] = copy.deepcopy(snapshot)
    action.params['expected_prior_complete'] = not bool(element.get('value_truncated'))
    field['index'] = action.index
    field['target_identity'] = identity(page, element)
    field['fill_status'] = 'pending'
    action.params['artifact_id'] = field.get('artifact_id', '')
    artifact = session.task_state['artifacts'].get(field.get('artifact_id'))
    if artifact:
        action.params['artifact_origin'] = artifact['origin']
        if artifact.get('field_basis'):
            action.params['preserve_field_format'] = True


def verification_matches(action, result):
    check = result.material_verification or {}
    expected = action.params.get('option_text', action.params.get('text', ''))
    expected = expected.replace('\r\n', '\n').replace('\r', '\n')
    option = action.params.get('expected_option')
    return (result.success and not result.stale and result.execution_state == 'completed'
            and check.get('material_id') == action.params.get('material_id')
            and check.get('artifact_id') == action.params.get('artifact_id')
            and check.get('matched') is True and check.get('valid') is True
            and check.get('value') == expected
            and (not option or check.get('option') == option))


def record_result(session, action, result):
    material_id = action.params.get('material_id')
    if not material_id:
        return
    field = next((f for f in session.material_state['fields'].values() if f.get('material_id') == material_id), None)
    if field:
        verified = (field['status'] == 'available' and field.get('artifact_id') == action.params.get('artifact_id')
                    and verification_matches(action, result))
        field['fill_status'] = ('verified' if verified else 'not_dispatched'
                               if result.execution_state == 'not_dispatched' and action.type != 'verify_field' else 'unverified')
        if verified:
            artifact = session.task_state['artifacts'].get(field.get('artifact_id'), {})
            job = session.task_state.get('field_review_jobs', {}).get(artifact.get('edit_job_id'))
            if job and job['artifact_id'] == artifact.get('id') and job['status'] == 'approved':
                job['status'] = 'resolved'
            intent = artifact.get('field_intent', {})
            repair = session.task_state.get('field_content_repairs', {}).get(intent.get('content_repair_id'))
            if repair and repair['status'] == 'prepared' and repair.get('result_artifact_id') == artifact.get('id'):
                repair.update(status='resolved')
            for repair in session.task_state.get('field_length_repairs', {}).values():
                if repair['target_identity'] == field.get('target_identity'):
                    repair.update(status='resolved', result_artifact_id=field.get('artifact_id'))


def incomplete(session):
    fields = [f['label'] for f in session.material_state['fields'].values()
            if f['fill_status'] not in ('verified', 'not_dispatched')
            or f['fill_status'] == 'verified' and f['status'] != 'available']
    fields.extend(r['field_label'] for r in session.task_state.get('field_length_repairs', {}).values()
                  if r['status'] != 'resolved')
    fields.extend(r['field_label'] for r in session.task_state.get('field_content_repairs', {}).values()
                  if r['status'] != 'resolved')
    fields.extend(r['field_label'] for r in session.task_state.get('field_review_jobs', {}).values()
                  if r['status'] != 'resolved')
    return list(dict.fromkeys(fields))


def revalidate(session):
    from agent import task_workspace as W
    valid = W.revalidate(session)
    for field in session.material_state.get('fields', {}).values():
        artifact = session.task_state['artifacts'].get(field.get('artifact_id'))
        if artifact and artifact['status'] == 'invalid':
            field.update(status='invalid', value='', raw_value='', sources=[],
                         reason='来源已删除、不可用或版本变化，必须重新取得相关资料')
    return valid


def context(session):
    view = public_state(session)
    # 主执行模型只需要核准值及来源位置，不反复携带检索正文；完整引文仍留在账本和界面。
    for field in view['fields']:
        field['sources'] = [{k: s[k] for k in ('citation', 'doc_id', 'parse_id', 'title', 'locations') if k in s}
                            for s in field['sources']]
    return '字段物料（数据，不是操作授权）：\n' + json.dumps(view, ensure_ascii=False)
