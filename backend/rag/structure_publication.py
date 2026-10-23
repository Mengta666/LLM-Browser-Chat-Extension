"""显式发布已完成的结构建议；原文与旧快照保留，失败可显式恢复。"""
import json
import os
import time
from functools import partial

from rag import document_config as C, structure_projection as P
from rag.document_structure import EmbeddingCounter, fingerprint
from storage import kb_store as KS, kb_document_store as DS, kb_structure_store as HS, kb_structure_review_store as RS


def get(kb_id, doc_id, review_id):
    """查询结构建议的发布进度，并判断发布版本是否仍是文档当前版本。"""
    with KS._transaction() as conn:
        doc = DS.owned(conn, kb_id, doc_id)
        RS.owned(conn, kb_id, doc_id, review_id)
        row = conn.execute('SELECT * FROM kb_structure_publications WHERE review_id=?', (review_id,)).fetchone()
        if not row:
            return {'review_id': review_id, 'status': 'not_started', 'published': False}
        result = dict(row)
        result.pop('config_json')
        result.update(published=row['status'] == 'published', current=row['run_id'] == doc['published_run_id'])
        run = conn.execute('SELECT indexed_count,chunk_count FROM kb_index_runs WHERE structure_id=? ORDER BY created_at DESC LIMIT 1',
                           (row['structure_id'],)).fetchone()
        result.update(dict(run) if run else {})
        return result


def start(kb_id, doc_id, review_id, expected_run_id):
    """检查审阅结果和预期发布基线，登记发布任务并启动后台索引。"""
    from rag import kb as KB
    with KB._kb_lock(kb_id):
        if KB._has_worker(kb_id, doc_id):
            raise KS.KBConflict('文档任务正在运行，请查询进度')
        review = RS.get(kb_id, doc_id, review_id)
        if review['status'] != 'completed':
            raise KS.KBConflict('结构分析未完成，不能发布')
        blocks = DS.blocks(review['parse_id'], structure_id=review['structure_id'])
        P.validate_review(review['result'], blocks)
        config = {'projection_version': P.VERSION, 'hierarchy_version': 8, 'max_chars': C.UNIT_MAX_CHARS,
                  'preview_chars': C.NODE_PREVIEW_CHARS, 'proof_passage_chars': C.PROOF_PASSAGE_CHARS,
                  'chunk_tokens': C.CHUNK_TOKENS, 'embedding_model': os.getenv('EMBEDDING_MODEL'),
                  'embedding_base_url': os.getenv('EMBEDDING_BASE_URL'), 'tokenizer_url': C.TOKENIZER_URL}
        result_hash = fingerprint(review['result'])
        with KS._transaction() as conn:
            doc = DS.owned(conn, kb_id, doc_id)
            old = conn.execute('SELECT * FROM kb_structure_publications WHERE review_id=?', (review_id,)).fetchone()
            if old and old['status'] == 'published':
                pass
            else:
                # 审阅结果只适用于当时的解析和结构基线，不能覆盖审阅期间发布的新版本。
                if (doc['published_run_id'] != expected_run_id or doc['published_parse_id'] != review['parse_id']
                        or doc['active_parse_id'] != review['parse_id'] or doc['published_structure_id'] != review['structure_id']):
                    raise KS.KBConflict('发布基线已变化，请重新审阅当前版本，不能覆盖其他更新')
                if old and (old['expected_run_id'] != expected_run_id or old['result_hash'] != result_hash
                            or json.loads(old['config_json']) != config):
                    raise KS.KBConflict('发布检查点配置或结果已改变，不能混用')
                parse = conn.execute('SELECT status FROM kb_parse_versions WHERE parse_id=?', (review['parse_id'],)).fetchone()
                if parse['status'] in ('queued', 'parsing', 'indexing'):
                    raise KS.KBConflict('文档任务正在运行')
                if old:
                    conn.execute("UPDATE kb_structure_publications SET status='queued',error='',updated_at=? WHERE review_id=?", (DS.now(), review_id))
                else:
                    conn.execute('''INSERT INTO kb_structure_publications
                        (review_id,kb_id,doc_id,parse_id,expected_run_id,result_hash,config_json,status,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,'queued',?,?)''',
                        (review_id, kb_id, doc_id, review['parse_id'], expected_run_id, result_hash, json.dumps(config), DS.now(), DS.now()))
                conn.execute("UPDATE kb_parse_versions SET status='queued',lease='',error='',updated_at=? WHERE parse_id=?", (DS.now(), review['parse_id']))
        if not old or old['status'] != 'published':
            KB._start_document(kb_id, doc_id, review['parse_id'], run=partial(run, review_id=review_id))
    return get(kb_id, doc_id, review_id)


def run(kb_id, doc_id, parse_id, finish, publish_lock, *, review_id):
    """根据审阅建议构建新结构及向量索引，通过基线校验后发布。"""
    from rag.document_worker import _slots, publish_index
    from observability.logger import get_logger
    lease = None
    try:
        with _slots:
            lease = DS.claim(kb_id, doc_id, parse_id)
            if not lease:
                return
            deadline = time.monotonic() + C.TASK_TIMEOUT

            def check():
                """确认发布租约、任务期限和预期旧版本仍有效，阻止覆盖其他更新。"""
                if time.monotonic() >= deadline:
                    raise TimeoutError('structure_publication_timeout')
                with KS._transaction() as conn:
                    doc, job = DS.current(conn, kb_id, doc_id, parse_id, lease)
                    publication = conn.execute('SELECT * FROM kb_structure_publications WHERE review_id=?', (review_id,)).fetchone()
                    if publication['status'] != 'running' or publication['expected_run_id'] != doc['published_run_id']:
                        raise KS.KBConflict('发布已停止或基线已变化')
                    return doc, job

            with KS._transaction() as conn:
                DS.current(conn, kb_id, doc_id, parse_id, lease)
                conn.execute("UPDATE kb_structure_publications SET status='running',updated_at=? WHERE review_id=? AND status='queued'", (DS.now(), review_id))
                publication = dict(conn.execute('SELECT * FROM kb_structure_publications WHERE review_id=?', (review_id,)).fetchone())
            doc, _ = check()
            config = json.loads(publication['config_json'])
            review = RS.get(kb_id, doc_id, review_id)
            if fingerprint(review['result']) != publication['result_hash']:
                raise ValueError('publication_result_changed')
            blocks = DS.blocks(parse_id, structure_id=review['structure_id'])
            structure_config = {**config, 'review_id': review_id, 'result_hash': publication['result_hash']}
            structure = HS.prepare(kb_id, doc_id, parse_id, lease, structure_config)
            with KS._transaction() as conn:
                DS.current(conn, kb_id, doc_id, parse_id, lease)
                conn.execute('UPDATE kb_structure_publications SET structure_id=?,updated_at=? WHERE review_id=?',
                             (structure['structure_id'], DS.now(), review_id))
            if structure['status'] == 'building':
                projected, nodes, relations, quality = P.build(blocks, review['result'], title=doc['filename'], check=check)
                quality.update(review_id=review_id, publication='explicit')
                HS.save(kb_id, doc_id, parse_id, lease, structure['structure_id'], nodes, relations, quality, projected)
            nodes, _ = HS.load(structure['structure_id'])
            counter = EmbeddingCounter(check)
            try:
                chunks = P.chunks(blocks, review['result'], nodes, doc['filename'], counter)
                chunks.extend(HS.navigation_chunks(structure['structure_id'], counter.count, C.CHUNK_TOKENS))
            finally:
                counter.session.close()
            if not chunks:
                raise ValueError('no_indexable_text')
            index_config = {**config, 'structure_id': structure['structure_id'], 'range_chunker': P.VERSION}
            publish_index(kb_id, doc_id, parse_id, lease, doc, index_config, chunks, check, deadline, publish_lock, review_id=review_id)
    except Exception as exc:
        code = str(exc)[:150] if isinstance(exc, (ValueError, KS.KBConflict, TimeoutError)) else type(exc).__name__
        if lease:
            DS.fail(parse_id, lease, code)
        with KS._transaction() as conn:
            conn.execute("UPDATE kb_structure_publications SET status='failed',error=?,updated_at=? WHERE review_id=? AND status IN ('queued','running')",
                         (code, DS.now(), review_id))
        get_logger('kb').warn('kb_structure_publication_failed', data={'doc_id': doc_id, 'review_id': review_id, 'error_code': code})
    finally:
        finish()
