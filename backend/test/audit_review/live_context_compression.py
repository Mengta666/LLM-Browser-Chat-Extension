"""手动验收真实摘要模型；会话库隔离在临时目录，不写用户历史或长期记忆。"""

import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):
    os.environ.pop(name, None)

from agent.memory import chat_compact, chat_context, config
from storage import chat_store


def main():
    create = chat_compact._llm_client.chat.completions.create
    def measured_create(**kwargs):
        response = create(**kwargs)
        choice = response.choices[0]
        print(json.dumps({'summary_call':True,'model':kwargs['model'],'max_tokens':kwargs.get('max_tokens'),
            'finish_reason':choice.finish_reason,'content_tokens_estimate':chat_context.estimate_text_tokens(choice.message.content or ''),
            'usage':response.usage.model_dump() if response.usage else None},ensure_ascii=False),flush=True)
        return response
    chat_compact._llm_client.chat.completions.create = measured_create
    folder = Path(tempfile.gettempdir()) / ('browser-agent-compress-' + uuid4().hex)
    folder.mkdir()
    chat_store._DB_PATH = folder / 'history.sqlite3'
    text = '合成任务：测试箱 TESTBOX-173 的紧急代码为 ZX-491，下一步只读检查，不得删除。\n'
    text += '\n'.join(f'填充记录 {i}：仅为软件测试，不是用户属性，不需要逐条保留。' for i in range(600))
    text += '\n尾部纠正：测试箱颜色不是红色，是紫色。'
    for i in range(4):
        turn = chat_store.begin_turn('audit_live_compact',str(i),str(i),2*i,text if i == 0 else '请保留前面的任务和纠正。')
        chat_store.complete_turn('audit_live_compact',str(i),turn['attempt'],'确认，按合成测试约束处理。',[])
    before = chat_store.context_snapshot('audit_live_compact')
    assert chat_context.compress('audit_live_compact'), 'Real summary did not publish'
    after = chat_store.context_snapshot('audit_live_compact')
    assert after['upto_seq'] == 2 and len(after['messages']) == 6
    assert '紫' in after['summary'] and 'ZX-491' in after['summary']
    messages = chat_context.prepare('audit_live_compact',{'role':'user','content':'根据此前摘要回答测试箱紧急代码、最终颜色和禁止的操作。'},['只根据提供的合成会话回答。'])
    response = chat_compact._llm_client.chat.completions.create(model=chat_compact._COMPACT_MODEL,messages=messages,max_tokens=config.CHAT_MAX_OUTPUT_TOKENS,timeout=120)
    answer = response.choices[0].message.content or ''
    assert 'ZX-491' in answer and '紫' in answer and '删除' in answer
    print(json.dumps({'real_compaction':True,'model':chat_compact._COMPACT_MODEL,
        'before_estimate':chat_context.request_tokens(before['messages']),
        'after_estimate':chat_context.request_tokens(messages),'summary_upto_seq':after['upto_seq'],
        'summary_version':after['version'],'summary_tokens_estimate':chat_context.estimate_text_tokens(after['summary']),
        'tail_messages':len(after['messages']),'fact_and_correction_retained':True,'database':str(chat_store._DB_PATH)},ensure_ascii=False),flush=True)
    models = chat_compact._llm_client.models.list()
    for model in models.data:
        if model.id == chat_compact._COMPACT_MODEL:
            fields = model.model_dump()
            print(json.dumps({'model':model.id,'reported_context':{key:fields[key] for key in ('max_model_len','context_length','max_context_length') if key in fields},'configured_context':config.CHAT_CONTEXT_LENGTH},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
