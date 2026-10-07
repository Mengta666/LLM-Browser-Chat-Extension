"""知识库向量适配层；正文与来源以 SQLite 为准。"""

from uuid import NAMESPACE_URL, uuid5

from qdrant_client import models
from agent.memory import vector as V
from agent.memory.vector import (batch_insert_memories, get_kb_chunk, kb_index_complete,
                                 purge_kb_chunks, set_kb_chunks_valid)


def search_memories(query_vector, *, page=None, section_id=None, dense_k=None, sparse_k=None, snapshots=None, node_id=None, **options):
    if page is None and section_id is None and dense_k is None and sparse_k is None and not snapshots and not node_id:
        return V.search_memories(query_vector, **options)
    from storage import kb_store as KS
    ids = None
    if page is not None or section_id is not None or node_id:
        with KS._transaction() as conn:
            KS._owned_kb(conn, options['kb_id'], options['user_id'], active=True)
            overrides = [(did,s['index_run_id']) for did,s in (snapshots or {}).items() if s.get('index_run_id')]
            expr = ('CASE d.doc_id ' + ' '.join('WHEN ? THEN ?' for _ in overrides) + ' ELSE d.published_run_id END') if overrides else 'd.published_run_id'
            rows = conn.execute(f'''SELECT DISTINCT d.doc_id,s.run_id AS published_run_id,s.chunk_idx FROM kb_docs d
            JOIN kb_chunk_sources s ON s.run_id={expr}
            JOIN kb_blocks b ON b.parse_id=s.parse_id AND b.block_id=s.block_id
            WHERE d.kb_id=? AND d.deleted_at='' AND d.status='indexed' AND d.sync_pending=0
              AND (? IS NULL OR b.page=?) AND (? IS NULL OR d.doc_id=?)
              AND (? IS NULL OR b.section_id=? OR b.block_id=? OR EXISTS
                (SELECT 1 FROM json_each(b.structure_json,'$.section_path') WHERE value=?))
              AND (? IS NULL OR EXISTS(SELECT 1 FROM kb_node_sources ns JOIN kb_index_runs r ON r.run_id=s.run_id
                  WHERE ns.structure_id=r.structure_id AND ns.node_id=? AND ns.block_id=s.block_id AND ns.start<s.end AND ns.end>s.start))''',
                (*[v for pair in overrides for v in pair],options['kb_id'], page, page, options.get('doc_id'), options.get('doc_id'),
                 section_id, section_id, section_id, section_id,node_id,node_id)).fetchall()
        if not rows:
            return []
        ids = [V._point_id(str(uuid5(NAMESPACE_URL, 'kb-index:' + ':'.join(map(str, (
            options['user_id'], options['kb_id'], r['doc_id'], r['published_run_id'], r['chunk_idx'])))))) for r in rows]
    V.ensure_collection()
    flt = V._build_filter(user_id=options['user_id'], memory_type=options['memory_type'],
                          scope=None, domain=None, kb_id=options['kb_id'], doc_id=options.get('doc_id'),include_invalid=bool(snapshots))
    if snapshots:
        flt.must.append(models.Filter(should=[models.Filter(must=[
            models.FieldCondition(key='doc_id',match=models.MatchValue(value=did)),
            (models.FieldCondition(key='index_run_id',match=models.MatchValue(value=s['index_run_id'])) if s['index_run_id'] else
             models.Filter(should=[models.FieldCondition(key='index_run_id',match=models.MatchValue(value='')),
                                   models.IsEmptyCondition(is_empty=models.PayloadField(key='index_run_id'))]))])
            for did,s in snapshots.items()]))
    if ids is not None:
        flt.must.append(models.HasIdCondition(has_id=ids))
    limit = max(1, options['top_k'])
    prefetch = [models.Prefetch(query=query_vector, using=V.DENSE_VECTOR_NAME, filter=flt, limit=max(1, dense_k or limit * 2))]
    sparse = V.to_sparse_vector(options.get('query_text', ''))
    if sparse is not None and sparse.indices:
        prefetch.append(models.Prefetch(query=sparse, using=V.SPARSE_VECTOR_NAME, filter=flt, limit=max(1, sparse_k or limit * 2)))
    response = V.get_client().query_points(collection_name=V.MEMORY_COLLECTION, prefetch=prefetch,
        query=models.FusionQuery(fusion=models.Fusion.RRF), limit=limit, with_payload=True)
    return [{**hit.payload, 'score': hit.score} for hit in response.points]
