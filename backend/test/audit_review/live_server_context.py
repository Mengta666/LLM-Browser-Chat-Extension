"""手动运行：真实服务端会话验证，只创建带 audit_context 前缀的合成测试会话。"""

import json
import time
from pathlib import Path
from uuid import uuid4

import requests
from dotenv import dotenv_values


def main():
    config = dotenv_values(Path(__file__).resolve().parents[2] / 'config/.env')
    session = requests.Session()
    session.trust_env = False
    base = 'http://127.0.0.1:8000'
    model = config.get('AGENT_MODEL') or config.get('MEMORY_MODEL')
    chat_id = 'audit_context_' + uuid4().hex[:12]
    body = {'model': model, 'context_mode': 'server', 'chat_id': chat_id,
            'request_id': 'first', 'expected_last_seq': 0, 'stream': False,
            'messages': [{'role': 'user', 'content': '这是软件合成测试，不是个人事实，不应写入长期记忆。仅在本会话记住测试箱编号 TESTBOX-173，颜色是紫色。请简短确认。'}]}
    started = time.monotonic()
    first = session.post(base+'/v1/chat/completions', json=body, timeout=150)
    first.raise_for_status()
    assert first.json()['session_meta']['persisted']
    replay = session.post(base+'/v1/chat/completions', json=body, timeout=10)
    replay.raise_for_status()
    assert replay.json()['choices'] == first.json()['choices']
    page = session.get(base+f'/v1/sessions/{chat_id}/messages?limit=1', timeout=5).json()
    assert page['last_seq'] == 2 and page['has_more']
    second_body = {**body, 'request_id': 'second', 'expected_last_seq': 2,
                   'messages': [{'role': 'user', 'content': '仅根据本会话此前内容，说出测试箱编号和颜色。'}]}
    second = session.post(base+'/v1/chat/completions', json=second_body, timeout=150)
    second.raise_for_status()
    answer = second.json()['choices'][0]['message']['content']
    assert 'TESTBOX-173' in answer and '紫' in answer, 'Conversation fact was not retained'
    conflict = session.post(base+'/v1/chat/completions', json={**second_body, 'request_id': 'stale'}, timeout=5)
    assert conflict.status_code == 409
    saved = session.get(base+f'/v1/sessions/{chat_id}/messages?limit=100', timeout=5).json()
    assert len(saved['messages']) == 4
    print(json.dumps({'chat_id': chat_id, 'rounds': 2, 'replay_identical': True,
        'fact_retained': True, 'message_count': 4, 'stale_status': conflict.status_code,
        'elapsed_s': round(time.monotonic()-started, 2)}, ensure_ascii=False), flush=True)

    kb_chat = 'audit_context_kb_' + uuid4().hex[:12]
    response = session.post(base+'/v1/chat/completions', json={**body, 'chat_id': kb_chat,
        'kb_id': 'kb_171d1dfb', 'stream': True,
        'messages': [{'role': 'user', 'content': '这是合成知识库测试，不是个人事实。请查询绑定知识库：Atlas-R7 送料轴承是什么型号？请给出引用。'}]}, stream=True, timeout=150)
    response.raise_for_status()
    text = ''; meta = None; steps = []
    for line in response.iter_lines():
        if not line.startswith(b'data: {'):
            continue
        event = json.loads(line[6:])
        assert not event.get('error'), event.get('error')
        meta = event.get('session_meta', meta)
        if event.get('enhancement_step'):
            steps.append(event['enhancement_step'])
        text += ''.join(choice.get('delta', {}).get('content') or '' for choice in event.get('choices', []))
    assert meta and meta['persisted']
    assert 'RB-208' in text or 'RB－208' in text
    assert any(step.get('result_count', 0) for step in steps)
    print(json.dumps({'chat_id': kb_chat, 'kb_answer_correct': True, 'persisted': True,
                      'tool_steps': len(steps)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
