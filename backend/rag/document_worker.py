"""后端持久化 PDF 任务。远端只处理有限单页，不维护第二份任务状态。"""

import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path

import fitz
import requests
from PIL import Image, ImageStat
from io import BytesIO

from agent.memory.config import CHAT_USER_ID, MEMORY_TYPE_KB_CHUNK
from observability.logger import get_logger
from rag import document_config as C
from rag.document_structure import EmbeddingCounter, build_chunks, fingerprint, normalize_page
from rag.document_layout import link_document, printed_page
from rag.embedder import embed_texts
from storage import kb_document_store as DS, kb_store as KS

_slots = threading.BoundedSemaphore(max(1, C.CONCURRENCY))
_log = get_logger('kb')


class TruncatedPage(ValueError):
    def __init__(self, result):
        """携带被截断的单页解析结果，供失败时留存诊断材料。"""
        super().__init__('parser_output_truncated')
        self.result = result


def parse_config():
    """返回决定 PDF 解析版本兼容性的管线、渲染和协议配置。"""
    return {'schema': 1, 'normalizer': 4, 'parser_url': C.PARSER_URL, 'dpi': C.DPI,
            'max_pixels': C.MAX_PIXELS, 'pipeline': 'PaddleOCR-VL-1.6', 'layout': 'PP-DocLayoutV3'}


def inspect_pdf(path):
    """检查 PDF 是否可读、未加密且页数受限，返回允许处理的页面数量。"""
    if not C.PARSER_URL:
        raise ValueError('请先配置 KB_PDF_PARSER_URL；不会回退到丢失页块结构的旧解析')
    try:
        # Windows 上损坏文件构造异常可能暂留 MuPDF 文件句柄；有界缓冲校验不锁定待清理文件。
        from agent.memory.config import KB_MAX_FILE_BYTES
        with Path(path).open('rb') as source:
            data = source.read(KB_MAX_FILE_BYTES + 1)
        if len(data) > KB_MAX_FILE_BYTES:
            raise ValueError('PDF 大小超限')
        with fitz.open(stream=data, filetype='pdf') as pdf:
            if not pdf.is_pdf or pdf.needs_pass or not 0 < len(pdf) <= C.MAX_PAGES:
                raise ValueError('PDF 无法读取、已加密或页数超限')
            return len(pdf)
    except RuntimeError as exc:
        raise ValueError('PDF 无法打开') from exc


def parse_page(image, page, check, deadline):
    """调用远端单页解析并校验图像身份、坐标和块内容，按期限重试临时网络失败。"""
    with requests.Session() as session:
        session.trust_env = False
        for attempt in range(3):
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('document_task_timeout')
            try:
                with session.post(C.PARSER_URL + '/v1/parse/page', params={'page_number': page}, data=image,
                                  headers={'Content-Type': 'image/png', 'Authorization': 'Bearer ' + C.PARSER_KEY},
                                  timeout=(5, min(C.PAGE_TIMEOUT, remaining)), stream=True, allow_redirects=False) as response:
                    if response.status_code in (429, 502, 503, 504):
                        raise requests.ConnectionError('parser_temporarily_unavailable')
                    response.raise_for_status()
                    data = bytearray()
                    for part in response.iter_content(65536):
                        check()
                        data.extend(part)
                        if len(data) > 8 * 1024 * 1024:
                            raise ValueError('parser_response_too_large')
                    result = json.loads(data)
                if (result.get('status') != 'ok' or result.get('schema_version') != 1 or result.get('page_number') != page
                        or result.get('pipeline') != 'PaddleOCR-VL-1.6'
                        or result.get('layout_model') != 'PP-DocLayoutV3'
                        or result.get('image_sha256') != hashlib.sha256(image).hexdigest()
                        or not isinstance(result.get('blocks'), list)):
                    raise ValueError('parser_protocol_mismatch')
                with Image.open(BytesIO(image)) as check_image:
                    if (result.get('width'), result.get('height')) != check_image.size:
                        raise ValueError('parser_image_size_mismatch')
                for block in result['blocks']:
                    bbox = block.get('bbox')
                    if (not isinstance(block.get('text'), str) or not isinstance(bbox, list) or len(bbox) != 4
                            or not all(type(v) in (float, int) and math.isfinite(v) for v in bbox)
                            or not 0 <= bbox[0] <= bbox[2] <= result['width']
                            or not 0 <= bbox[1] <= bbox[3] <= result['height']):
                        raise ValueError('parser_block_invalid')
                if not result['blocks']:
                    with Image.open(BytesIO(image)) as check_image:
                        if any(low < 250 for low, _ in ImageStat.Stat(check_image).extrema):
                            raise ValueError('nonblank_page_without_blocks')
                if result.get('truncated') is True:
                    raise TruncatedPage(result)
                if result.get('coordinate_space') != 'input_image_pixels' or result.get('rotation_applied') != 0:
                    raise ValueError('unsupported_parser_coordinates')
                if result.get('truncated') is None:
                    result.setdefault('warnings', []).append('上游未提供可靠截断判定，解析覆盖不等于识别无遗漏')
                if any(b.get('type') == 'image' and not b['text'].strip() for b in result['blocks']):
                    result.setdefault('warnings', []).append('页面包含未转为文字的图片区域；不能凭文字检索判断图中内容或箭头关系')
                return result
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)


def run(kb_id, doc_id, parse_id, finish, publish_lock):
    """从检查点继续 PDF 页面解析、结构构建及索引发布，并记录可恢复的失败。"""
    lease, current_page = None, None
    try:
        with _slots:
            lease = DS.claim(kb_id, doc_id, parse_id)
            if not lease:
                return
            deadline = time.monotonic() + C.TASK_TIMEOUT

            def check():
                """检查文档任务期限和租约，返回仍有效的文档及解析状态。"""
                if time.monotonic() >= deadline:
                    raise TimeoutError('document_task_timeout')
                return DS.check(kb_id, doc_id, parse_id, lease)

            doc, _ = check()
            source = C.document_path(doc_id, 'original.pdf')
            if str(source) != doc['original_path']:
                raise ValueError('invalid_source_path')
            with source.open('rb') as original:
                if hashlib.file_digest(original, 'sha256').hexdigest() != doc['sha256']:
                    raise ValueError('original_file_changed')
            saved_pages = [p for p in DS.pages(parse_id) if p['status'] in ('parsed', 'blank')]
            completed = {p['page'] for p in saved_pages}
            parser_identity = None
            if saved_pages:
                with Path(saved_pages[0]['raw_path']).open(encoding='utf-8') as saved:
                    parser_identity = json.load(saved).get('versions')
            with fitz.open(source) as pdf:
                for number in range(1, len(pdf) + 1):
                    check()
                    if number in completed:
                        continue
                    current_page = number
                    page = pdf[number - 1]
                    scale = min(C.DPI / 72, math.sqrt(C.MAX_PIXELS / max(1, page.rect.width * page.rect.height)))
                    DS.begin_page(kb_id, doc_id, parse_id, lease, number, math.ceil(page.rect.width * scale), math.ceil(page.rect.height * scale))
                    image = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False).tobytes('png')
                    if len(image) > 8 * 1024 * 1024:
                        raise ValueError('rendered_page_too_large')
                    result = parse_page(image, number, check, deadline)
                    # 恢复任务时允许复用已存页面，但同一解析版本不能混入不同解析器的输出。
                    if parser_identity is not None and result.get('versions') != parser_identity:
                        raise ValueError('parser_version_changed_reparse_required')
                    parser_identity = result.get('versions')
                    result['printed_page'] = printed_page(result['blocks'])
                    blocks, ids = normalize_page(result)
                    check()
                    raw = C.document_path(doc_id, parse_id, f'page-{number}-{lease}.json')
                    raw.parent.mkdir(parents=True, exist_ok=True)
                    with raw.open('x', encoding='utf-8') as output:
                        json.dump(result, output, ensure_ascii=False)
                    DS.save_page(kb_id, doc_id, parse_id, lease, result, raw, blocks, ids)
                    current_page = None
                    _log.info('kb_pdf_page', data={'doc_id': doc_id, 'page': number, 'blocks': len(blocks),
                                                 'elapsed_ms': result.get('elapsed_ms'), 'truncated': result.get('truncated')})
                from rag.document_features import pdf_features
                features, bookmarks = pdf_features(pdf, DS.pages(parse_id))
            check()
            from rag import document_hierarchy as H
            from storage import kb_structure_store as HS
            config = {'chunker': 5, 'tokens': C.CHUNK_TOKENS, 'model': os.getenv('EMBEDDING_MODEL'),
                      'base_url': os.getenv('EMBEDDING_BASE_URL'), 'tokenizer_url': C.TOKENIZER_URL}
            stored_blocks = DS.blocks(parse_id)
            if sum(len(b['text']) for b in stored_blocks) > 10_000_000:
                raise ValueError('document_text_limit_exceeded')
            structure_config = {'version': H.VERSION, 'max_chars': C.UNIT_MAX_CHARS,
                                'projection_version': 1, 'features_version': 1, 'preview_chars': C.NODE_PREVIEW_CHARS,
                                'proof_passage_chars':C.PROOF_PASSAGE_CHARS, 'bookmarks': bookmarks}
            from storage import kb_structure_review_store as RS
            with KS._transaction() as conn:
                retained = conn.execute("SELECT review_id FROM kb_structure_publications WHERE structure_id=? AND status='published'", (doc['published_structure_id'],)).fetchone()
            review = RS.get(kb_id,doc_id,retained['review_id']) if retained and doc['published_parse_id']==parse_id else None
            structure = ({'structure_id':doc['published_structure_id'],'status':'published'} if review else
                         HS.prepare(kb_id, doc_id, parse_id, lease, structure_config))
            if structure['status'] == 'building':
                stored_blocks = link_document(stored_blocks, DS.pages(parse_id), bookmarks, features)
                nodes, relations, quality = H.build(stored_blocks, title=doc['filename'], bookmarks=bookmarks,
                    max_chars=C.UNIT_MAX_CHARS, preview_chars=C.NODE_PREVIEW_CHARS,
                    proof_passage_chars=C.PROOF_PASSAGE_CHARS, check=check)
                HS.save(kb_id, doc_id, parse_id, lease, structure['structure_id'], nodes, relations, quality, stored_blocks)
            stored_blocks = DS.blocks(parse_id, structure_id=structure['structure_id'])
            config['structure_id'] = structure['structure_id']
            counter = EmbeddingCounter(check)
            try:
                if review:
                    from rag import structure_projection as P
                    nodes, _ = HS.load(structure['structure_id'])
                    chunks = P.chunks(stored_blocks,review['result'],nodes,doc['filename'],counter)
                    config['range_chunker'] = P.VERSION
                else:
                    chunks = build_chunks(stored_blocks, doc['filename'], counter)
                chunks.extend(HS.navigation_chunks(structure['structure_id'], counter.count, C.CHUNK_TOKENS))
            finally:
                counter.session.close()
            if not chunks:
                raise ValueError('no_indexable_text')
            publish_index(kb_id, doc_id, parse_id, lease, doc, config, chunks, check, deadline, publish_lock)
    except Exception as exc:
        if lease:
            detail = str(exc)[:150] if isinstance(exc, (ValueError, KS.KBConflict, TimeoutError)) else type(exc).__name__
            if current_page is not None:
                try:
                    failed_raw = ''
                    if isinstance(exc, TruncatedPage):
                        DS.check(kb_id, doc_id, parse_id, lease)
                        raw_path = C.document_path(doc_id, parse_id, f'page-{current_page}-{lease}-truncated.json')
                        try:
                            raw_path.parent.mkdir(parents=True, exist_ok=True)
                            with raw_path.open('x', encoding='utf-8') as output:
                                json.dump(exc.result, output, ensure_ascii=False)
                            failed_raw = raw_path
                        except OSError:
                            _log.warn('kb_failed_page_archive_unavailable', data={'doc_id': doc_id, 'page': current_page})
                    DS.fail_page(kb_id, doc_id, parse_id, lease, current_page, detail, str(failed_raw))
                except KS.KBConflict:
                    pass
            DS.fail(parse_id, lease, f'处理失败：{detail}；原文件和检查点已保留')
        _log.warn('kb_document_failed', data={'doc_id': doc_id, 'parse_id': parse_id, 'error_type': type(exc).__name__,
                                            'error_code': str(exc)[:150] if isinstance(exc, (ValueError, TimeoutError)) else ''})
    finally:
        finish()


def publish_index(kb_id, doc_id, parse_id, lease, doc, config, chunks, check, deadline, publish_lock, *, review_id=None):
    """分批写入暂存向量并推进检查点，完整校验后切换文档已发布索引。"""
    from rag import kb_vectors as V
    run_id, published = None, False
    try:
        run_info = DS.prepare_index(kb_id, doc_id, parse_id, lease, fingerprint(config), config, chunks, config['structure_id'])
        run_id = run_info['run_id']
        count = run_info['indexed_count']
        while count < run_info['chunk_count']:
            check()
            batch = DS.index_chunks(run_id, count)
            vectors = embed_texts([c['text'] for c in batch], batch_size=32,
                                 timeout=max(.1, min(120, deadline - time.monotonic())))
            if len(vectors) != len(batch):
                raise ValueError('embedding_count_mismatch')
            # 分批写入暂不可检索的向量，全部完成后才切换文档的已发布版本。
            items = [{'content': c['text'], 'vector': vector, 'memory_type': MEMORY_TYPE_KB_CHUNK,
                      'user_id': CHAT_USER_ID, 'kb_id': kb_id, 'doc_id': doc_id, 'index_run_id': run_id,
                      'source': doc['filename'], 'chunk_idx': c['chunk_idx'], 'chunk_id': c['chunk_idx'],
                      'valid': False} for c, vector in zip(batch, vectors)]
            V.batch_insert_memories(items, before_batch=check)
            count += len(batch)
            DS.advance_index(kb_id, doc_id, parse_id, lease, run_id, count)
        with publish_lock:
            check()
            if not V.kb_index_complete(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id, index_run_id=run_id, expected=count):
                raise ValueError('incomplete_vector_index')
            V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id, index_run_id=run_id, valid=True)
            if review_id:
                DS.publish(kb_id, doc_id, parse_id, lease, run_id, review_id=review_id)
            else:
                DS.publish(kb_id, doc_id, parse_id, lease, run_id)
            published = True
            # 先发布新版本再停用旧向量；旧向量清理失败不撤销已经成功的发布。
            if doc['published_run_id'] and doc['published_run_id'] != run_id:
                try:
                    V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id,
                                         index_run_id=doc['published_run_id'], valid=False)
                except Exception:
                    _log.warn('kb_retired_index_cleanup_pending', data={'doc_id': doc_id})
    finally:
        if run_id and not published:
            try:
                V.set_kb_chunks_valid(user_id=CHAT_USER_ID, kb_id=kb_id, doc_id=doc_id, index_run_id=run_id, valid=False)
            except Exception:
                _log.warn('kb_staging_cleanup_pending', data={'doc_id': doc_id})
