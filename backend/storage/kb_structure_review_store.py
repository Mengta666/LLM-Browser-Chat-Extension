"""影子分析不写正式结构；每个检查点通过租约阻止取消后的迟到提交。"""
import json
from uuid import uuid4

from storage import kb_store as KS, kb_document_store as DS


def create(kb_id, doc_id, snapshot, fingerprint, config,request_id=''):
    with KS._transaction() as conn:
        doc = DS.owned(conn, kb_id, doc_id)
        if doc['file_type'] != 'pdf':
            raise ValueError('结构影子分析首批仅支持 PDF')
        if request_id:
            existing=conn.execute('SELECT * FROM kb_structure_reviews WHERE doc_id=? AND request_id=?',(doc_id,request_id)).fetchone()
            if existing:
                if existing['fingerprint']!=fingerprint:
                    raise KS.KBConflict('同一结构请求标识不能更换配置或原文快照')
                return dict(existing)
        row = conn.execute('SELECT * FROM kb_structure_reviews WHERE doc_id=? AND fingerprint=?',
                           (doc_id, fingerprint)).fetchone()
        if row:
            return dict(row)
        review_id = 'sr_' + uuid4().hex
        conn.execute('''INSERT INTO kb_structure_reviews
            (review_id,kb_id,doc_id,parse_id,structure_id,fingerprint,config_json,status,created_at,updated_at,request_id,checkpoint_json)
            VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)''',
            (review_id,kb_id,doc_id,snapshot['parse_id'],snapshot['structure_id'],fingerprint,
             json.dumps(config),DS.now(),DS.now(),request_id,json.dumps({'phase':'roles'})))
        return dict(conn.execute('SELECT * FROM kb_structure_reviews WHERE review_id=?',(review_id,)).fetchone())


def owned(conn, kb_id, doc_id, review_id):
    DS.owned(conn, kb_id, doc_id)
    row = conn.execute('SELECT * FROM kb_structure_reviews WHERE review_id=? AND kb_id=? AND doc_id=?',
                       (review_id,kb_id,doc_id)).fetchone()
    if not row:
        raise KS.KBNotFound('结构分析任务不存在')
    return dict(row)


def get(kb_id, doc_id, review_id):
    with KS._transaction() as conn:
        row = owned(conn,kb_id,doc_id,review_id)
        row['checkpoint'] = json.loads(row.pop('checkpoint_json'))
        for window in [row['checkpoint'].get('active_window',{}),*row['checkpoint'].get('retired_windows',[])]:
            window.pop('request',None)
            window.pop('result',None)
        row['result'] = json.loads(row.pop('result_json'))
        row['config'] = json.loads(row.pop('config_json'))
        row.pop('lease')
        if not row.get('request_id'):row.pop('request_id',None)
        row['mode'] = 'shadow'
        row['published'] = False
        publication = conn.execute("SELECT structure_id,run_id FROM kb_structure_publications WHERE review_id=? AND status='published'", (review_id,)).fetchone()
        if publication:
            row['published'] = True
            row['publication'] = dict(publication)
        row['batches'] = [{'batch_no':r['batch_no'],'metrics':json.loads(r['metrics_json'])} for r in conn.execute(
            'SELECT batch_no,metrics_json FROM kb_structure_review_batches WHERE review_id=? ORDER BY batch_no', (review_id,))]
        return row


def claim(kb_id, doc_id, review_id, fingerprint):
    with KS._transaction() as conn:
        row = owned(conn,kb_id,doc_id,review_id)
        if row['fingerprint'] != fingerprint:
            raise KS.KBConflict('分析配置已改变，请新建任务，不能混用检查点')
        if row['status'] in ('running','completed'):
            return None
        lease = uuid4().hex
        conn.execute("UPDATE kb_structure_reviews SET status='running',lease=?,error='',updated_at=? WHERE review_id=?",
                     (lease,DS.now(),review_id))
        return lease


def check(conn, kb_id, doc_id, review_id, lease):
    row = owned(conn,kb_id,doc_id,review_id)
    if row['status'] != 'running' or row['lease'] != lease:
        raise KS.KBConflict('结构分析已停止或租约已过期')
    return row


def batches(review_id):
    with KS._transaction() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM kb_structure_review_batches WHERE review_id=? ORDER BY batch_no', (review_id,))]


def save_batch(kb_id,doc_id,review_id,lease,batch_no,input_hash,request,output,metrics,checkpoint):
    with KS._transaction() as conn:
        current=check(conn,kb_id,doc_id,review_id,lease)
        checkpoint={**json.loads(current['checkpoint_json']),**checkpoint}
        checkpoint.pop('active_window',None)
        checkpoint.pop('failed_batch_metrics',None)
        conn.execute('INSERT INTO kb_structure_review_batches VALUES(?,?,?,?,?,?)',
                     (review_id,batch_no,input_hash,json.dumps(request,ensure_ascii=False),
                      json.dumps(output,ensure_ascii=False),json.dumps(metrics)))
        conn.execute('UPDATE kb_structure_reviews SET checkpoint_json=?,updated_at=? WHERE review_id=?',
                     (json.dumps(checkpoint),DS.now(),review_id))


def reserve_call(kb_id,doc_id,review_id,lease,limit,*,request=None,input_hash=None):
    with KS._transaction() as conn:
        row=check(conn,kb_id,doc_id,review_id,lease)
        checkpoint=json.loads(row['checkpoint_json'])
        calls=checkpoint.get('model_calls',0)
        if calls>=limit:
            raise ValueError('review_model_call_limit')
        if request is not None:
            active=checkpoint.get('active_window')
            if active is None:
                active={'input_hash':input_hash,'request':request,'attempts':[]}
            if active['input_hash']!=input_hash or active['request']!=request:
                raise ValueError('review_checkpoint_mismatch')
            if len(active['attempts'])>=2:
                raise ValueError('review_window_attempt_limit')
            active['attempts'].append({'status':'pending'})
            checkpoint['active_window']=active
            checkpoint['phase']='global_review' if request.get('window_id')=='global' else 'roles'
        checkpoint['model_calls']=calls+1
        conn.execute('UPDATE kb_structure_reviews SET checkpoint_json=?,updated_at=? WHERE review_id=?',
                     (json.dumps(checkpoint),DS.now(),review_id))


def record_attempt(kb_id,doc_id,review_id,lease,input_hash,metrics,result=None):
    with KS._transaction() as conn:
        row=check(conn,kb_id,doc_id,review_id,lease)
        checkpoint=json.loads(row['checkpoint_json']);active=checkpoint['active_window']
        if active['input_hash']!=input_hash or active['attempts'][-1]['status']!='pending':
            raise ValueError('review_checkpoint_mismatch')
        active['attempts'][-1]={'status':'returned','metrics':metrics}
        if result is not None:active['result']=result
        conn.execute('UPDATE kb_structure_reviews SET checkpoint_json=?,updated_at=? WHERE review_id=?',
                     (json.dumps(checkpoint,ensure_ascii=False),DS.now(),review_id))


def split_window(kb_id,doc_id,review_id,lease,input_hash):
    with KS._transaction() as conn:
        row=check(conn,kb_id,doc_id,review_id,lease)
        checkpoint=json.loads(row['checkpoint_json']);active=checkpoint['active_window']
        if active['input_hash']!=input_hash or active['attempts'][-1].get('metrics',{}).get('finish_reason')!='length':
            raise ValueError('review_checkpoint_mismatch')
        size=len(active['request']['anchors'])
        if size<=1:raise ValueError('review_output_incomplete')
        checkpoint['window_anchor_limit']=max(1,size//2)
        checkpoint.setdefault('retired_windows',[]).append(checkpoint.pop('active_window'))
        conn.execute('UPDATE kb_structure_reviews SET checkpoint_json=?,updated_at=? WHERE review_id=?',
                     (json.dumps(checkpoint,ensure_ascii=False),DS.now(),review_id))


def complete(kb_id,doc_id,review_id,lease,result):
    with KS._transaction() as conn:
        row=check(conn,kb_id,doc_id,review_id,lease)
        checkpoint={**json.loads(row['checkpoint_json']),'phase':'completed'}
        conn.execute("UPDATE kb_structure_reviews SET status='completed',lease='',result_json=?,checkpoint_json=?,updated_at=? WHERE review_id=?",
                     (json.dumps(result,ensure_ascii=False),json.dumps(checkpoint),DS.now(),review_id))


def fail(review_id,lease,error,metrics=None):
    with KS._transaction() as conn:
        row=conn.execute("SELECT checkpoint_json FROM kb_structure_reviews WHERE review_id=? AND lease=? AND status='running'", (review_id,lease)).fetchone()
        if not row:
            return
        checkpoint=json.loads(row['checkpoint_json'])
        if metrics:
            checkpoint['failed_batch_metrics']=metrics
        conn.execute("UPDATE kb_structure_reviews SET status='failed',lease='',error=?,updated_at=? WHERE review_id=? AND lease=? AND status='running'",
                     (error,DS.now(),review_id,lease))
        conn.execute('UPDATE kb_structure_reviews SET checkpoint_json=? WHERE review_id=?', (json.dumps(checkpoint),review_id))


def cancel(kb_id,doc_id,review_id):
    with KS._transaction() as conn:
        row=owned(conn,kb_id,doc_id,review_id)
        if row['status'] in ('pending','running'):
            conn.execute("UPDATE kb_structure_reviews SET status='cancelled',lease='',updated_at=? WHERE review_id=?",
                         (DS.now(),review_id))
    return get(kb_id,doc_id,review_id)


def interrupt_running():
    with KS._transaction() as conn:
        conn.execute("UPDATE kb_structure_reviews SET status='interrupted',lease='',updated_at=? WHERE status='running'", (DS.now(),))
