"""保留原文位置的 Markdown 语法块与章节；不调用生成模型。"""

import hashlib
import bisect
import re
from importlib.metadata import version

from rag.text_source import line_starts


def config(file_type):
    import os
    return {'schema': 1, 'parser': 'markdown-it-py' if file_type in ('md','markdown') else 'plain-text',
            'parser_version': version('markdown-it-py'), 'rules_version': 2,
            'file_type': file_type, 'extensions': ['table','strikethrough'],
            'fallback_encoding': os.getenv('KB_TEXT_FALLBACK_ENCODING','')}


def markdown(text):
    from markdown_it import MarkdownIt
    starts = line_starts(text)
    if starts[-1] != len(text):
        starts.append(len(text))
    body, shift, spans = text, 0, []
    front = re.match(r'\A---\r?\n.*?\r?\n(?:---|\.\.\.)(?:\r?\n|$)', text, re.S)
    if front:
        spans.append({'start':0,'end':front.end(),'kind':'metadata','title':'元数据'})
        shift = len(line_starts(front[0])) - 1
        body = text[front.end():]
    tokens = MarkdownIt('commonmark').enable(['table','strikethrough']).parse(body)
    for i, token in enumerate(tokens):
        if token.level or not token.map or token.nesting == -1:
            continue
        kind = {'heading_open':'section_title','paragraph_open':'text','bullet_list_open':'list',
                'ordered_list_open':'list','blockquote_open':'quote','fence':'code','code_block':'code',
                'table_open':'table','html_block':'html','hr':'separator'}.get(token.type)
        if not kind:
            continue
        a,b = (starts[n+shift] for n in token.map)
        item = {'start':a,'end':b,'kind':kind,'title':kind}
        if kind == 'section_title':
            item.update(level=int(token.tag[1:]), title=tokens[i+1].content, basis='markdown_heading')
        if kind == 'code':
            item['language'] = token.info.strip()[:80]
        if kind == 'table':
            offsets = line_starts(text[a:b])
            if offsets[-1] != b-a:
                offsets.append(b-a)
            item['row_ranges'] = [[x,y] for x,y in zip(offsets,offsets[1:])]
            item['header_rows'] = [0,1]
        children=[]
        if kind in ('list','quote'):
            for sub in tokens[i+1:]:
                if sub.level == 0:
                    break
                if sub.type == 'list_item_open' and sub.map:
                    x,y = (starts[n+shift] for n in sub.map)
                    children.append({'start':x,'end':y,'kind':'list_item','title':text[x:y].strip()[:80]})
        item['children'] = children
        spans.append(item)
    return spans


def build(text, file_type, filename):
    spans = markdown(text) if file_type in ('md','markdown') else plain(text)
    from rag.document_config import TEXT_MAX_BLOCKS
    if len(spans)>TEXT_MAX_BLOCKS:
        raise ValueError('text_structure_too_many_blocks')
    blocks, cursor = [],0
    for span in [*spans, {'start':len(text),'end':len(text),'kind':'end'}]:
        if span['start'] < cursor or span['end'] > len(text):
            raise ValueError('invalid_text_parser_span')
        pieces=[]
        if cursor < span['start']:
            pieces.append({'start':cursor,'end':span['start'],'kind':'text' if text[cursor:span['start']].strip() else 'trivia'})
        if span['end'] > span['start']:
            pieces.append(span)
        for p in pieces:
            bid = 'T' + hashlib.sha256(f"{p['start']}:{p['end']}:{p['kind']}".encode()).hexdigest()[:16]
            structure = {k:v for k,v in p.items() if k not in ('start','end','kind')}
            structure.update(location_kind='text_span',source_start=p['start'],source_end=p['end'])
            if p['kind'] in ('trivia','separator'):
                structure['index_excluded']=True
            blocks.append({'block_id':bid,'ordinal':len(blocks),'page':None,'bbox':[],
                'kind':p['kind'],'text':text[p['start']:p['end']], 'structure':structure,'section_id':'','parent_id':''})
        cursor = span['end']
    nodes=[]
    node_lookup={}
    block_starts=[b['structure']['source_start'] for b in blocks]
    block_ends=[b['structure']['source_end'] for b in blocks]

    def add(kind,parent,start,end,title,basis,sources,**attrs):
        nid='N'+hashlib.sha256(f'{kind}:{start}:{end}:{parent}'.encode()).hexdigest()[:16]
        node={'node_id':nid,'parent_id':parent,'kind':kind,'ordinal':len(nodes),
              'depth':node_lookup[parent]['depth']+1 if parent else 0,
              'title':title,'preview':text[start:end].strip()[:120], 'basis':basis,
              'start_order':start,'end_order':end,'page':None,'end_page':None,
              'attributes':{'location_kind':'text_span',**attrs},'sources':sources}
        nodes.append(node)
        node_lookup[nid]=node
        return node

    def sources(start,end):
        return [{'block_id':b['block_id'],'start':max(start,b['structure']['source_start'])-b['structure']['source_start'],
                 'end':min(end,b['structure']['source_end'])-b['structure']['source_start']}
                for b in blocks[bisect.bisect_right(block_ends,start):bisect.bisect_left(block_starts,end)]]

    root=add('document','',0,len(text),filename,'document',sources(0,len(text)))
    headings=[b for b in blocks if b['kind']=='section_title']
    stack=[]
    for i,b in enumerate(headings):
        s=b['structure']; level=s['level']; start=s['source_start']
        end=next((h['structure']['source_start'] for h in headings[i+1:] if h['structure']['level']<=level),len(text))
        while stack and stack[-1]['attributes']['declared_level']>=level:
            stack.pop()
        n=add('section',stack[-1]['node_id'] if stack else root['node_id'],start,end,s['title'],
              s['basis'],sources(start,end),declared_level=level,heading_block=b['block_id'])
        stack.append(n)
    sections=[n for n in nodes if n['kind']=='section']
    for b in blocks:
        s=b['structure']; a,z=s['source_start'],s['source_end']
        owners=[n for n in sections if n['start_order']<=a<n['end_order']]
        owner=max(owners,key=lambda n:n['depth']) if owners else root
        s['section_path']=[n['attributes']['heading_block'] for n in owners]
        b['section_id']=owner['attributes'].get('heading_block','')
        if b['kind']=='section_title' or s.get('index_excluded'):
            continue
        node=add('paragraph' if b['kind']=='text' else b['kind'],owner['node_id'],a,z,
                 s.get('title') or b['text'].strip()[:80], s.get('basis','syntax_block'),sources(a,z),block_id=b['block_id'])
        s['node_id']=node['node_id']
        nesting=[node]
        for c in s.pop('children',[]):
            while not nesting[-1]['start_order']<=c['start']<c['end']<=nesting[-1]['end_order']:
                nesting.pop()
            while len(nesting)>1 and c['start']>=nesting[-1]['end_order']:
                nesting.pop()
            child=add(c['kind'],nesting[-1]['node_id'],c['start'],c['end'],c['title'],'syntax_block',sources(c['start'],c['end']))
            nesting.append(child)
    nodes.sort(key=lambda n:(n['start_order'],n['depth']))
    for ordinal,node in enumerate(nodes):
        node['ordinal']=ordinal
    from rag.document_hierarchy import validate
    validate(nodes,[],blocks)
    assert ''.join(b['text'] for b in blocks)==text
    return blocks,nodes,[],{'nodes':len(nodes),'sections':len(sections),'source_blocks':len(blocks),
                          'semantic_verified':False,'warnings':[]}


def plain(text):
    lines=text.splitlines(keepends=True)
    candidates=[]; pos=0
    for i,line in enumerate(lines):
        title=line.strip()
        match=re.fullmatch(r'(第[一二三四五六七八九十百零〇0-9]+[章节篇部])\s*\S.{0,68}',title)
        english=re.fullmatch(r'(?:chapter|section)\s+(?:\d+|[IVX]+)\b.{1,68}',title,re.I)
        numeric=re.fullmatch(r'(\d+(?:\.\d+)+)\s+\S.{0,68}',title)
        separated=(i==0 or not lines[i-1].strip()) and i+1<len(lines)
        if separated and not re.search(r'[。！？；;{}=]|https?://',title) and (match or english or numeric):
            family='chinese' if match else 'english' if english else 'decimal'
            level=(1 if match[1][-1] in '章篇部' else 2) if match else (numeric[1].count('.')+1 if numeric else 1)
            candidates.append({'start':pos,'end':pos+len(line),'kind':'section_title','title':title,
                               'level':level,'basis':'explicit_text_heading','family':family})
        pos+=len(line)
    # 至少两个同格式标题才使用层级；单独编号更可能是普通记录。
    headings=[{k:v for k,v in c.items() if k!='family'} for c in candidates
              if sum(x['family']==c['family'] for x in candidates)>=2]
    spans=[];cursor=0
    for h in [*headings,{'start':len(text),'end':len(text)}]:
        region=text[cursor:h['start']]
        for m in re.finditer(r'(?s)\S.*?(?=(?:\r\n|\r(?!\n)|(?<!\r)\n)[ \t]*(?:\r\n|\r(?!\n)|(?<!\r)\n)|\Z)',region):
            a=cursor+m.start(); b=cursor+m.end()
            # 段落不丢弃首行缩进；余下空行在统一构建时作为 trivia 保留。
            a=max(cursor,text.rfind('\n',cursor,a)+1,text.rfind('\r',cursor,a)+1)
            spans.append({'start':a,'end':b,'kind':'text','basis':'plain_paragraph'})
        if h['end']>h['start']:
            spans.append(h)
        cursor=h['end']
    return spans
