"""仅供本地浏览器验收：真实 agent 路由与循环，决策替身，不读取 .env。"""
import os
import socket
import sys
from pathlib import Path

import dotenv
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

dotenv.load_dotenv = lambda *a, **k: False
os.environ['MODEL_BASE_URL'] = 'http://127.0.0.1:1/v1'
os.environ['OPENAI_API_KEY'] = 'offline-test-placeholder'
os.environ['AGENT_DEBUG_SCREENSHOT'] = '0'
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules['tiktoken'] = None

from observability.logger import StructuredLogger
StructuredLogger._write_file = lambda *a: None
from agent import loop
from api.agent import router

pages, trace = {}, []
plans = {
    'normal': [('click', 'submit', {}), ('type', 'text', {'text': 'ABCD'}),
               ('select', 'choice', {'option_text': 'Beta'}), ('clear', 'text', {})],
    'single': [('click', 'submit', {})],
    'failures': [('select', 'choice', {'option_text': 'Missing'}),
                 ('clear', 'readonly', {}), ('type', 'revert', {'text': 'new'}),
                 ('select', 'revert-select', {'option_text': 'Beta'})],
}


def decide(session):
    plan = plans[session.task]
    if session.current_step >= len(plan):
        return {'action': {'type': 'task_complete', 'summary': '合成流程验收结束',
                           'success': session.task != 'failures'}}
    kind, html_id, params = plan[session.current_step]
    element = next(e for e in pages[session.session_id]['interactive_elements'] if e['html_id'] == html_id)
    return {'action': {'type': kind, 'index': element['id'], **params}}


loop._call_llm = decide
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
app.include_router(router)


@app.middleware('http')
async def capture(request: Request, call_next):
    if request.url.path.startswith('/v1/agent/'):
        payload = await request.json()
        if 'page_state' in payload:
            pages[payload['session_id']] = payload['page_state']
        trace.append({'path': request.url.path, 'session_id': payload.get('session_id'),
                      'observation_id': payload.get('page_state', {}).get('observation_id'),
                      'action_result': payload.get('action_result')})
    return await call_next(request)


@app.get('/audit/state')
def state():
    return {'trace': trace, 'sessions': {sid: {'task': s.task, 'status': s.status.value}
            for sid, s in loop._sessions.items()}}


if __name__ == '__main__':
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    print(f'PORT={listener.getsockname()[1]}', flush=True)
    server.run(sockets=[listener])
