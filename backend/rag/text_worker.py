"""文本解析使用本地语法规则；索引发布沿用知识库版本和检查点。"""

import os
import json
import time
import threading

from rag import document_config as C, text_structure
from rag.document_structure import EmbeddingCounter, build_chunks, fingerprint
from rag.text_source import original
from rag.embedder import embed_texts
from storage import kb_store as KS, kb_document_store as DS, kb_structure_store as HS, kb_text_store as TS
from observability.logger import get_logger
from agent.memory.config import CHAT_USER_ID, MEMORY_TYPE_KB_CHUNK

_log=get_logger('kb')
_slots=threading.BoundedSemaphore(C.TEXT_CONCURRENCY)


def run(kb_id,doc_id,parse_id,finish,publish_lock):
    """恢复或执行文本解析、结构构建和分批索引，完整校验后发布版本。"""
    from rag import kb_vectors as V
    lease,run_id,published=None,None,False
    _slots.acquire()
    try:
        lease=DS.claim(kb_id,doc_id,parse_id)
        if not lease:
            return
        deadline=time.monotonic()+C.TASK_TIMEOUT
        def check():
            """确认文本任务未超时且当前租约仍有效。"""
            if time.monotonic()>=deadline:
                raise TimeoutError('document_task_timeout')
            return DS.check(kb_id,doc_id,parse_id,lease)
        doc,job=check()
        parse_config=json.loads(job['config_json'])
        blocks=DS.blocks(parse_id)
        if not blocks:
            # 没有可复用的块时需重新解析；配置指纹必须匹配，防止恢复任务时混用新旧规则。
            if fingerprint(text_structure.config(doc['file_type']))!=job['fingerprint']:
                raise ValueError('text_parser_changed_reparse_required')
            text,encoding=original(doc)
            blocks,nodes,relations,quality=text_structure.build(text,doc['file_type'],doc['filename'])
            check()
            TS.save(kb_id,doc_id,parse_id,lease,text,encoding,blocks)
        structure=HS.prepare(kb_id,doc_id,parse_id,lease,parse_config)
        if structure['status']=='building':
            if fingerprint(text_structure.config(doc['file_type']))!=job['fingerprint']:
                raise ValueError('text_parser_changed_reparse_required')
            text,_=TS.source(parse_id)
            _,nodes,relations,quality=text_structure.build(text,doc['file_type'],doc['filename'])
            HS.save(kb_id,doc_id,parse_id,lease,structure['structure_id'],nodes,relations,quality)
        cfg={'text_chunker':2,'tokens':C.CHUNK_TOKENS,'structure_id':structure['structure_id'],
             'model':os.getenv('EMBEDDING_MODEL'),'tokenizer_url':C.TOKENIZER_URL,'base_url':os.getenv('EMBEDDING_BASE_URL')}
        counter=EmbeddingCounter(check)
        try:
            chunks=build_chunks(blocks,doc['filename'],counter)
            chunks.extend(HS.navigation_chunks(structure['structure_id'],counter.count,C.CHUNK_TOKENS))
        finally:
            counter.session.close()
        if not chunks:
            raise ValueError('no_indexable_text')
        run=DS.prepare_index(kb_id,doc_id,parse_id,lease,fingerprint(cfg),cfg,chunks,structure['structure_id'])
        run_id=run['run_id']; count=run['indexed_count']
        while count<run['chunk_count']:
            check()
            batch=DS.index_chunks(run_id,count)
            vectors=embed_texts([c['text'] for c in batch],batch_size=32,timeout=max(.1,min(120,deadline-time.monotonic())))
            if len(vectors)!=len(batch):
                raise ValueError('embedding_count_mismatch')
            V.batch_insert_memories([{'content':c['text'],'vector':v,'memory_type':MEMORY_TYPE_KB_CHUNK,
                'user_id':CHAT_USER_ID,'kb_id':kb_id,'doc_id':doc_id,'index_run_id':run_id,'source':doc['filename'],
                'chunk_idx':c['chunk_idx'],'chunk_id':c['chunk_idx'],'valid':False} for c,v in zip(batch,vectors)],before_batch=check)
            count+=len(batch)
            DS.advance_index(kb_id,doc_id,parse_id,lease,run_id,count)
        with publish_lock:
            # 分批索引期间旧版本仍可用，完成性校验通过后才在发布锁内切换可检索版本。
            check()
            if not V.kb_index_complete(user_id=CHAT_USER_ID,kb_id=kb_id,doc_id=doc_id,index_run_id=run_id,expected=count):
                raise ValueError('incomplete_vector_index')
            V.set_kb_chunks_valid(user_id=CHAT_USER_ID,kb_id=kb_id,doc_id=doc_id,index_run_id=run_id,valid=True)
            DS.publish(kb_id,doc_id,parse_id,lease,run_id)
            published=True
            if doc['published_run_id'] and doc['published_run_id']!=run_id:
                try:
                    V.set_kb_chunks_valid(user_id=CHAT_USER_ID,kb_id=kb_id,doc_id=doc_id,index_run_id=doc['published_run_id'],valid=False)
                except Exception:
                    _log.warn('kb_retired_index_cleanup_pending',data={'doc_id':doc_id})
    except Exception as exc:
        if lease:
            detail=str(exc)[:150] if isinstance(exc,(ValueError,KS.KBConflict,TimeoutError)) else type(exc).__name__
            DS.fail(parse_id,lease,f'文本处理失败：{detail}；原文件与检查点已保留')
        _log.warn('kb_text_failed',data={'doc_id':doc_id,'error_type':type(exc).__name__})
    finally:
        if run_id and not published:
            try:
                V.set_kb_chunks_valid(user_id=CHAT_USER_ID,kb_id=kb_id,doc_id=doc_id,index_run_id=run_id,valid=False)
            except Exception:
                _log.warn('kb_staging_cleanup_pending',data={'doc_id':doc_id})
        finish()
        _slots.release()
