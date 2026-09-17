"""显式 --run 才执行：仅操作本次新建的合成知识库，保留数据供前端核验。"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import requests
from dotenv import dotenv_values


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--run', action='store_true')
    args.add_argument('--resume', type=Path, help='继续本脚本创建但尚未通过的测试库')
    options = args.parse_args()
    if not options.run:
        args.error('真实服务测试会创建合成数据，请显式指定 --run')
    backend = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(backend))
    config = dotenv_values(backend / 'config/.env')
    session = requests.Session()
    session.trust_env = False
    # 仅影响本测试进程的已配置服务，不改系统代理或真实 .env。
    from urllib.parse import urlparse
    direct_hosts = [urlparse(config.get(key) or '').hostname for key in
                    ('EMBEDDING_BASE_URL', 'KB_RERANK_API_URL', 'QDRANT_URL')]
    os.environ['NO_PROXY'] = ','.join(filter(None, [os.environ.get('NO_PROXY'), '127.0.0.1', 'localhost', *direct_hosts]))
    base = 'http://127.0.0.1:8000'
    label = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    owned = set()
    events = json.loads(options.resume.read_text(encoding='utf-8')) if options.resume else []
    output = options.resume or backend.parent / 'output' / f'kb-lifecycle-{label}.json'
    output.parent.mkdir(parents=True, exist_ok=True)

    def record(event, **data):
        entry = {'event': event, **data}
        events.append(entry)
        output.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    def request(method, path, expected=200, **kwargs):
        if method != 'GET' and path.startswith('/v1/kb/'):
            assert path.split('/')[3] in owned, '只能修改本次创建的知识库'
            assert not path.endswith('/hard'), '真实测试不执行不可逆删除'
        response = session.request(method, base + path, timeout=180, **kwargs)
        assert response.status_code == expected, f'{method} {path}: HTTP {response.status_code}, expected {expected}'
        return response.json()

    def upload(kb_id, filename, content, expected=200):
        return request('POST', f'/v1/kb/{kb_id}/docs', expected,
                       files={'file': (filename, content.encode('utf-8'), 'text/plain')})

    def wait_doc(kb_id, doc_id, expected='indexed'):
        deadline = time.monotonic() + 180
        previous = None
        while time.monotonic() < deadline:
            state = request('GET', f'/v1/kb/{kb_id}/docs/{doc_id}/status')
            signature = (state['status'], state.get('sync_pending'))
            if signature != previous:
                record('doc_status', kb_id=kb_id, doc_id=doc_id, **state)
                previous = signature
            if state['status'] in ('indexed', 'failed') and not state['sync_pending']:
                assert state['status'] == expected, f'Unexpected final state: {state["status"]}'
                return state
            time.sleep(1)
        raise TimeoutError('索引未在三分钟内完成')

    try:
        schemas = request('GET', '/openapi.json')['components']['schemas']
        assert 'sync_pending' in schemas['DocStatusResponse']['properties']
        current_kbs = request('GET', '/v1/kb')
        original_ids = {k['kb_id'] for k in current_kbs}
        resumed = [event for event in events if event['event'] == 'created']
        for suffix in ('主库', '归属校验库'):
            if options.resume:
                created = next(k for k in resumed if k['name'].endswith('-' + suffix))
                assert created['name'].startswith('审计-知识库一致性-')
                assert any(k['kb_id'] == created['kb_id'] and k['name'] == created['name'] for k in current_kbs)
            else:
                created = request('POST', '/v1/kb', json={
                    'name': f'审计-知识库一致性-{label}-{suffix}',
                    'description': '合成测试数据；本轮创建并保留，未改动已有知识库。',
                })
                record('created', **created)
            owned.add(created['kb_id'])
            if suffix == '主库':
                main_kb = created['kb_id']
            else:
                other_kb = created['kb_id']

        manual = '\n\n'.join(
            f'## 检查单 {i}\n合成设备 LIFECYCLE-R7 的备用轴承型号是 LC-7319，'
            '库存位置是 Z区第17格，标准更换周期为840小时。这些编号仅供软件测试，不代表真实设备。'
            '检查人员应核对轴承型号、库存位置和运行时间，断电后进行更换，记录测试结果。'
            for i in range(18))
        removed_text = '合成设备 REMOVE-T8 的独立删除验证代码是 RM-5821。本文用于验证单独删除的文档不会随整库还原而复活。'
        existing = {d['filename']: d for d in request('GET', f'/v1/kb/{main_kb}/docs')}
        doc_id = (existing.get('lifecycle-r7-manual.md') or upload(main_kb, 'lifecycle-r7-manual.md', manual))['doc_id']
        removed_doc = (existing.get('independent-deletion.txt') or upload(main_kb, 'independent-deletion.txt', removed_text))['doc_id']
        assert wait_doc(main_kb, doc_id)['chunk_count'] > 1
        wait_doc(main_kb, removed_doc)
        upload(main_kb, 'duplicate-name.md', manual, 409)
        request('DELETE', f'/v1/kb/{other_kb}/docs/{doc_id}', 404)
        assert request('GET', f'/v1/kb/{other_kb}/docs/{doc_id}/status')['status'] == 'not_found'
        assert request('GET', f'/v1/kb/{main_kb}/docs/{doc_id}/status')['status'] == 'indexed'
        record('duplicate_and_parent_guards', duplicate_http=409, wrong_parent_http=404)

        from rag import kb

        query = 'LIFECYCLE-R7 备用轴承的型号、库存位置、更换周期分别是什么？'
        def search(stage, should_find):
            results = kb.search_kb(main_kb, query, top_k=8)
            assert bool(results) == should_find, stage
            if should_find:
                assert any(r['doc_id'] == doc_id and 'LC-7319' in r['content'] for r in results)
                if stage == 'after_restore':
                    assert all(r['doc_id'] != removed_doc for r in results)
                assert all(r.get('index_run_id') for r in results)
                assert any(r.get('window_size', 1) > 1 for r in results)
                assert any('rerank_score' in r for r in results), '未观察到真实精排结果'
            record('search', stage=stage, count=len(results),
                   doc_ids=sorted({r['doc_id'] for r in results}),
                   rerank_observed=any('rerank_score' in r for r in results),
                   max_window=max((r.get('window_size', 1) for r in results), default=0))

        search('before_delete', True)
        request('DELETE', f'/v1/kb/{main_kb}/docs/{removed_doc}')
        request('DELETE', f'/v1/kb/{main_kb}/docs/{removed_doc}')
        assert removed_doc not in {d['doc_id'] for d in request('GET', f'/v1/kb/{main_kb}/docs')}
        assert all(r['doc_id'] != removed_doc for r in kb.search_kb(
            main_kb, 'REMOVE-T8 RM-5821 独立删除验证代码', top_k=8))
        record('independent_delete', doc_id=removed_doc, idempotent=True)
        deleted = request('DELETE', f'/v1/kb/{main_kb}')
        assert main_kb not in {k['kb_id'] for k in request('GET', '/v1/kb')}
        assert main_kb in {k['kb_id'] for k in request('GET', '/v1/kb/trash')}
        request('GET', f'/v1/kb/{main_kb}/docs', 404)
        upload(main_kb, 'blocked.txt', '已删除的知识库必须拒绝上传', 404)
        search('deleted', False)
        record('soft_delete', kb_id=main_kb, **deleted)
        restored = request('POST', f'/v1/kb/{main_kb}/restore')
        assert not restored['sync_pending']
        assert request('POST', f'/v1/kb/{main_kb}/restore')['ok']
        restored_ids = {d['doc_id'] for d in request('GET', f'/v1/kb/{main_kb}/docs')}
        assert doc_id in restored_ids and removed_doc not in restored_ids
        assert main_kb not in {k['kb_id'] for k in request('GET', '/v1/kb/trash')}
        search('after_restore', True)
        record('restore', kb_id=main_kb, independent_doc_stays_deleted=True, idempotent=True)

        for attempt in range(2):
            failed_doc = upload(main_kb, 'empty-content.txt', ' \n \n')['doc_id']
            wait_doc(main_kb, failed_doc, 'failed')
            record('failed_content_reupload', attempt=attempt + 1, doc_id=failed_doc)

        chat_id = 'audit_kb_lifecycle_' + uuid4().hex[:12]
        record('chat_started', chat_id=chat_id, kb_id=main_kb)
        response = session.post(base + '/v1/chat/completions', json={
            'model': config.get('AGENT_MODEL') or config.get('MEMORY_MODEL'),
            'context_mode': 'server', 'chat_id': chat_id, 'kb_id': main_kb,
            'request_id': 'lifecycle-query', 'expected_last_seq': 0, 'stream': True,
            'messages': [{'role': 'user', 'content':
                '这是合成软件测试，不是个人事实，不要保存为长期记忆。请调用绑定知识库查询，'
                '不要联网：LIFECYCLE-R7 备用轴承的型号、库存位置和更换周期是什么？请给出引用。'}],
        }, stream=True, timeout=180)
        assert response.status_code == 200, f'Chat HTTP {response.status_code}'
        answer, meta, steps = '', None, []
        for line in response.iter_lines():
            if not line.startswith(b'data: {'):
                continue
            event = json.loads(line[6:])
            assert not event.get('error'), 'Chat SSE error'
            meta = event.get('session_meta', meta)
            if event.get('enhancement_step'):
                step = event['enhancement_step']
                steps.append(step)
                record('chat_step', status=step.get('status'), tool=step.get('type'), result_count=step.get('result_count'))
            answer += ''.join(c.get('delta', {}).get('content') or '' for c in event.get('choices', []))
        assert meta and meta['persisted']
        assert any(step.get('result_count', 0) > 0 for step in steps)
        assert all(word in answer for word in ('LC-7319', '17', '840'))
        record('chat_passed', chat_id=chat_id, persisted=True, tool_steps=len(steps), answer_correct=True)
        final_ids = {k['kb_id'] for k in request('GET', '/v1/kb')}
        assert original_ids <= final_ids
        record('passed', kb_ids=sorted(owned), prior_kbs_still_listed=True, output=str(output))
    except Exception as exc:
        record('failed', error_type=type(exc).__name__, kb_ids=sorted(owned), output=str(output))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
