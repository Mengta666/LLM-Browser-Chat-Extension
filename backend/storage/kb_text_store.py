"""不可变文本版本；正文块的位置绑定同一个解码结果。"""

import bisect
import hashlib
import json
from pathlib import Path

from storage import kb_store as KS, kb_document_store as DS
from rag.text_source import line_starts


def save(kb_id,doc_id,parse_id,lease,text,encoding,blocks):
    from rag.document_config import document_path
    from rag.document_structure import identifiers
    path=document_path(doc_id,parse_id,'source.txt')
    data=text.encode('utf-8'); digest=hashlib.sha256(data).hexdigest()
    with KS._transaction() as conn:
        DS.current(conn,kb_id,doc_id,parse_id,lease)
        if conn.execute('SELECT 1 FROM kb_text_sources WHERE parse_id=?',(parse_id,)).fetchone():
            return
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('text_checkpoint_hash_mismatch')
        else:
            with path.open('xb') as f:
                f.write(data)
        conn.execute('INSERT INTO kb_text_sources VALUES(?,?,?,?,?,?)',
                     (parse_id,str(path),digest,encoding,len(text),json.dumps(line_starts(text))))
        for b in blocks:
            conn.execute('INSERT INTO kb_blocks(parse_id,block_id,page,ordinal,kind,text,bbox_json,structure_json,section_id,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (parse_id,b['block_id'],None,b['ordinal'],b['kind'],b['text'],'[]',json.dumps(b['structure'],ensure_ascii=False),b['section_id'],b['parent_id']))
            conn.executemany('INSERT INTO kb_identifiers VALUES(?,?,?,?,?,?)',
                [(parse_id,b['block_id'],value,original,start,end) for value,original,start,end in identifiers(b['text'])])


def source(parse_id):
    from rag.document_config import document_path
    with KS._transaction() as conn:
        row=conn.execute('SELECT s.*,p.doc_id FROM kb_text_sources s JOIN kb_parse_versions p ON p.parse_id=s.parse_id WHERE s.parse_id=?',(parse_id,)).fetchone()
    if not row:
        raise KS.KBNotFound('文本原文版本不存在')
    path=document_path(row['doc_id'],parse_id,'source.txt')
    if Path(row['text_path']).resolve()!=path:
        raise ValueError('invalid_text_source_path')
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=row['text_sha256']:
        raise ValueError('text_source_changed')
    return data.decode('utf-8'),json.loads(row['line_starts_json'])


def location(starts,structure,start,end):
    a=structure['source_start']+start; b=structure['source_start']+end
    return {'location_kind':'text_span','source_start':a,'source_end':b,
            'line_start':bisect.bisect_right(starts,a),
            'line_end':bisect.bisect_right(starts,max(a,b-1))}


def annotate(row):
    """裁剪/表格补行后按最终块内范围重新计算文本位置。"""
    spans=row.get('segments') or row.get('locations') or []
    if not any(s.get('location_kind')=='text_span' for s in spans):
        return row
    with KS._transaction() as conn:
        raw=conn.execute('SELECT line_starts_json FROM kb_text_sources WHERE parse_id=?',(row['parse_id'],)).fetchone()
        if not raw:
            raise KS.KBNotFound('文本原文版本不存在')
        starts=json.loads(raw[0])
        records=list(conn.execute(
            'SELECT block_id,kind,structure_json FROM kb_blocks WHERE parse_id=? AND block_id IN (SELECT value FROM json_each(?))',
            (row['parse_id'],json.dumps(list({s['block_id'] for s in spans})))))
        blocks={r['block_id']:json.loads(r['structure_json']) for r in records}
    for s in spans:
        s.update(location(starts,blocks[s['block_id']],s['start'],s['end']))
    if row.get('segments'):
        row['locations']=[{k:v for k,v in s.items() if k!='text'} for s in spans]
    row['source_kind']='text_span'
    if any(r['kind'] in ('code','table') for r in records):
        row['protected_raw']=True
    return row
