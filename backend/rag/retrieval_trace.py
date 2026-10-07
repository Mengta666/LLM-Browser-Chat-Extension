"""阶段候选位置账本；不记录正文，不进入模型工具结果。"""

import hashlib
import json


def record_stage(trace, name, rows, snapshots=None):
    catalog = trace.setdefault('candidates', {})
    entries = []
    for row in rows:
        snapshot = (snapshots or {}).get(row.get('doc_id'), {})
        candidate = {k: row.get(k, snapshot.get(k)) for k in
                     ('doc_id', 'parse_id', 'index_run_id', 'structure_id', 'chunk_idx', 'node_id', 'bundle_id')}
        candidate = {k: v for k, v in candidate.items() if v is not None}
        candidate['locations'] = [{k: p[k] for k in ('block_id', 'page', 'start', 'end') if k in p}
                                  for p in row.get('locations', [])]
        key = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()[:24]
        catalog[key] = candidate
        entries.append({'candidate': key, **({'rerank_score': row['rerank_score']}
                                             if row.get('rerank_score') is not None else {})})
    trace['version'] = 1
    trace.setdefault('stages', []).append({'name': name, 'count': len(entries), 'entries': entries})
