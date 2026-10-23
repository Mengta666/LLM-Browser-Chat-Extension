"""文档检查点、不可变解析版本和条件发布。"""

import json
from datetime import datetime, timezone
from uuid import uuid4

from agent.memory.config import CHAT_USER_ID
from storage import kb_store as KS


def now():
    """返回当前 UTC 时间，供文档处理状态记录使用。"""
    return datetime.now(timezone.utc).isoformat()


def owned(conn, kb_id, doc_id):
    """校验知识库归属并读取其未删除文档。"""
    KS._owned_kb(conn, kb_id, CHAT_USER_ID, active=True)
    doc = conn.execute("SELECT * FROM kb_docs WHERE kb_id=? AND doc_id=? AND deleted_at=''",
                       (kb_id, doc_id)).fetchone()
    if not doc:
        raise KS.KBNotFound("文档不存在或已删除")
    return dict(doc)


def document(kb_id, doc_id):
    """在事务中读取指定知识库拥有的文档元数据。"""
    with KS._transaction() as conn:
        return owned(conn, kb_id, doc_id)


def create_pdf(kb_id, doc_id, filename, path, sha256, pages, fingerprint, config):
    """检查 PDF 重复上传，保存原件元数据并创建初始解析版本。"""
    with KS._transaction() as conn:
        KS._owned_kb(conn, kb_id, CHAT_USER_ID, active=True)
        if conn.execute("SELECT 1 FROM kb_docs WHERE kb_id=? AND content_hash=? AND deleted_at=''", (kb_id, sha256)).fetchone():
            raise KS.KBConflict('相同内容的文档已存在；失败文档请恢复处理')
        conn.execute("""INSERT INTO kb_docs(doc_id,kb_id,filename,file_type,file_bytes,created_at,content_hash,
                        original_path,sha256,page_count) VALUES(?,?,?,'pdf',?,?,?,?,?,?)""",
                     (doc_id, kb_id, filename, path.stat().st_size, now(), sha256, str(path), sha256, pages))
        return _new_parse(conn, doc_id, pages, fingerprint, config)


def create_text(kb_id,doc_id,filename,file_type,path,sha256,fingerprint,config):
    """检查文本重复上传，保存原件元数据并创建文本解析版本。"""
    with KS._transaction() as conn:
        KS._owned_kb(conn,kb_id,CHAT_USER_ID,active=True)
        if conn.execute("SELECT 1 FROM kb_docs WHERE kb_id=? AND content_hash=? AND deleted_at=''",(kb_id,sha256)).fetchone():
            raise KS.KBConflict('相同内容文档已存在，失败文档请恢复处理')
        conn.execute('''INSERT INTO kb_docs(doc_id,kb_id,filename,file_type,file_bytes,created_at,content_hash,original_path,sha256)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                     (doc_id,kb_id,filename,file_type,path.stat().st_size,now(),sha256,str(path),sha256))
        return _new_parse(conn,doc_id,0,fingerprint,config)


def _new_parse(conn, doc_id, pages, fingerprint, config):
    """创建新的排队解析版本，并更新文档当前活动解析标识。"""
    parse_id = "pv_" + uuid4().hex
    conn.execute("INSERT INTO kb_parse_versions(parse_id,doc_id,fingerprint,config_json,status,page_count,created_at,updated_at) VALUES(?,?,?,?,'queued',?,?,?)",
                 (parse_id, doc_id, fingerprint, json.dumps(config, sort_keys=True), pages, now(), now()))
    conn.execute("UPDATE kb_docs SET active_parse_id=? WHERE doc_id=?", (parse_id, doc_id))
    conn.execute("UPDATE kb_docs SET status='pending',error_msg='' WHERE doc_id=? AND published_run_id=''", (doc_id,))
    return parse_id


def restart(kb_id, doc_id, fingerprint, config, *, reparse=False, reindex=False):
    """按重新解析或索引模式恢复文档任务，检查既有版本与配置兼容性。"""
    with KS._transaction() as conn:
        doc = owned(conn, kb_id, doc_id)
        if not doc['original_path']:
            raise KS.KBConflict("旧文档没有保存原文件，请重新上传；不能用片段补造完整来源")
        job = conn.execute("SELECT * FROM kb_parse_versions WHERE parse_id=?", (doc['active_parse_id'],)).fetchone()
        if job and job['status'] in ('queued', 'parsing', 'indexing'):
            raise KS.KBConflict("文档任务仍在运行")
        if reparse or not job:
            return _new_parse(conn, doc_id, doc['page_count'], fingerprint, config)
        if job['status'] == 'published' and not reindex:
            raise KS.KBConflict("文档已发布；重新识别请使用重新解析")
        if job['fingerprint'] != fingerprint and not reindex:
            raise KS.KBConflict("解析配置已变化，不能混用旧检查点；请重新解析")
        conn.execute("UPDATE kb_parse_versions SET status='queued',lease='',error='',updated_at=? WHERE parse_id=?",
                     (now(), job['parse_id']))
        conn.execute("UPDATE kb_docs SET status='pending',error_msg='' WHERE doc_id=? AND published_run_id=''", (doc_id,))
        return job['parse_id']


def claim(kb_id, doc_id, parse_id):
    """为排队解析任务分配独占租约，无法领取时返回空值。"""
    lease = uuid4().hex
    with KS._transaction() as conn:
        doc = owned(conn, kb_id, doc_id)
        if doc['active_parse_id'] != parse_id:
            return None
        cur = conn.execute("UPDATE kb_parse_versions SET status='parsing',lease=?,updated_at=? WHERE parse_id=? AND status='queued'",
                           (lease, now(), parse_id))
    return lease if cur.rowcount else None


def current(conn, kb_id, doc_id, parse_id, lease):
    """在调用方事务中校验活动解析版本及租约，返回文档和任务状态。"""
    doc = owned(conn, kb_id, doc_id)
    job = conn.execute("SELECT * FROM kb_parse_versions WHERE parse_id=?", (parse_id,)).fetchone()
    if (doc['active_parse_id'] != parse_id or not job or job['lease'] != lease
            or job['status'] not in ('parsing', 'indexing')):
        raise KS.KBConflict("任务已取消或版本已变化")
    return doc, dict(job)


def check(kb_id, doc_id, parse_id, lease):
    """在新事务内检查文档任务租约是否仍有效。"""
    with KS._transaction() as conn:
        return current(conn, kb_id, doc_id, parse_id, lease)


def stop(kb_id, doc_id):
    """停止当前文档任务并使已有解析租约失效。"""
    with KS._transaction() as conn:
        doc = owned(conn, kb_id, doc_id)
        conn.execute("UPDATE kb_parse_versions SET status='cancelled',lease='',error='用户取消，已完成页保留',updated_at=? WHERE parse_id=? AND status IN ('queued','parsing','indexing')",
                     (now(), doc['active_parse_id']))
        conn.execute("UPDATE kb_docs SET status='failed',error_msg='已取消，可恢复' WHERE doc_id=? AND published_run_id=''", (doc_id,))
        conn.execute("UPDATE kb_pages SET status='interrupted',error='用户取消' WHERE parse_id=? AND status='parsing'", (doc['active_parse_id'],))
        conn.execute("UPDATE kb_structure_publications SET status='cancelled',error='用户取消，可显式恢复',updated_at=? WHERE doc_id=? AND status IN ('queued','running')", (now(), doc_id))


def fail(parse_id, lease, error):
    """仅把匹配租约的解析任务置为失败，避免覆盖新任务。"""
    with KS._transaction() as conn:
        changed = conn.execute("UPDATE kb_parse_versions SET status='failed',lease='',error=?,updated_at=? WHERE parse_id=? AND lease=? AND status IN ('parsing','indexing')",
                     (error[:500], now(), parse_id, lease))
        if changed.rowcount:
            conn.execute("UPDATE kb_docs SET status='failed',error_msg=? WHERE active_parse_id=? AND published_run_id=''", (error[:500], parse_id))


def progress(doc):
    """汇总解析页数、失败页、索引计数和结构质量为文档进度视图。"""
    doc={**doc,'original_available':bool(doc.get('original_path')),
         'source_kind':('pdf_page' if doc['file_type']=='pdf' else 'text_span') if doc.get('published_parse_id') else 'legacy_chunk'}
    if not doc.get('active_parse_id'):
        return {**doc, 'legacy': True}
    with KS._transaction() as conn:
        row = conn.execute("SELECT * FROM kb_parse_versions WHERE parse_id=?", (doc['active_parse_id'],)).fetchone()
        run = conn.execute("SELECT * FROM kb_index_runs WHERE parse_id=? ORDER BY created_at DESC LIMIT 1", (doc['active_parse_id'],)).fetchone()
        failures = [dict(r) for r in conn.execute("SELECT page,status,error,attempts FROM kb_pages WHERE parse_id=? AND status IN ('failed','truncated','interrupted') ORDER BY page", (doc['active_parse_id'],))]
        uncertain = conn.execute('SELECT count(*) FROM kb_pages WHERE parse_id=? AND status IN (\'parsed\',\'blank\') AND truncated IS NULL', (doc['active_parse_id'],)).fetchone()[0]
        structure = conn.execute('SELECT quality_json FROM kb_structure_versions WHERE structure_id=?',(doc.get('published_structure_id',''),)).fetchone()
        quality = json.loads(structure[0]) if structure else {}
    return {**doc, 'legacy': False, 'phase': row['status'], 'completed_pages': row['completed_pages'],
            'page_count': row['page_count'], 'job_error': row['error'],
            'indexed_chunks': run['indexed_count'] if run else 0,
            'total_chunks': run['chunk_count'] if run else 0,
            'published_available': bool(doc['published_run_id']), 'failed_pages': failures,
            'failed_page_count': len(failures), 'uncertain_page_count': uncertain,
            'structure_nodes':quality.get('nodes',0),'structure_quality':quality}


def begin_page(kb_id, doc_id, parse_id, lease, page, width, height):
    """在有效租约下登记待解析页面及其渲染尺寸。"""
    with KS._transaction() as conn:
        current(conn, kb_id, doc_id, parse_id, lease)
        conn.execute("""INSERT INTO kb_pages(parse_id,page,status,width,height,coordinate_space,rotation,raw_path,warnings_json,attempts)
            VALUES(?,?,'parsing',?,?,'input_image_pixels',0,'','[]',1)
            ON CONFLICT(parse_id,page) DO UPDATE SET status='parsing',error='',attempts=kb_pages.attempts+1
            WHERE kb_pages.status NOT IN ('parsed','blank')""", (parse_id, page, width, height))


def fail_page(kb_id, doc_id, parse_id, lease, page, error, raw_path=''):
    """保存单页失败状态和可选原始响应路径。"""
    with KS._transaction() as conn:
        current(conn, kb_id, doc_id, parse_id, lease)
        conn.execute("UPDATE kb_pages SET status=?,error=?,truncated=?,raw_path=? WHERE parse_id=? AND page=? AND status='parsing'",
                     ('truncated' if error == 'parser_output_truncated' else 'failed', error[:150],
                      1 if error == 'parser_output_truncated' else None, raw_path, parse_id, page))


def save_page(kb_id, doc_id, parse_id, lease, result, raw_path, blocks, identifiers):
    """原子保存页面结果、不可变原文块及标识符，并推进页面处理状态。"""
    with KS._transaction() as conn:
        current(conn, kb_id, doc_id, parse_id, lease)
        page = result['page_number']
        if conn.execute("SELECT 1 FROM kb_pages WHERE parse_id=? AND page=? AND status IN ('parsed','blank')", (parse_id, page)).fetchone():
            return
        conn.execute("""INSERT INTO kb_pages(parse_id,page,status,printed_page,width,height,coordinate_space,rotation,raw_path,warnings_json,truncated)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(parse_id,page) DO UPDATE SET
                     status=excluded.status,printed_page=excluded.printed_page,width=excluded.width,height=excluded.height,
                     coordinate_space=excluded.coordinate_space,rotation=excluded.rotation,raw_path=excluded.raw_path,
                     warnings_json=excluded.warnings_json,truncated=excluded.truncated,error=''""",
                     (parse_id, page, 'parsed' if blocks else 'blank', result.get('printed_page'), result['width'], result['height'],
                      result['coordinate_space'], result['rotation_applied'], str(raw_path),
                      json.dumps(result.get('warnings', []), ensure_ascii=False), result.get('truncated')))
        for block in blocks:
            conn.execute("INSERT INTO kb_blocks VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (parse_id, block['block_id'], page, block['ordinal'], block['kind'], block['text'],
                          json.dumps(block['bbox']), json.dumps(block['structure'], ensure_ascii=False),
                          block.get('section_id', ''), block.get('parent_id', '')))
        conn.executemany("INSERT INTO kb_identifiers VALUES(?,?,?,?,?,?)",
                         [(parse_id, *item) for item in identifiers])
        conn.execute("UPDATE kb_parse_versions SET completed_pages=(SELECT count(*) FROM kb_pages WHERE parse_id=? AND status IN ('parsed','blank')),updated_at=? WHERE parse_id=?",
                     (parse_id, now(), parse_id))


def pages(parse_id):
    """按页码读取解析版本中的页面记录。"""
    with KS._transaction() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM kb_pages WHERE parse_id=? ORDER BY page", (parse_id,))]


def blocks(parse_id, page=None, *, structure_id=None):
    """按页序读取原始块或指定结构投影中的块，并解码位置和结构字段。"""
    with KS._transaction() as conn:
        table = 'kb_blocks' if structure_id is None else 'kb_resolved_blocks'
        condition, params = 'parse_id=?', [parse_id]
        if structure_id is not None:
            condition += ' AND structure_id=?'
            params.append(structure_id)
        if page is not None:
            condition += ' AND page=?'
            params.append(page)
        rows = conn.execute(f'SELECT * FROM {table} WHERE {condition} ORDER BY page,ordinal', params).fetchall()
    return [{**dict(r), 'structure': json.loads(r['structure_json']), 'bbox': json.loads(r['bbox_json'])} for r in rows]


def save_structure(kb_id, doc_id, parse_id, lease, blocks):
    """在尚未发布索引时保存块结构信息，保护已发布解析结果不被改写。"""
    with KS._transaction() as conn:
        current(conn, kb_id, doc_id, parse_id, lease)
        if conn.execute("SELECT 1 FROM kb_index_runs WHERE parse_id=? AND status='published'", (parse_id,)).fetchone():
            raise KS.KBConflict('已发布解析版本不可修改结构；请重新解析')
        for block in blocks:
            conn.execute('UPDATE kb_blocks SET section_id=?,parent_id=?,structure_json=? WHERE parse_id=? AND block_id=?',
                         (block['section_id'], block['parent_id'], json.dumps(block['structure'], ensure_ascii=False), parse_id, block['block_id']))


def prepare_index(kb_id, doc_id, parse_id, lease, fingerprint, config, chunks, structure_id=''):
    """恢复配置匹配的暂存索引，或登记新索引及其完整片段计划。"""
    with KS._transaction() as conn:
        doc, job = current(conn, kb_id, doc_id, parse_id, lease)
        if doc['file_type']!='pdf' and not conn.execute('SELECT 1 FROM kb_text_sources WHERE parse_id=?',(parse_id,)).fetchone():
            raise KS.KBConflict('完整文本检查点尚未保存')
        covered = [r[0] for r in conn.execute("SELECT page FROM kb_pages WHERE parse_id=? AND status IN ('parsed','blank') ORDER BY page", (parse_id,))]
        if covered != list(range(1, job['page_count'] + 1)):
            raise KS.KBConflict("解析页覆盖不完整")
        row = conn.execute("SELECT * FROM kb_index_runs WHERE parse_id=? AND fingerprint=? AND status='building' ORDER BY created_at DESC LIMIT 1",
                           (parse_id, fingerprint)).fetchone()
        conn.execute("UPDATE kb_parse_versions SET status='indexing',updated_at=? WHERE parse_id=?", (now(), parse_id))
        if row:
            return dict(row)
        run_id = uuid4().hex
        conn.execute("INSERT INTO kb_index_runs(run_id,parse_id,fingerprint,config_json,status,chunk_count,indexed_count,created_at,structure_id) VALUES(?,?,?,?,?,?,?,?,?)",
                     (run_id, parse_id, fingerprint, json.dumps(config, sort_keys=True), 'building', len(chunks), 0, now(), structure_id))
        for i, chunk in enumerate(chunks):
            conn.execute("INSERT INTO kb_chunks(run_id,chunk_idx,kind,text,token_count,node_id) VALUES(?,?,?,?,?,?)", (run_id, i, chunk['kind'], chunk['text'], chunk['token_count'], chunk.get('node_id','')))
            for src in chunk['sources']:
                conn.execute("INSERT INTO kb_chunk_sources VALUES(?,?,?,?,?,?,?,?)",
                             (run_id, i, parse_id, src['block_id'], src['start'], src['end'], src.get('row_start'), src.get('row_end')))
        return {'run_id': run_id, 'indexed_count': 0, 'chunk_count': len(chunks)}


def index_chunks(run_id, offset=0, limit=32):
    """按片段编号分页读取待向量化的索引内容。"""
    with KS._transaction() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM kb_chunks WHERE run_id=? AND chunk_idx>=? ORDER BY chunk_idx LIMIT ?", (run_id, offset, limit))]


def advance_index(kb_id, doc_id, parse_id, lease, run_id, count):
    """在有效租约下更新索引已写入片段数量。"""
    with KS._transaction() as conn:
        current(conn, kb_id, doc_id, parse_id, lease)
        conn.execute("UPDATE kb_index_runs SET indexed_count=? WHERE run_id=? AND parse_id=? AND status='building'", (count, run_id, parse_id))


def publish(kb_id, doc_id, parse_id, lease, run_id, *, review_id=None):
    """验证暂存索引完成且基线有效，原子切换文档的已发布解析、结构和索引版本。"""
    with KS._transaction() as conn:
        doc, _ = current(conn, kb_id, doc_id, parse_id, lease)
        if review_id:
            publication = conn.execute("SELECT * FROM kb_structure_publications WHERE review_id=? AND doc_id=? AND parse_id=? AND status='running'",
                                       (review_id, doc_id, parse_id)).fetchone()
            if not publication or publication['expected_run_id'] != doc['published_run_id']:
                raise KS.KBConflict('发布基线已变化，不能覆盖更新后的版本')
        row = conn.execute("SELECT * FROM kb_index_runs WHERE run_id=? AND parse_id=? AND status='building'", (run_id, parse_id)).fetchone()
        if not row or row['indexed_count'] != row['chunk_count'] or not row['chunk_count']:
            raise KS.KBConflict("索引未完成，不能发布")
        if row['structure_id']:
            structure = conn.execute("SELECT status FROM kb_structure_versions WHERE structure_id=? AND parse_id=?", (row['structure_id'],parse_id)).fetchone()
            if not structure or structure['status'] not in ('ready','published'):
                raise KS.KBConflict('结构未完成，不能发布')
            conn.execute("UPDATE kb_structure_versions SET status='published' WHERE structure_id=?", (row['structure_id'],))
        conn.execute("UPDATE kb_index_runs SET status='published' WHERE run_id=?", (run_id,))
        conn.execute("UPDATE kb_parse_versions SET status='published',lease='',error='',updated_at=? WHERE parse_id=?", (now(), parse_id))
        conn.execute("UPDATE kb_docs SET published_parse_id=?,published_run_id=?,index_run_id=?,published_structure_id=?,status='indexed',chunk_count=?,indexed_at=?,error_msg='',sync_pending=0,sync_error='' WHERE doc_id=?",
                     (parse_id, run_id, run_id, row['structure_id'], row['chunk_count'], now(), doc_id))
        if review_id:
            conn.execute("UPDATE kb_structure_publications SET status='published',structure_id=?,run_id=?,error='',updated_at=? WHERE review_id=?",
                         (row['structure_id'], run_id, now(), review_id))


def resolve_version(kb_id, doc_id, parse_id=None):
    """解析显式或默认解析版本，只允许访问已有发布索引支撑的版本。"""
    with KS._transaction() as conn:
        doc = owned(conn, kb_id, doc_id)
        version = parse_id or doc['published_parse_id']
        if not version:
            raise KS.KBConflict("旧索引未保存原文结构，不能提供精确定位；有原文件时请重新解析，否则重新上传")
        row = conn.execute("SELECT * FROM kb_parse_versions WHERE parse_id=? AND doc_id=? AND EXISTS (SELECT 1 FROM kb_index_runs r WHERE r.parse_id=kb_parse_versions.parse_id AND r.status='published')", (version, doc_id)).fetchone()
        if not row:
            raise KS.KBNotFound("解析版本未发布或不属于此文档")
        return doc, dict(row)
