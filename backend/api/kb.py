"""KB REST API:建 KB / 上传文档 / 列表 / 删除。

multipart 上传:接收文件 → 存临时 → 交给 rag.kb 后台处理 → 立刻返回 doc_id + pending。
前端轮询 /kb/{kb_id}/docs/{doc_id}/status 查进度。
"""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from agent.memory.config import KB_MAX_FILE_BYTES
from rag import kb as KB
from storage import kb_store as KS

router = APIRouter(prefix="/v1/kb", tags=["kb"])


@contextmanager
def _kb_errors():
    try:
        yield
    except KS.KBNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except KS.KBConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KB.KBSyncUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    original_available: bool = False
    source_kind: str = 'legacy_chunk'
    doc_id: str
    filename: str
    file_type: str
    file_bytes: int
    chunk_count: int
    status: str                     # pending / indexed / failed
    error_msg: str
    created_at: str
    indexed_at: str
    sync_pending: bool = False
    sync_error: str = ""
    phase: str = ""
    page_count: int = 0
    completed_pages: int = 0
    job_error: str = ""
    indexed_chunks: int = 0
    total_chunks: int = 0
    published_available: bool = False
    legacy: bool = True
    failed_pages: list[dict] = Field(default_factory=list)
    failed_page_count: int = 0
    uncertain_page_count: int = 0
    published_structure_id: str = ''
    structure_nodes: int = 0
    structure_quality: dict = Field(default_factory=dict)


class DocStatusResponse(BaseModel):
    original_available: bool = False
    source_kind: str = 'legacy_chunk'
    status: str                     # pending / indexed / failed / not_found
    error_msg: str = ""
    chunk_count: int = 0
    indexed_at: str = ""
    sync_pending: bool = False
    sync_error: str = ""
    phase: str = ""
    page_count: int = 0
    completed_pages: int = 0
    job_error: str = ""
    indexed_chunks: int = 0
    total_chunks: int = 0
    published_available: bool = False
    legacy: bool = True
    failed_pages: list[dict] = Field(default_factory=list)
    failed_page_count: int = 0
    uncertain_page_count: int = 0
    published_structure_id: str = ''
    structure_nodes: int = 0
    structure_quality: dict = Field(default_factory=dict)


class UploadDocResponse(BaseModel):
    doc_id: str
    status: str
    filename: str


class TrashKBItem(BaseModel):
    kb_id: str
    name: str
    description: str
    created_at: str
    deleted_at: str
    doc_count: int
    sync_action: str = ""
    sync_error: str = ""


class HardDeleteResponse(BaseModel):
    ok: bool
    chunks_deleted: int
    docs_deleted: int


# ─── Endpoints ────────────────────────────────────────────────────


@router.post("", response_model=CreateKBResponse)
def create_kb(item: CreateKBRequest):
    """建 KB。"""
    return KB.create_kb(item.name, item.description)


@router.get("", response_model=list[KBListItem])
def list_kbs():
    """列 KB(不含软删)。"""
    return KB.list_kbs()


@router.get("/trash", response_model=list[TrashKBItem])
def list_trash():
    """列已软删 KB(回收站)。"""
    return KB.list_deleted_kbs()


@router.post("/{kb_id}/restore")
def restore_kb(kb_id: str):
    """还原 KB + 级联恢复文档 + 恢复 chunks。"""
    with _kb_errors():
        return KB.restore_kb(kb_id)


@router.delete("/{kb_id}/hard", response_model=HardDeleteResponse)
def hard_delete_kb(kb_id: str):
    """彻底删除 KB:物删 chunks + docs + KB 行。"""
    with _kb_errors():
        return KB.hard_delete_kb(kb_id)


@router.delete("/{kb_id}")
def delete_kb(kb_id: str):
    """软删 KB + 级联失效所有 doc chunks。"""
    with _kb_errors():
        return KB.delete_kb(kb_id)


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

    kb = KS.get_kb(kb_id)
    if not kb or kb["user_id"] != KB.CHAT_USER_ID or kb.get("deleted_at") or kb.get("sync_action"):
        raise HTTPException(404, f"知识库不存在: {kb_id}")

    digest, total = hashlib.sha256(), 0
    from rag.document_config import STORAGE
    staging = STORAGE / 'staging'
    staging.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir=staging) as tmp:
            tmp_path = tmp.name
            while part := await file.read(1024 * 1024):
                total += len(part)
                if total > KB_MAX_FILE_BYTES:
                    raise HTTPException(413, f"文件超过 {KB_MAX_FILE_BYTES} 字节")
                digest.update(part)
                tmp.write(part)
        if not total:
            raise HTTPException(400, "文件内容为空")
        with _kb_errors():
            result = KB.add_doc(kb_id, tmp_path, Path(filename).name, ext, content_hash=digest.hexdigest())
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        raise

    return result


@router.get("/{kb_id}/docs", response_model=list[DocListItem])
def list_docs(kb_id: str):
    """列该 KB 下所有 doc(不含软删)。"""
    with _kb_errors():
        return KB.list_docs(kb_id)


@router.delete("/{kb_id}/docs/{doc_id}")
def delete_doc(kb_id: str, doc_id: str):
    """软删 doc + 失效该 doc 所有 chunks。"""
    with _kb_errors():
        return KB.delete_doc(kb_id, doc_id)


@router.get("/{kb_id}/docs/{doc_id}/status", response_model=DocStatusResponse)
def get_doc_status(kb_id: str, doc_id: str):
    """查 doc 处理进度(pending / indexed / failed),供前端轮询。"""
    return KB.get_doc_status(kb_id, doc_id)


@router.post('/{kb_id}/docs/{doc_id}/retry')
def retry_doc(kb_id: str, doc_id: str, reparse: bool = False, reindex: bool = False):
    with _kb_errors():
        return KB.retry_document(kb_id, doc_id, reparse=reparse, reindex=reindex)


@router.post('/{kb_id}/docs/{doc_id}/cancel')
def cancel_doc(kb_id: str, doc_id: str):
    with _kb_errors():
        return KB.cancel_document(kb_id, doc_id)


@router.get('/{kb_id}/docs/{doc_id}/outline')
def document_outline(kb_id: str, doc_id: str, parse_id: str | None = None,
                     page: int | None = Query(None, ge=1), offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50),
                     section_id: str | None = None, view: str = 'blocks', structure_id: str | None = None,
                     index_run_id: str | None = None, node_id: str | None = None, depth: int = Query(1,ge=1,le=3)):
    from rag.document_retrieval import outline
    with _kb_errors():
        return outline(kb_id, doc_id, parse_id, page, offset, limit, section_id, view,structure_id,index_run_id,node_id,depth)


@router.get('/{kb_id}/docs/{doc_id}/read')
def document_read(kb_id: str, doc_id: str, block_id: str | None = None, parse_id: str | None = None,
                  offset: int = Query(0, ge=0), end: int | None = Query(None, ge=0),
                  page: int | None = Query(None, ge=1), section_id: str | None = None, formula_number: str | None = None,
                  include_related: bool = False, row_start: int | None = Query(None, ge=0), row_end: int | None = Query(None, ge=1),
                  structure_id: str | None = None,index_run_id: str | None = None,node_id: str | None = None,
                  cursor: int | None = Query(None,ge=0),block_offset: int | None = Query(None,ge=0)):
    from rag.document_retrieval import read
    from search.kb_documents import trim_row
    from search.kb_protocol import from_legacy, to_legacy
    with _kb_errors():
        locator = dict(doc_id=doc_id, parse_id=parse_id, page=page, block_id=block_id, end=end,
                       section_id=section_id, formula_number=formula_number, include_related=include_related,
                       row_start=row_start, row_end=row_end)
        locator.update(structure_id=structure_id,index_run_id=index_run_id,node_id=node_id,cursor=cursor,block_offset=block_offset)
        if offset or row_start is None:
            locator['offset'] = offset
        target = from_legacy(locator)
        if target['mode'] != 'node' and (cursor is not None or block_offset is not None):
            from search.kb_documents import execute
            rows, meta = execute('kb_read_document', {'kb_id':kb_id,'targets':[target]})
            if meta['items'][0]['outcome'] == 'error':
                raise KS.KBConflict(meta['items'][0]['error'])
            kept, remaining = [], 16000
            for row in rows:
                if kept:
                    remaining -= 1
                if remaining <= 0:
                    break
                part = trim_row(row, remaining) if len(row['content']) > remaining else row
                kept.append(part)
                remaining -= len(part['content'])
            return {'kb_id':kb_id, 'doc_id':doc_id, 'content':'\n'.join(r['content'] for r in kept),
                    'segments':[s for r in kept for s in r['segments']],
                    'locations':[s for r in kept for s in r['locations']],
                    'next_read':kept[-1].get('next_read') if kept else None,
                    'parse_id':kept[0]['parse_id'] if kept else parse_id,
                    'warnings':list(dict.fromkeys(w for r in kept for w in r.get('warnings',[])))}
        result = read(kb_id=kb_id, **to_legacy(target))
        if len(result['content']) > 16000:
            result = trim_row(result, 16000)
        from storage.kb_text_store import annotate
        return annotate(result)


@router.get('/{kb_id}/docs/{doc_id}/text')
def document_text(kb_id: str, doc_id: str, parse_id: str, offset: int = Query(0,ge=0),
                  end: int | None = Query(None,ge=0)):
    from storage import kb_document_store as DS, kb_text_store as TS
    with _kb_errors():
        doc, version=DS.resolve_version(kb_id,doc_id,parse_id)
        if doc['file_type']=='pdf':
            raise ValueError('PDF 请使用页块读取')
        text,starts=TS.source(version['parse_id'])
        requested=len(text) if end is None else end
        if not 0<=offset<=requested<=len(text):
            raise ValueError('原文字符范围超出边界')
        stop=min(requested,offset+16000)
        return {'doc_id':doc_id,'parse_id':parse_id,'content':text[offset:stop],'length':len(text),
                **TS.location(starts,{'source_start':0},offset,stop),
                'next_offset':stop if stop<requested else None,'truncated':stop<requested}


@router.get('/{kb_id}/docs/{doc_id}/file')
def document_file(kb_id: str, doc_id: str):
    from storage import kb_document_store as DS
    from rag.document_config import document_path
    with _kb_errors():
        doc = DS.document(kb_id, doc_id)
        path = document_path(doc_id, 'original.' + doc['file_type'])
        if not doc['original_path'] or str(path) != doc['original_path'] or not path.is_file():
            raise KS.KBNotFound('完整原文件未保留')
        return FileResponse(path, media_type='application/pdf' if doc['file_type']=='pdf' else 'application/octet-stream',
                            filename=doc['filename'], content_disposition_type='inline' if doc['file_type']=='pdf' else 'attachment',
                            headers={'X-Content-Type-Options':'nosniff'})


@router.get('/{kb_id}/docs/{doc_id}/pages/{page}.png')
def document_page(kb_id: str, doc_id: str, page: int, parse_id: str):
    import fitz
    from storage import kb_document_store as DS
    from rag.document_config import document_path, MAX_PIXELS
    with _kb_errors():
        doc, version = DS.resolve_version(kb_id, doc_id, parse_id)
        saved = next((p for p in DS.pages(version['parse_id']) if p['page'] == page), None)
        if not saved or saved['width'] * saved['height'] > MAX_PIXELS + 20000:
            raise KS.KBNotFound('没有可显示的原页')
        with fitz.open(document_path(doc_id, 'original.pdf')) as pdf:
            source = pdf[page - 1]
            pix = source.get_pixmap(matrix=fitz.Matrix(saved['width'] / source.rect.width, saved['height'] / source.rect.height),
                                    colorspace=fitz.csRGB, alpha=False)
            return Response(pix.tobytes('png'), media_type='image/png', headers={'Cache-Control': 'no-store'})
