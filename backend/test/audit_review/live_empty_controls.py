"""stdin 接收隔离页面观察，仅调用配置模型；不写会话、记忆或真实网站。"""
import json
import re
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values
from openai import OpenAI

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
from agent.context_builder import build_messages
from agent.state import AgentSession, PageState


def main():
    config = dotenv_values(BACKEND / 'config' / '.env')
    model = config.get('AGENT_MODEL') or config.get('MEMORY_MODEL')
    packet = json.load(sys.stdin)
    state = PageState(**packet['state'])
    results = []
    with OpenAI(base_url=config['MODEL_BASE_URL'], api_key=config['OPENAI_API_KEY'],
                max_retries=0, http_client=httpx.Client(trust_env=False, timeout=55)) as client:
        for task, expected in [('点击签到按钮。', 'control-a'), ('点击兑换按钮。', 'control-b'), ('点击导出按钮。', 'control-c')]:
            session = AgentSession(session_id='isolated-empty-model', task=task, model=model)
            try:
                response = client.chat.completions.create(model=model, messages=build_messages(session, state),
                                                          max_tokens=4096, timeout=55)
                raw = response.choices[0].message.content or ''
                raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.S).strip()
                if raw.startswith('```'):
                    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw).strip()
                action = json.loads(raw)['action']
                selected = next((el for el in state.interactive_elements if el['id'] == action.get('index')), {})
                results.append({'task': task, 'action': action.get('type'), 'index': action.get('index'),
                                'selected': selected.get('html_id'), 'expected': expected,
                                'passed': action.get('type') == 'click' and selected.get('html_id') == expected})
            except Exception as exc:
                results.append({'task': task, 'passed': False, 'error_type': type(exc).__name__})
    print(json.dumps({'model': model, 'results': results}, ensure_ascii=False))


if __name__ == '__main__':
    main()
