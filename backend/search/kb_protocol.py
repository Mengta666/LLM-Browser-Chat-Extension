"""模型读取模式与旧 HTTP 定位之间的无损适配。"""

import copy


DOCUMENT = {'doc_id': {'type': 'string', 'minLength': 1},
            'doc_name': {'type': 'string', 'minLength': 1},
            'parse_id': {'type': 'string', 'minLength': 1},
            'structure_id': {'type': 'string', 'minLength': 1},
            'index_run_id': {'type': 'string', 'minLength': 1}}
POSITION = {'block_id': {'type': 'string', 'minLength': 1},
            'page': {'type': 'integer', 'minimum': 1},
            'section_id': {'type': 'string', 'minLength': 1},
            'formula_number': {'type': 'string', 'minLength': 1, 'description': '原文公式编号，例如 10 或 2.3；允许成对的中英文括号'},
            'offset': {'type': 'integer', 'minimum': 0}, 'end': {'type': 'integer', 'minimum': 0},
            'row_start': {'type': 'integer', 'minimum': 0}, 'row_end': {'type': 'integer', 'minimum': 1},
            'node_id': {'type': 'string', 'minLength': 1}, 'cursor': {'type':'integer','minimum':0},
            'block_offset': {'type':'integer','minimum':0}}
INTENT = {'type':'string','enum':['preview','complete'],
          'description':'preview 仅抽看，不建立通读义务；complete 要求读完本目标范围。默认页/章节为 preview，其他精确目标为 complete。不要把检索候选当成已选择全文。'}
MODES = {'block': (('block_id',), ('page', 'section_id')),
         'range': (('block_id', 'offset'), ('end', 'page', 'section_id')),
         'rows': (('block_id', 'row_start'), ('row_end', 'page', 'section_id')),
         'page': (('page',), ()), 'section': (('section_id',), ('page',)),
         'formula': (('formula_number',), ('page', 'section_id')),
         'related': (('block_id',), ('page', 'section_id')),
         'node': (('node_id',), ('cursor','block_offset'))}
MODES = {mode: (required, tuple(dict.fromkeys((*optional, 'cursor', 'block_offset'))))
         for mode, (required, optional) in MODES.items()}


def target_schema():
    """构造各读取模式的参数契约，明确必填定位和允许的字段。"""
    return {'oneOf': [{'type': 'object', 'properties': {**DOCUMENT,
        'mode': {'const': mode}, 'intent':INTENT, **{k: POSITION[k] for k in (*required, *optional)}},
        'required': ['mode', *required], 'additionalProperties': False,
        'oneOf': [{'required': ['doc_id']}, {'required': ['doc_name']}]}
        for mode, (required, optional) in MODES.items()]}


def validate_target(target):
    """校验读取目标的模式、文档身份和范围参数，返回独立的规范化副本。"""
    mode = target.get('mode') if isinstance(target, dict) else None
    required, optional = MODES.get(mode, ((), ())) if isinstance(mode, str) else ((), ())
    fields = {**DOCUMENT, 'intent':INTENT, **{k: POSITION[k] for k in (*required, *optional)}}
    valid = (isinstance(target, dict) and isinstance(mode, str) and mode in MODES and
             set(target) <= {'mode', *fields} and all(k in target for k in required) and
             sum(k in target for k in ('doc_id', 'doc_name')) == 1)
    if valid:
        for key, value in target.items():
            if key == 'mode':
                continue
            spec = fields[key]
            if spec['type'] == 'string':
                valid = isinstance(value, str) and bool(value.strip()) and ('enum' not in spec or value in spec['enum'])
            else:
                valid = type(value) is int and value >= spec['minimum']
            if not valid:
                break
    if not valid:
        raise ValueError(f'读取模式 {mode!r} 参数不合法；文档只能选 doc_id/doc_name 之一；'
                         f'必需字段 {list(required)}，可选定位 {list(optional)}；不能混用其他模式字段')
    if target.get('end', target.get('offset', 0)) < target.get('offset', 0):
        raise ValueError('end 不能小于 offset')
    if 'row_end' in target and target['row_end'] <= target['row_start']:
        raise ValueError('row_end 必须大于 row_start')
    result = copy.deepcopy(target)
    if 'formula_number' in result:
        number = result['formula_number'].strip()
        if (number.startswith('(') and number.endswith(')')) or (number.startswith('（') and number.endswith('）')):
            number = number[1:-1].strip()
        if not number:
            raise ValueError('formula_number 不能为空编号')
        result['formula_number'] = number
    return result


def from_legacy(args):
    """把旧 HTTP 定位参数无损转换为统一读取目标。"""
    data = {k: v for k, v in args.items() if k != 'kb_id' and v is not None}
    if 'mode' in data:
        raise ValueError('mode 只能放在 targets 内，不能混入旧定位参数')
    related = data.pop('include_related', False)
    if type(related) is not bool:
        raise ValueError('include_related 必须为布尔值')
    if 'node_id' in data:
        if related or data.get('offset', 0) != 0:
            raise ValueError('节点读取不能混用字符定位或关联读取')
        data.pop('offset', None)
        mode = 'node'
    elif related:
        if data.get('offset', 0) != 0 or any(k in data for k in ('end', 'row_start', 'row_end')):
            raise ValueError('关联展开不能与非零 offset、end 或行范围混用；请选择 range/rows 或 related 模式')
        data.pop('offset', None)
        mode = 'related'
    elif 'row_start' in data or 'row_end' in data:
        mode = 'rows'
    elif 'block_id' in data:
        mode = 'range' if 'offset' in data or 'end' in data else 'block'
        if mode == 'range':
            data.setdefault('offset', 0)
    else:
        if data.get('offset') == 0:
            data.pop('offset')
        mode = 'formula' if 'formula_number' in data else 'section' if 'section_id' in data else 'page'
    return validate_target({'mode': mode, **data})


def to_legacy(target):
    """将校验后的统一读取目标转换为旧接口接受的定位参数。"""
    data = validate_target(target)
    mode = data.pop('mode')
    data.pop('intent', None)
    if mode == 'related':
        data['include_related'] = True
    return data


def read_entry(kb_id, **args):
    """将知识库及定位包装为统一的可调用原文读取入口。"""
    if set(args)=={'targets'}:
        return {'kb_id':kb_id,'targets':[validate_target(t) for t in args['targets']]}
    return {'kb_id': kb_id, 'targets': [from_legacy(args)]}
