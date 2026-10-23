"""独立、显式启动的 PDF 结构影子分析；不发布模型推断。"""
import hashlib
import json
import os
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from openai import OpenAI, APIStatusError

from agent.token_utils import RequestTokenCounter
from storage import kb_store as KS, kb_document_store as DS, kb_structure_store as HS
from storage import kb_structure_review_store as RS

from rag.structure_review_protocol import VERSION, SYSTEM, validate, assemble, advance
from rag import structure_review_evidence as E
from rag import structure_review_global as G
from rag import structure_review_schema as S
_slot = threading.BoundedSemaphore(1)
_workers = set()
_lock = threading.Lock()

def settings():
    """读取并校验结构审阅模型、接口和预算参数，生成任务配置指纹。"""
    mode = os.getenv('KB_STRUCTURE_MODE', 'rules')
    if mode not in ('rules', 'shadow'):
        raise ValueError('第一批 KB_STRUCTURE_MODE 仅支持 rules/shadow，不支持发布模型结构')
    cfg = {'version':VERSION, 'mode':mode, 'base_url':os.getenv('KB_STRUCTURE_BASE_URL','').rstrip('/'),
           'model':os.getenv('KB_STRUCTURE_MODEL',''), 'revision':os.getenv('KB_STRUCTURE_MODEL_REVISION',''),
           'reasoning_effort':os.getenv('KB_STRUCTURE_REASONING_EFFORT','medium'),
           'response_format':os.getenv('KB_STRUCTURE_RESPONSE_FORMAT','json_object'),
           'tokenizer_url':os.getenv('KB_STRUCTURE_TOKENIZER_URL',''), 'protocol_hash':digest([SYSTEM,G.SYSTEM])}
    for key, default in [('context_tokens',32768),('input_tokens',8192),('output_tokens',4096),
                         ('anchor_chars',1000),('call_timeout',150),('task_timeout',1800),('model_call_limit',12)]:
        cfg[key] = int(os.getenv('KB_STRUCTURE_'+key.upper(),str(default)))
        if cfg[key] <= 0:
            raise ValueError('结构分析参数必须为正整数')
    if cfg['input_tokens']+cfg['output_tokens']+256 > cfg['context_tokens']:
        raise ValueError('结构分析输入、输出及安全预留超过窗口')
    cfg['output_json_ratio']=float(os.getenv('KB_STRUCTURE_OUTPUT_JSON_RATIO','.45'))
    if not 0<cfg['output_json_ratio']<=1:
        raise ValueError('结构 JSON 规划比例必须在 (0,1]')
    if cfg['reasoning_effort'] not in ('','low','medium','high','xhigh'):
        raise ValueError('结构分析推理档位不正确')
    if cfg['response_format'] not in ('json_object','json_schema'):
        raise ValueError('KB_STRUCTURE_RESPONSE_FORMAT 仅支持 json_object/json_schema')
    for url in (cfg['base_url'],cfg['tokenizer_url']):
        if url:
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('结构服务地址应为不含凭证与查询参数的 HTTP(S) 地址')
    return cfg


def digest(value):
    """计算结构化输入的稳定摘要，用于审阅窗口及配置身份校验。"""
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def anchors(blocks,pages,limit):
    """从原文块和页面信息提取有限的结构锚点，保留来源引用。"""
    dimensions={p['page']:(p['width'],p['height']) for p in pages}
    result=[]
    for b in blocks:
        start=0
        for line in b['text'].splitlines(keepends=True):
            for shift in range(0,len(line),limit):
                lo=start+shift; hi=min(start+len(line),lo+limit)
                box=b.get('bbox'); w,h=dimensions[b['page']]
                result.append({'id':f"{b['block_id']}@{lo}:{hi}",'block_id':b['block_id'],'start':lo,'end':hi,
                               'page':b['page'],'bbox':[round(v/(w if i%2==0 else h),4) for i,v in enumerate(box)] if box else None,
                               'kind':b['kind'],'text':b['text'][lo:hi],
                               'features':b['structure'].get('heading_features',{}),
                               'rule_heading':b['structure'].get('section_heading') if lo==0 else None,
                               'rule_excluded':b['structure'].get('index_excluded','')})
            start+=len(line)
    for a in result:
        a['evidence_refs']=[a['id']]
        if a['bbox']: a['evidence_refs'].append(a['id']+'#layout')
        if a['features']: a['evidence_refs'].append(a['id']+'#style')
        if a['rule_heading']: a['evidence_refs'].append(a['id']+'#rule')
    return result


def messages(payload, repair='',system=SYSTEM):
    """将审阅窗口和可选协议修复提示转换为模型消息。"""
    wire=G.wire_payload(payload) if payload['window_id']=='global' else payload
    return [{'role':'system','content':system+('\n上次输出协议错误：'+repair+'。path 是字段路径，missing 需补齐字段，extra_forbidden 需移除多余字段；按已给契约修正，重新返回完整结果，不猜测未提供的锚点、不输出解释。' if repair else '')},
            {'role':'user','content':json.dumps(wire,ensure_ascii=False)}]


def output_load(window,counter):
    """估算结构审阅窗口所需的输出额度，辅助拆分超限窗口。"""
    items=[]
    for a in window['anchors']:
        items.append({'anchors':[a['id']],'role':'section_heading','parent_section':a['id'],
                      'section_level':2,'status':'unresolved','evidence_refs':a['evidence_refs'][:2],
                      'reason':'依据本文相邻内容与编号样式综合判断。角色及层级存在歧义时保留未决，不把候选当成已经核实的结论。'})
    return counter.count_text(json.dumps({'window_id':window['window_id'],'items':items,'issues':[]},ensure_ascii=False))


def start(kb_id,doc_id,review_id=None,*,request_id=''):
    """创建或续接结构审阅任务，获得租约后启动后台分析。"""
    cfg=settings()
    if cfg['mode']!='shadow' or not cfg['base_url'] or not cfg['model']:
        raise ValueError('请显式配置 KB_STRUCTURE_MODE=shadow 及独立模型地址、名称')
    snapshot=HS.snapshot(kb_id,doc_id)
    if not snapshot['structure_id']:
        raise KS.KBConflict('文档需先发布原文和规则结构')
    row=RS.get(kb_id,doc_id,review_id) if review_id else None
    if row:
        if request_id:raise ValueError('恢复结构任务不能指定新请求标识')
        request_id=row.get('request_id','')
    if request_id:request_id=str(UUID(request_id))
    identity={'config':cfg,'parse_id':snapshot['parse_id'],'structure_id':snapshot['structure_id']}
    if request_id:identity['request_id']=request_id
    fingerprint=digest(identity)
    if row is None:row=RS.create(kb_id,doc_id,snapshot,fingerprint,cfg,request_id)
    if row['fingerprint']!=fingerprint:
        raise KS.KBConflict('配置或当前结构版本已变化，请新建影子任务')
    if review_id is None and row['status'] in ('failed','cancelled','interrupted'):
        return RS.get(kb_id,doc_id,row['review_id'])
    with _lock:
        if row['review_id'] not in _workers and row['status']!='completed':
            if len(_workers)>=4:
                raise KS.KBConflict('结构分析队列已满，请稍后显式启动')
            lease=RS.claim(kb_id,doc_id,row['review_id'],fingerprint)
            if lease:
                _workers.add(row['review_id'])
                threading.Thread(target=run,args=(kb_id,doc_id,row['review_id'],lease,cfg),daemon=True).start()
    return RS.get(kb_id,doc_id,row['review_id'])


def run(kb_id,doc_id,review_id,lease,cfg):
    """分窗口执行结构分析并保存检查点，完成全局整理后保存建议但不自动发布。"""
    from observability.logger import get_logger
    log=get_logger('kb')
    deadline=time.monotonic()+cfg['task_timeout']
    metrics=[]
    def check():
        """确认审阅租约、文档归属和任务期限仍然有效。"""
        if time.monotonic()>=deadline:
            raise TimeoutError('review_task_timeout')
        with KS._transaction() as conn:
            return RS.check(conn,kb_id,doc_id,review_id,lease)
    try:
        with _slot:
            row=check()
            blocks=DS.blocks(row['parse_id'],structure_id=row['structure_id'])
            all_anchors=anchors(blocks,DS.pages(row['parse_id']),cfg['anchor_chars'])
            groups=E.catalog(all_anchors)
            counter=RequestTokenCounter(cfg['model'],deadline=deadline,time_budget=30,
                                        chat_template_kwargs={'reasoning_effort':cfg['reasoning_effort']} if cfg['reasoning_effort'] else {})
            counter.url=cfg['tokenizer_url'];counter.api_key=os.getenv('KB_STRUCTURE_TOKENIZER_API_KEY','')
            counter._disabled='' if counter.url else 'not_configured'
            saved=RS.batches(review_id); outputs=[];cursor=0;prior=[];batch_no=0
            with OpenAI(base_url=cfg['base_url'],api_key=os.getenv('KB_STRUCTURE_API_KEY') or 'none',max_retries=0,
                        http_client=httpx.Client(trust_env=False,follow_redirects=False)) as client:
                def invoke(window,active,validator,system,planned_output,allow_split):
                    """复用窗口检查点或调用模型，验证结构协议并按预算决定修复或拆分。"""
                    nonlocal metrics
                    input_hash=digest(window);active=active or {'attempts':[]}
                    metrics=[a['metrics'] for a in active['attempts'] if 'metrics' in a]
                    result=active.get('result')
                    if result is not None:result=validator(result,window)
                    if metrics and metrics[-1]['finish_reason']=='length':
                        if not allow_split:raise ValueError('review_output_incomplete')
                        RS.split_window(kb_id,doc_id,review_id,lease,input_hash)
                        return None,True
                    error=json.dumps({'code':metrics[-1].get('protocol_error'),'details':metrics[-1].get('diagnostic',{})},ensure_ascii=False) if metrics else ''
                    if result is None and len(active['attempts'])>=2:
                        raise ValueError('review_window_attempt_limit')
                    for attempt in range(len(active['attempts']),2) if result is None else ():
                        check();request=messages(window,error,system);count=counter(request)
                        if count>min(cfg['input_tokens'],min(cfg['context_tokens'],counter.model_window or cfg['context_tokens'])-cfg['output_tokens']-256):
                            raise ValueError('review_request_exceeds_input_budget')
                        RS.reserve_call(kb_id,doc_id,review_id,lease,cfg['model_call_limit'],request=window,input_hash=input_hash)
                        started=time.monotonic()
                        format_spec=S.response_format(window,cfg['response_format'])
                        try:
                            response=client.chat.completions.create(model=cfg['model'],messages=request,
                                max_tokens=cfg['output_tokens'],response_format=format_spec,
                                extra_body={'chat_template_kwargs':counter.chat_template_kwargs} if counter.chat_template_kwargs else {},
                                timeout=max(.1,min(cfg['call_timeout'],deadline-time.monotonic())))
                        except APIStatusError as exc:
                            metric={'input_tokens':count,**counter.last,'finish_reason':'api_error',
                                    'phase':'global' if window['window_id']=='global' else 'roles',
                                    'seconds':round(time.monotonic()-started,3),'http_status':exc.status_code,
                                    'response_format':cfg['response_format'],'response_format_hash':digest(format_spec)}
                            metrics.append(metric)
                            RS.record_attempt(kb_id,doc_id,review_id,lease,input_hash,metric)
                            raise ValueError('review_model_request_rejected') from None
                        check();choice=response.choices[0]
                        reasoning=getattr(choice.message,'reasoning_content',None)
                        if reasoning is None:reasoning=getattr(choice.message,'reasoning',None)
                        metrics.append({'input_tokens':count,**counter.last,'finish_reason':choice.finish_reason,
                                        'response_format':cfg['response_format'],'response_format_hash':digest(format_spec),
                                        'phase':'global' if window['window_id']=='global' else 'roles',
                                        'seconds':round(time.monotonic()-started,3),'planned_json_tokens':planned_output,
                                        'content_chars':len(choice.message.content or ''),
                                        'reasoning_chars':len(reasoning) if isinstance(reasoning,str) else None,
                                        'usage':response.usage.model_dump() if response.usage else {}})
                        if choice.finish_reason!='stop':
                            RS.record_attempt(kb_id,doc_id,review_id,lease,input_hash,metrics[-1])
                            if allow_split and choice.finish_reason=='length' and len(window['anchors'])>1:
                                RS.split_window(kb_id,doc_id,review_id,lease,input_hash)
                                return None,True
                            raise ValueError('review_output_incomplete')
                        try:
                            result=validator(json.loads(choice.message.content or ''),window)
                        except (ValueError,TypeError,KeyError) as exc:
                            code=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('review_') else 'review_invalid_json'
                            diagnostic=getattr(exc,'details',{})
                            if isinstance(exc,json.JSONDecodeError):
                                diagnostic={'path':'/','kind':'invalid_json','line':exc.lineno,'column':exc.colno}
                            metrics[-1]['protocol_error']=code;metrics[-1]['diagnostic']=diagnostic
                            error=json.dumps({'code':code,'details':metrics[-1]['diagnostic']},ensure_ascii=False)
                            RS.record_attempt(kb_id,doc_id,review_id,lease,input_hash,metrics[-1])
                            if attempt:raise ValueError(code) from None
                        else:
                            RS.record_attempt(kb_id,doc_id,review_id,lease,input_hash,metrics[-1],result)
                            break
                    return result,False

                while cursor<len(all_anchors):
                    checkpoint=json.loads(check()['checkpoint_json'])
                    metrics=[]
                    if batch_no<len(saved):
                        previous=saved[batch_no]
                        window=json.loads(previous['input_json'])
                        stop=cursor+len(window['anchors'])
                        if previous['batch_no']!=batch_no or previous['input_hash']!=digest(window) or window['window_id']!=f'{batch_no}:{cursor}' or not window['anchors'] or window['anchors']!=all_anchors[cursor:stop] or window['prior_headings']!=prior or window.get('document_evidence')!=E.window_evidence(groups,all_anchors,window['anchors']):
                            raise ValueError('review_checkpoint_mismatch')
                        result=validate(json.loads(previous['output_json']),window)
                    else:
                        active=checkpoint.get('active_window')
                        if active:
                            window=active['request'];stop=cursor+len(window['anchors'])
                            if active['input_hash']!=digest(window) or window['window_id']!=f'{batch_no}:{cursor}' or not window['anchors'] or window['anchors']!=all_anchors[cursor:stop] or window['prior_headings']!=prior or window['document_evidence']!=E.window_evidence(groups,all_anchors,window['anchors']):
                                raise ValueError('review_checkpoint_mismatch')
                        else:
                            # 输入和结构输出双约束；截断后的窗口上限随检查点保留，不扩大输出额度。
                            window={'window_id':f'{batch_no}:{cursor}','prior_headings':prior,'anchors':[]}
                            low=cursor;high=min(len(all_anchors),cursor+checkpoint.get('window_anchor_limit',len(all_anchors)))
                            while low<high:
                                end=(low+high+1)//2
                                trial={**window,'anchors':all_anchors[cursor:end]}
                                trial['document_evidence']=E.window_evidence(groups,all_anchors,trial['anchors'])
                                count=counter(messages(trial))
                                ceiling=min(cfg['input_tokens'],min(cfg['context_tokens'],counter.model_window or cfg['context_tokens'])-cfg['output_tokens']-256)
                                if count<=ceiling-256 and output_load(trial,counter)<=cfg['output_tokens']*cfg['output_json_ratio']:
                                    low=end
                                else:
                                    high=end-1
                            stop=low
                            if stop==cursor:
                                raise ValueError('review_anchor_exceeds_window_budget')
                            window['anchors']=all_anchors[cursor:stop]
                            window['document_evidence']=E.window_evidence(groups,all_anchors,window['anchors'])
                        input_hash=digest(window)
                        result,split=invoke(window,active,validate,SYSTEM,output_load(window,counter),True)
                        if split:continue
                        RS.save_batch(kb_id,doc_id,review_id,lease,batch_no,input_hash,window,result,metrics,
                                      {'completed_anchors':stop,'total_anchors':len(all_anchors),'completed_batches':batch_no+1,
                                       'phase':'global_review' if stop==len(all_anchors) else 'roles'})
                    outputs.append(result)
                    lookup={a['id']:a for a in window['anchors']}
                    prior=advance(prior,result,lookup)
                    cursor=stop;batch_no+=1
                checkpoint=json.loads(check()['checkpoint_json']);metrics=[]
                global_window=G.window(all_anchors,outputs,groups)
                previous=saved[batch_no] if batch_no<len(saved) else None
                active=checkpoint.get('active_window')
                pinned=json.loads(previous['input_json']) if previous else active['request'] if active else None
                if pinned:
                    global_window['edit_limit']=pinned.get('edit_limit')
                    if 'encoding' in pinned:
                        if pinned['encoding']!='compact_v1':raise ValueError('review_global_encoding')
                        global_window['encoding']=pinned['encoding']
                    if global_window!=pinned or (previous['input_hash'] if previous else active['input_hash'])!=digest(pinned):
                        raise ValueError('review_checkpoint_mismatch')
                else:
                    global_window['encoding']='compact_v1'
                    low=0;high=min(32,len(global_window['anchors']))
                    while low<high:
                        middle=(low+high+1)//2;global_window['edit_limit']=middle
                        if G.output_load(global_window,counter)<=cfg['output_tokens']*cfg['output_json_ratio']:low=middle
                        else:high=middle-1
                    global_window['edit_limit']=low
                    if not low and global_window['anchors']:raise ValueError('review_global_output_budget')
                if previous:
                    reviewed=G.validate(json.loads(previous['output_json']),global_window,all_anchors,groups)
                else:
                    reviewed,_=invoke(global_window,active,lambda v,w:G.validate(v,w,all_anchors,groups),
                                      G.SYSTEM,G.output_load(global_window,counter),False)
                    RS.save_batch(kb_id,doc_id,review_id,lease,batch_no,digest(global_window),global_window,reviewed,metrics,
                                  {'global_review_done':reviewed['complete'],'phase':'assembly' if reviewed['complete'] else 'global_review'})
                if not reviewed['complete']:raise ValueError('review_global_incomplete')
                result=E.attach(assemble(all_anchors,G.corrected(reviewed,global_window,all_anchors),groups),all_anchors,groups,DS.pages(row['parse_id']))
                result['global_review']={'complete':True,'edits':reviewed['edits'],'issues':reviewed['issues'],
                                         'coverage':global_window['coverage'],'semantics_verified':False}
                RS.complete(kb_id,doc_id,review_id,lease,result)
                log.info('kb_structure_review_completed',data={'review_id':review_id,'doc_id':doc_id,'batches':batch_no+1,'role_batches':batch_no,'anchors':len(all_anchors),'published':False})
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('review_') else type(exc).__name__
        RS.fail(review_id,lease,code,metrics)
        log.warn('kb_structure_review_failed',data={'review_id':review_id,'error_code':code})
    finally:
        with _lock:
            _workers.discard(review_id)
