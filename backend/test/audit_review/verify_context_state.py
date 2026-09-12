"""只读核验迁移前原消息、序号一致性和新进程的合成会话状态。"""

import json
import sqlite3
from pathlib import Path

import requests

backend = Path(__file__).resolve().parents[2]
backup = backend / 'data/chat_history.before_server_context.20260912T162534Z.sqlite3'
current = backend / 'data/chat_history.sqlite3'
with sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True) as old, sqlite3.connect(current.as_uri()+'?mode=ro',uri=True) as new:
    columns = 'message_id,chat_id,role,content,created_at,tools'
    original = old.execute('SELECT '+columns+' FROM chat_messages').fetchall()
    for row in original:
        assert new.execute('SELECT '+columns+' FROM chat_messages WHERE message_id=?',(row[0],)).fetchone() == row
    assert new.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert new.execute('SELECT COUNT(*) FROM chat_messages WHERE seq IS NULL').fetchone()[0] == 0
    assert new.execute('SELECT COUNT(*) FROM chat_sessions s WHERE last_seq != COALESCE((SELECT MAX(seq) FROM chat_messages m WHERE m.chat_id=s.chat_id),0)').fetchone()[0] == 0
    print(json.dumps({'original_messages_preserved':len(original),'integrity':'ok',
        'current_messages':new.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0],
        'sequence_invariants':True,'backup':str(backup)},ensure_ascii=False))
session = requests.Session()
session.trust_env = False
base = 'http://127.0.0.1:8000'
assert session.get(base+'/v1/sessions/capabilities',timeout=5).json()['server_context']
ui = session.get(base+'/v1/sessions/audit_context_ui_20260912/messages?limit=100',timeout=5).json()
assert ui['context_mode'] == 'server' and not ui['active_request_id']
assert ui['last_seq'] == 4 and len(ui['messages']) == 4
assert 'UI-204' in ui['messages'][-1]['content'] and '蓝' in ui['messages'][-1]['content']
print(json.dumps({'real_extension_rounds':2,'persisted':True,'history_restored':True,'last_seq':ui['last_seq']}))
