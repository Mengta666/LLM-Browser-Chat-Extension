"""KB REST API:建 KB / 上传文档 / 列表 / 删除。

multipart 上传:接收文件 → 存临时 → 交给 rag.kb 后台处理 → 立刻返回 doc_id + pending。
前端轮询 /kb/{kb_id}/docs/{doc_id}/status 查进度。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from agent.memory.config import KB_MAX_FILE_BYTES
from rag import kb as KB

router = APIRouter(prefix="/v1/kb", tags=["kb"])


# ─── Models ───────────────────────────────────────────────────────


class CreateKBRequest(BaseModel):
    name: str
    description: str = ""


class CreateKBResponse(BaseModel):
    kb_id: str
    name: str
    description: str
    created_at: str


class KBListItem(BaseModel):
    kb_id: str
    name: str
    description: str
    created_at: str
    updated_at: str


class DocListItem(BaseModel):
    doc_id: str
    filename: str
    file_type: str
    file_bytes: int
    chunk_count: int
    status: str                     # pending / indexed / failed
    error_msg: str
    created_at: str
    indexed_at: str


class DocStatusResponse(BaseModel):
    status: str                     # pending / indexed / failed / not_found
    error_msg: str = ""
    chunk_count: int = 0
    indexed_at: str = ""


class UploadDocResponse(BaseModel):
    doc_id: str
    status: str
    filename: str


# ─── Endpoints ────────────────────────────────────────────────────


@router.post("", response_model=CreateKBResponse)
def create_kb(item: CreateKBRequest):
    """建 KB。"""
    return KB.create_kb(item.name, item.description)


@router.get("", response_model=list[KBListItem])
def list_kbs():
    """列 KB(不含软删)。"""
    return KB.list_kbs()


@router.delete("/{kb_id}")
def delete_kb(kb_id: str):
    """软删 KB + 级联失效所有 doc chunks。"""
    KB.delete_kb(kb_id)
    return {"ok": True}


@router.post("/{kb_id}/docs", response_model=UploadDocResponse)
async def upload_doc(
    kb_id: str,
    file: Annotated[UploadFile, File(description="PDF / Markdown / 纯文本")],
):
    """上传文档,立刻返回 doc_id + pending,后台处理。

    前端预检:扩展名 / 大小 ≤ 50MB。
    后端再校验一次,防绕过。
    """
    # 1. 校验扩展名
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ("pdf", "md", "markdown", "txt"):
        raise HTTPException(400, f"不支持的文件类型: {ext}")

    # TODO: 缺少文档安全校验，避免恶意文档

    # 2. 读文件 + 校验大小
    content = await file.read()
    if len(content) > KB_MAX_FILE_BYTES:
        raise HTTPException(413, f"文件过大: {len(content)} 字节,上限 {KB_MAX_FILE_BYTES}")
    if len(content) == 0:
        raise HTTPException(400, "文件内容为空")

    # 3. KB 存在性校验
    from storage import kb_store as KS
    kb = KS.get_kb(kb_id)
    if not kb or kb.get("deleted_at"):
        raise HTTPException(404, f"知识库不存在: {kb_id}")

    # 4. 文件 hash 去重
    import hashlib
    file_hash = hashlib.md5(content).hexdigest()
    dup = KS.find_duplicate_doc(kb_id, file_hash)
    if dup:
        raise HTTPException(409, f"该知识库已存在相同内容的文档: {dup['filename']} (状态: {dup['status']})")

    # 5. 存临时文件
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    # 6. 交给 rag.kb 后台处理(daemon 线程)
    try:
        result = KB.add_doc(kb_id, tmp_path, filename, ext, content_hash=file_hash)
    finally:
        pass

    return result


@router.get("/{kb_id}/docs", response_model=list[DocListItem])
def list_docs(kb_id: str):
    """列该 KB 下所有 doc(不含软删)。"""
    return KB.list_docs(kb_id)


@router.delete("/{kb_id}/docs/{doc_id}")
def delete_doc(kb_id: str, doc_id: str):
    """软删 doc + 失效该 doc 所有 chunks。"""
    KB.delete_doc(kb_id, doc_id)
    return {"ok": True}


@router.get("/{kb_id}/docs/{doc_id}/status", response_model=DocStatusResponse)
def get_doc_status(kb_id: str, doc_id: str):
    """查 doc 处理进度(pending / indexed / failed),供前端轮询。"""
    return KB.get_doc_status(kb_id, doc_id)
