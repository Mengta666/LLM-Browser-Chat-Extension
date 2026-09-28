"""有检查点的状态提取；候选状态通过完整预算校验后才能原子发布。"""

import hashlib
import json
import os
import re
import time

from agent.memory import chat_compact as compact, config as C, context_state as state
from agent.token_utils import RequestTokenCounter
from storage import chat_store as store
from observability.logger import get_logger

_log = get_logger('chat_context')


class CompactionError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def new_counter(deadline=None):
    return RequestTokenCounter(os.getenv('CHAT_COMPACT_MODEL') or compact._COMPACT_MODEL,
                               deadline=deadline, time_budget=30.0)


def _input_limit(counter):
    window = min(C.CHAT_CONTEXT_LENGTH, counter.model_window or C.CHAT_CONTEXT_LENGTH)
    return min(C.CHAT_COMPACT_BATCH_TOKENS,
               window - C.CHAT_COMPACT_MAX_OUTPUT_TOKENS - C.CHAT_CONTEXT_SAFETY_TOKENS)


def _pieces(records, batch):
    return [{'seq': p['seq'], 'role': records[p['seq']]['role'], 'offset': p['offset'],
             'text': records[p['seq']]['content'][p['offset']:min(p['end'], records[p['seq']].get('source_length', p['end']))],
             'status': records[p['seq']].get('status', 'completed')} for p in batch]


def _request(working, pieces, repair='', *, counter):
    limit = min(C.CHAT_CONTEXT_STATE_MAX_TOKENS, max(0, _input_limit(counter) // 4))
    try:
        selected = state.select(working, target=limit, limit=limit, count=counter.count_text,
                                extraction=True, query=' '.join(p['text'][:2000] for p in pieces),
                                required_ids=re.findall(r'\bs[1-9][0-9]*\b', repair))
    except state.StateError as exc:
        raise CompactionError(exc.code) from None
    return [{'role': 'system', 'content': state.INSTRUCTIONS},
            {'role': 'user', 'content': state.prompt(selected, pieces, repair)}]


def split_batch(batch, records):
    if len(batch) > 1:
        mid = len(batch) // 2
        # 尽量在已完成轮次后拆分，不能跨边界时才拆消息。
        boundaries = [i for i in range(1, len(batch)) if records[batch[i-1]['seq']]['role'] == 'assistant']
        if boundaries:
            mid = min(boundaries, key=lambda i: abs(i - mid))
        return [batch[:mid], batch[mid:]]
    piece = batch[0]
    if piece['end'] - piece['offset'] <= 128:
        raise CompactionError('compaction_input_budget_exceeded')
    mid = (piece['offset'] + piece['end']) // 2
    text = records[piece['seq']]['content']
    boundary = text.rfind('\n', piece['offset'] + (mid - piece['offset']) // 2, mid)
    if boundary >= 0:
        mid = boundary + 1
    return [[{**piece, 'end': mid}], [{**piece, 'offset': mid}]]


def make_plan(messages, *, working=None, counter=None):
    counter = counter or new_counter()
    working = state.load(working) or state.empty()
    records = {m['seq']: m for m in messages}
    if counter(_request(working, [], counter=counter)) + 64 > _input_limit(counter):
        raise CompactionError('compaction_configuration_error')
    groups, group = [], []
    for message in messages:
        group.append({'seq': message['seq'], 'offset': 0, 'end': len(message['content'])})
        if message['role'] == 'assistant':
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    plan, batch = [], []
    while groups:
        if counter.deadline is not None and time.monotonic() >= counter.deadline:
            raise CompactionError('compaction_timeout')
        group = groups.pop(0)
        candidate = batch + group
        measured = counter(_request(working, _pieces(records, candidate), counter=counter))
        if measured <= _input_limit(counter):
            batch = candidate
        elif batch:
            plan.append(batch)
            batch = []
            groups.insert(0, group)
        else:
            groups[:0] = split_batch(group, records)
    if batch:
        plan.append(batch)
    fingerprint = hashlib.sha256(json.dumps({
        'schema': state.SCHEMA, 'instructions': state.INSTRUCTIONS, 'batch': C.CHAT_COMPACT_BATCH_TOKENS,
        'window': C.CHAT_CONTEXT_LENGTH, 'output': C.CHAT_COMPACT_MAX_OUTPUT_TOKENS,
        'safety': C.CHAT_CONTEXT_SAFETY_TOKENS,
        'model': os.getenv('CHAT_COMPACT_MODEL') or compact._COMPACT_MODEL,
        'tokenizer': (C.CHAT_TOKENIZER_URL, C.CHAT_TOKENIZER_MODEL, C.CHAT_TOKENIZER_SAFETY_RATIO),
    }, ensure_ascii=False).encode()).hexdigest()
    return plan, fingerprint


def run(chat_id, snapshot, plan, fingerprint, *, request_id='', attempt=0, deadline=None, validate=None,
        counter=None, query=''):
    deadline = min(deadline or float('inf'), time.monotonic() + C.CHAT_COMPACT_TIMEOUT)
    job = store.claim_compaction(chat_id, request_id, attempt, snapshot, plan, fingerprint)
    counter = counter or new_counter(deadline)
    counter.deadline = deadline
    records = {m['seq']: m for m in snapshot['messages']}

    def check():
        current = store.get_compaction(chat_id)
        if not current or current['generation'] != job['generation'] or current['status'] != 'running':
            raise CompactionError('compaction_interrupted')
        if time.monotonic() >= deadline:
            raise CompactionError('compaction_timeout')

    try:
        yield store.compaction_progress(job)
        working = state.load(job['state_json']) or state.empty()
        plan = job['plan']
        splits = 0
        while job['next_batch'] < len(plan):
            check()
            pieces = _pieces(records, plan[job['next_batch']])
            repair = ''
            replan = False
            for validation_try in range(2):
                check()
                messages = _request(working, pieces, repair, counter=counter)
                measured = counter(messages)
                if measured > _input_limit(counter):
                    replan = True
                    break
                job = store.checkpoint_compaction(job, phase='repair' if validation_try else 'extract',
                                                 repair_attempts=validation_try)
                for trial in range(max(1, min(5, C.CHAT_COMPACT_MAX_ATTEMPTS))):
                    check()
                    job = store.checkpoint_compaction(job, calls=job['calls'] + 1)
                    yield store.compaction_progress(job)
                    try:
                        output = compact._summarize_llm(state.INSTRUCTIONS, messages[1]['content'],
                            timeout=max(.1, min(C.CHAT_COMPACT_CALL_TIMEOUT, deadline - time.monotonic())), strict=True)
                        break
                    except compact.SummaryError as exc:
                        if exc.code == 'compaction_output_truncated':
                            replan = True
                            break
                        if not exc.retryable or trial + 1 >= max(1, min(5, C.CHAT_COMPACT_MAX_ATTEMPTS)):
                            raise CompactionError(exc.code) from None
                        job = store.checkpoint_compaction(job, error_code=exc.code)
                        yield store.compaction_progress(job)
                        until = min(deadline, time.monotonic() + 2 ** trial)
                        while time.monotonic() < until:
                            check()
                            time.sleep(max(0, min(.1, until - time.monotonic())))
                check()
                if replan:
                    break
                try:
                    candidate = state.merge(working, output, pieces)
                    break
                except state.StateError as exc:
                    _log.warn('compaction_state_patch_rejected', data={'chat_id': chat_id, 'job_id': job['job_id'],
                              'batch': job['next_batch'], 'validation_try': validation_try + 1,
                              'code': exc.code, 'reason': exc.detail})
                    if validation_try:
                        raise CompactionError(exc.code) from None
                    repair = exc.detail
            if replan:
                if splits >= 4:
                    raise CompactionError('compaction_replan_exhausted')
                try:
                    remaining = split_batch(plan[job['next_batch']], records)
                except CompactionError:
                    raise CompactionError('compaction_replan_exhausted') from None
                plan = plan[:job['next_batch']] + remaining + plan[job['next_batch'] + 1:]
                splits += 1
                job = store.checkpoint_compaction(job, plan_json=json.dumps(plan), phase='replan', repair_attempts=0)
                yield store.compaction_progress(job)
                continue
            working = candidate
            overview = json.loads(output).get('overview', '')
            if not isinstance(overview, str) or not overview.strip() or counter.count_text(overview) > 120:
                overview = '；'.join(f'M{p["seq"]}: {p["text"][:40]}' for p in pieces[:3])
                while overview and counter.count_text(overview) > 120:
                    overview = overview[:len(overview) // 2]
            job = store.checkpoint_compaction(job, state_json=json.dumps(working, ensure_ascii=False),
                                             next_batch=job['next_batch'] + 1, phase='extract',
                                             error_code='', repair_attempts=0,
                                             coverage=plan[job['next_batch']], overview=overview)
            _log.info('compaction_state_extracted', data={'chat_id': chat_id, 'job_id': job['job_id'],
                      'batch': job['next_batch'], 'item_count': len(working['items']),
                      'input_tokens': measured, **counter.last})
            yield store.compaction_progress(job)
        check()
        job = store.checkpoint_compaction(job, phase='render')
        yield store.compaction_progress(job)
        rendered = state.render(state.select(working, target=C.CHAT_COMPACT_SUMMARY_MAX_TOKENS,
                                limit=C.CHAT_CONTEXT_STATE_MAX_TOKENS, count=counter.count_text, query=query))
        job = store.checkpoint_compaction(job, summary=rendered, phase='validate')
        if validate:
            validate(job)
        check()
        job = store.publish_compaction(job)
        _log.info('context_summary_published', data={'chat_id': chat_id, 'upto_seq': job['through_seq'],
                  'batch_count': len(plan), 'calls': job['calls'], 'schema': state.SCHEMA})
        yield store.compaction_progress(job)
    except (CompactionError, state.StateError, store.SessionError) as exc:
        try:
            job = store.checkpoint_compaction(job, status='failed', error_code=exc.code)
            yield store.compaction_progress(job)
        except store.SessionError:
            pass
        _log.warn('context_compaction_failed', data={'chat_id': chat_id, 'code': exc.code, 'next_batch': job['next_batch']})
        raise CompactionError(exc.code) from None
    finally:
        current = store.get_compaction(chat_id)
        if current and current['generation'] == job['generation'] and current['status'] == 'running':
            store.checkpoint_compaction(job, status='interrupted', error_code='compaction_interrupted')
