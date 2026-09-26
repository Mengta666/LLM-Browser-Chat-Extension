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
    return JSONResponse(status_code=exc.status, content={'error': {'code': exc.code}})


@router.post('/v1/sessions/{chat_id}/attachments')
async def upload(request: Request, chat_id: str, client_attachment_id: str = Form(..., max_length=128),
                 file: UploadFile = File(...), first_frame: bool = Form(False)):
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
    try:
        return assets.signed_access(chat_id, identity)
    except SessionError as exc:
        return error_response(exc)


@router.get('/v1/chat-attachments/{identity}/content')
def content(identity: str, purpose: str = '', expires: int = 0, signature: str = ''):
    try:
        row = assets.verify_access(identity, purpose, expires, signature)
        return FileResponse(assets.file_path(row), media_type=row['mime'],
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    except SessionError as exc:
        return error_response(exc)


@router.delete('/v1/sessions/{chat_id}/attachments/{identity}')
def delete(chat_id: str, identity: str):
    try:
        assets.delete_unbound(chat_id, identity)
        return {'deleted': True}
    except SessionError as exc:
        return error_response(exc)


class AttachmentBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
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
            return chunks.pop(0) if chunks else await receive()
        await self.app(scope, bounded_receive, send)


class AttachmentAccessLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r'(/v1/chat-attachments/[^\s?]+)\?[^\s]*', r'\1?[redacted]', arg)
                                if isinstance(arg, str) else arg for arg in record.args)
        return True
