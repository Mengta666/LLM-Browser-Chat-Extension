"""自动化每步最多整理一个证据片段，复用知识库原文校验和压缩格式。"""

import json

from api.kb_compaction import prompt_for, partition, merge_parts
from agent.memory import config as C
from search import kb_workspace as W


def prepare_one(messages, records, state, counter, limit, complete, *, rows=None):
    config = W.settings(state)
    if not config['WORKSPACE_ENABLED'] or not config['WORKSPACE_COMPACT']:
        return False
    rows = W.collect(records, state) if rows is None else rows
    base = W.strip_view(messages)
    size = counter([*base, {'role':'user','content':W.MARKER + json.dumps(
        W.payload(W.view_rows(rows, state), len(rows)), ensure_ascii=False)}])
    active = any(p.get('status') == 'processing' for p in state.get('workspace', {}).get('packs', {}).values())
    if size < limit * (config['WORKSPACE_TARGET'] if active else config['WORKSPACE_TRIGGER']):
        return False
    workspace = state.setdefault('workspace', {'version':0})
    packs = workspace.setdefault('packs', {})
    bound = min(config['WORKSPACE_BATCH_TOKENS'], limit,
                min(counter.model_window or C.CHAT_CONTEXT_LENGTH, C.CHAT_CONTEXT_LENGTH)
                - config['WORKSPACE_OUTPUT_TOKENS'] - C.CHAT_CONTEXT_SAFETY_TOKENS)
    for group in W.units(rows)[:-1]:
        key = W.pack_key(group, state)
        record = packs.get(key)
        if record and record['status'] != 'processing':
            continue
        if any(r.get('atomic') or r.get('protected_raw') or r.get('node_kind') in ('table','formula','code') for r in group):
            continue
        before = counter.count_text(json.dumps([W.wire_row(r) for r in group], ensure_ascii=False))
        if before < config['WORKSPACE_MIN_TOKENS']:
            continue
        if record is None:
            try:
                parts = partition(group, state, counter, bound, max(1, config['WORKSPACE_OUTPUT_TOKENS'] // 256))
            except ValueError:
                packs[key] = {'status':'too_large'}
                continue
            record = packs[key] = {'status':'processing', 'parts':[], 'plan':[
                [{'citation':r['citation'], 'span':r.get('archive_span',[0,len(r['content'])]),
                  'partial':r.get('fragment_partial',False)} for r in part] for part in parts]}
        source = {r['citation']:r for r in group}
        index = len(record['parts'])
        part = [{**source[p['citation']], 'content':source[p['citation']]['content'][p['span'][0]:p['span'][1]],
                 'archive_span':p['span'], 'fragment_partial':p['partial']} for p in record['plan'][index]]
        prompt = prompt_for(part, state)
        measured = counter(prompt)
        if measured > bound:
            record['status'] = 'too_large'
            continue
        try:
            response = complete(prompt, config['WORKSPACE_OUTPUT_TOKENS'], config['WORKSPACE_TIMEOUT'])
            if response is None:
                raise ValueError('compaction_no_response')
            choice = response.choices[0]
            if choice.finish_reason != 'stop' or choice.message.tool_calls:
                raise ValueError('compaction_incomplete_output')
            record['parts'].append({'rows':W.validate_pack(part, choice.message.content or '', state)})
            if len(record['parts']) == len(record['plan']):
                merged = merge_parts(group, record['parts'], key)
                after = counter.count_text(json.dumps(merged,ensure_ascii=False))
                record.update(status='complete' if after < before else 'no_gain',
                              before_tokens=before, after_tokens=after)
                if after < before:
                    record['rows'] = merged
        except (ValueError, TypeError, KeyError) as exc:
            record.update(status='failed', error=type(exc).__name__)
        workspace['version'] += 1
        workspace['last_compaction'] = {'pack_id':key[:16], 'status':record['status'],
            'part':index + 1, 'parts':len(record['plan']), 'input_tokens':measured,
            **{k:record[k] for k in ('before_tokens','after_tokens','error') if k in record}}
        return True
    return False
