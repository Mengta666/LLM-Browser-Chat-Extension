"""单图上传、临时访问授权及受控下载。"""

import logging
import re

from fastapi import APIRouter, File, Form, UploadFile, Request
from starlette.datastructures import UploadFile as UploadedFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from storage import chat_attachments as assets
from storage.chat_store import SessionError

router = APIRouter(tags=['Chat 附件'])


def error_response(exc):
    """将附件业务异常转换为带标准错误码的 HTTP 响应。"""
    return JSONResponse(status_code=exc.status, content={'error': {'code': exc.code}})


@router.post('/v1/sessions/{chat_id}/attachments')
async def upload(request: Request, chat_id: str, client_attachment_id: str = Form(..., max_length=128),
                 file: UploadFile = File(...), first_frame: bool = Form(False)):
    """校验单图上传表单和大小，再交由存储层登记会话附件。"""
    try:
        assets.require_enabled()
        form = await request.form()
        if sum(isinstance(value, UploadedFile) for _, value in form.multi_items()) != 1:
            raise SessionError('attachment_count_exceeded', 422)
        data = await file.read(assets.MAX_BYTES + 1)
        return await run_in_threadpool(assets.upload, chat_id, client_attachment_id, file.filename, data, first_frame)
    except SessionError as exc:
        return error_response(exc)
    finally:
        await file.close()


@router.post('/v1/sessions/{chat_id}/attachments/{identity}/access')
def access(chat_id: str, identity: str):
    """为当前会话拥有的附件签发临时访问路径。"""
    try:
        return assets.signed_access(chat_id, identity)
    except SessionError as exc:
        return error_response(exc)


@router.get('/v1/chat-attachments/{identity}/content')
def content(identity: str, purpose: str = '', expires: int = 0, signature: str = ''):
    """验证附件访问签名并返回文件，禁止缓存及内容类型嗅探。"""
    try:
        row = assets.verify_access(identity, purpose, expires, signature)
        return FileResponse(assets.file_path(row), media_type=row['mime'],
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    except SessionError as exc:
        return error_response(exc)


@router.delete('/v1/sessions/{chat_id}/attachments/{identity}')
def delete(chat_id: str, identity: str):
    """删除尚未绑定消息的临时附件，已使用的附件由存储层拒绝删除。"""
    try:
        assets.delete_unbound(chat_id, identity)
        return {'deleted': True}
    except SessionError as exc:
        return error_response(exc)


class AttachmentBodyLimit:
    def __init__(self, app):
        """保存下游 ASGI 应用，供附件请求体检查后继续调用。"""
        self.app = app

    async def __call__(self, scope, receive, send):
        """对附件上传请求累计检查请求体大小，超限时在表单解析前返回错误。"""
        if (scope['type'] != 'http' or scope['method'] != 'POST'
                or not re.fullmatch(r'/v1/sessions/[^/]+/attachments/?', scope['path'])):
            return await self.app(scope, receive, send)
        # 在 multipart 解析前限制总量；缓冲量严格受 12 MiB 上限约束。
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > assets.BODY_LIMIT:
                return await error_response(SessionError('attachment_body_too_large', 413))(scope, receive, send)
            chunks.append(message)
            if not message.get('more_body'):
                break
        async def bounded_receive():
            """按原顺序重放已检查的请求体分块，再读取后续 ASGI 消息。"""
            return chunks.pop(0) if chunks else await receive()
        await self.app(scope, bounded_receive, send)


class AttachmentAccessLogFilter(logging.Filter):
    def filter(self, record):
        """脱敏访问日志中的附件签名查询参数，避免凭证进入日志。"""
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r'(/v1/chat-attachments/[^\s?]+)\?[^\s]*', r'\1?[redacted]', arg)
                                if isinstance(arg, str) else arg for arg in record.args)
        return True
