"""结构字段契约供解码约束与本地校验共用，语义仍由协议层校验。"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ProtocolError(ValueError):
    def __init__(self, code, details):
        """携带协议错误码和字段路径等细节，供模型修复输出。"""
        super().__init__(code)
        self.details = details


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ReviewItem(Contract):
    anchors: Annotated[list[str], Field(min_length=1)]
    role: Literal['document_title', 'section_heading', 'toc', 'front_matter', 'header', 'footer', 'unknown']
    parent_section: str | None
    section_level: Annotated[int, Field(ge=1, le=20)] | None
    status: Literal['inferred', 'unresolved']
    evidence_refs: Annotated[list[str], Field(min_length=1, max_length=8)]
    reason: Annotated[str, Field(min_length=1, max_length=200)]


class ReviewIssue(Contract):
    anchors: Annotated[list[str], Field(min_length=1)]
    note: Annotated[str, Field(max_length=200)]


class RoleOutput(Contract):
    window_id: str
    items: list[ReviewItem]
    issues: list[ReviewIssue]


class GlobalEdit(Contract):
    target_anchor: str
    replacement: ReviewItem | None


class GlobalOutput(Contract):
    window_id: str
    complete: bool
    edits: list[GlobalEdit]
    issues: list[ReviewIssue]


def validate_shape(value, payload):
    """使用共享数据模型检查字段契约，并把校验失败转换为结构化协议错误。"""
    global_review = payload['window_id'] == 'global' and 'candidate_items' in payload
    model = GlobalOutput if global_review else RoleOutput
    try:
        model.model_validate(value)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        fields = set().union(*(m.model_fields for m in (RoleOutput, ReviewItem, ReviewIssue, GlobalEdit, GlobalOutput)))
        # 未知字段名也可能含文档文字或凭证；仅记录可信字段路径和错误类型。
        safe = [{'path': '/' + '/'.join(str(p) if type(p) is int or p in fields else '<extra>' for p in e['loc']),
                 'kind': e['type']} for e in errors[:6]]
        loc = errors[0]['loc']
        code = ('review_global_schema' if global_review else 'review_item_schema' if loc[:1] == ('items',) and len(loc) > 1
                else 'review_issue_schema' if loc[:1] == ('issues',) else 'review_schema')
        raise ProtocolError(code, {'errors': safe, 'error_count': len(errors),
                                  'unexpected_fields': sum(e['type'] == 'extra_forbidden' for e in errors),
                                  'expected_window_id': payload['window_id']}) from None
    if value['window_id'] != payload['window_id']:
        raise ProtocolError('review_global_schema' if global_review else 'review_schema',
                            {'errors': [{'path': '/window_id', 'kind': 'wrong_window'}],
                             'expected_window_id': payload['window_id']})


def response_format(payload, mode):
    """根据审阅阶段和解码模式生成 JSON 输出约束。"""
    if mode == 'json_object':
        return {'type': 'json_object'}
    if mode != 'json_schema':
        raise ValueError('review_response_format')
    global_review = payload['window_id'] == 'global'
    schema = (GlobalOutput if global_review else RoleOutput).model_json_schema()
    schema['properties']['window_id']['const'] = payload['window_id']
    schema['properties']['issues']['maxItems'] = len(payload['anchors'])
    schema['properties']['edits' if global_review else 'items']['maxItems'] = payload['edit_limit'] if global_review else len(payload['anchors'])
    return {'type': 'json_schema', 'json_schema': {'name': 'structure_global' if global_review else 'structure_roles',
                                                 'strict': True, 'schema': schema}}
