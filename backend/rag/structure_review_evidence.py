"""文档内结构证据：只聚合观测，不用字号或编号直接决定章节等级。"""
import re
import unicodedata
from collections import defaultdict

from rag.document_sections import marker


def title_key(text):
    """规范化标题文字，供目录与结构候选进行字面匹配。"""
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',text)).casefold()


def catalog(anchors):
    """聚合锚点的编号和样式特征，形成供模型参考的观测分组。"""
    groups=[]; numbering={};styles=[]
    for a in anchors:
        a['group_refs']=[]
        text=a['text'].strip()
        if not text or len(text)>240:
            continue
        number=marker(text)
        if number:
            key=(number['family'],len(number['parts']))
            if key not in numbering:
                group={'id':f'N{len(numbering)+1}','kind':'numbering','family':key[0],
                       'component_count':key[1],'members':[]}
                numbering[key]=group;groups.append(group)
            group=numbering[key];group['members'].append(a['id']);a['group_refs'].append(group['id'])
        style=a.get('features',{})
        size=style.get('size',style.get('height'))
        if not isinstance(size,(int,float)) or size<=0:
            continue
        signature=(style.get('source'),style.get('bold'),style.get('italic'))
        group=next((g for g in styles if g['signature']==signature and abs(g['size']-size)<=.08*max(g['size'],size)),None)
        if group is None:
            group={'id':f'S{len(styles)+1}','kind':'style','signature':signature,'size':round(size,3),'members':[]}
            styles.append(group);groups.append(group)
        group['members'].append(a['id']);a['group_refs'].append(group['id'])
    return groups


def window_evidence(groups, anchors, window):
    """提取当前窗口相关的文档级特征组，并注明这些观测不代表预设章节层级。"""
    lookup={a['id']:a for a in anchors}
    relevant={g for a in window for g in a.get('group_refs',[])}
    # 同批相关组给代表样本，其余只给实际观测的编号体系；均无预设 level。
    selected=[g for g in groups if g['id'] in relevant or g['kind']=='numbering']
    rows=[]
    for group in selected:
        ids=group['members']
        samples=list(dict.fromkeys([ids[0],ids[len(ids)//2],ids[-1]])) if group['id'] in relevant else []
        rows.append({k:v for k,v in group.items() if k not in ('members','signature')} |
                    {'count':len(ids),'style_signature':list(group['signature']) if 'signature' in group else None,
                     'samples':[{'page':lookup[i]['page'],'text':lookup[i]['text'][:120],'context_only':True} for i in samples]})
    return {'groups':rows,'total_groups':len(groups),'shown_groups':len(rows),
            'meaning':'observed lexical/style groups, not heading roles or predefined levels'}


def attach(result, anchors, groups, pages):
    """为审阅结果补充样式、编号及目录关联证据。"""
    lookup={a['id']:a for a in anchors};nodes=result['nodes']
    by_anchor={n['heading_anchor']:n for n in nodes}
    profiles=[]
    for group in groups:
        members=[by_anchor[a] for a in group['members'] if a in by_anchor]
        if not members:
            continue
        levels=sorted({n['section_level'] for n in members if n['section_level'] is not None})
        profiles.append({**group,'section_members':[n['heading_anchor'] for n in members],
                         'observed_levels':levels,
                         'status':'ambiguous' if len(levels)>1 or any(n['section_level'] is None for n in members) else 'inferred',
                         'meaning':'model levels grouped by document evidence; not a universal level mapping'})
    for node in nodes:
        a=lookup[node['heading_anchor']]
        node['hierarchy_evidence']={'groups':a.get('group_refs',[]),'numbering':marker(node['title']),
                                    'style':a.get('features',{}),'rule_candidate':a.get('rule_heading')}
    result['heading_systems']=profiles
    result['toc_links']=toc_links(result,anchors,pages)
    return result


def toc_links(result, anchors, pages):
    """依据标题和印刷页码建立目录项与候选节点的来源关联。"""
    lookup={a['id']:a for a in anchors};positions={a['id']:i for i,a in enumerate(anchors)}
    titles=defaultdict(list)
    for node in result['nodes']:
        titles[title_key(node['title'])].append(node)
    labels=defaultdict(list)
    for page in pages:
        if page.get('printed_page'):
            labels[str(page['printed_page']).casefold()].append(page['page'])
    links=[]
    for material in result['materials']:
        if material['role']!='toc':
            continue
        for source in material['sources']:
            key=f"{source['block_id']}@{source['start']}:{source['end']}"
            text=lookup[key]['text']
            match=re.fullmatch(r'\s*(.+?)(?:\.{2,}|…+|\s{2,}|\t+)\s*(\d+|[ivxlcdm]+)\s*',text,re.I)
            # 无页码的目录也允许精确标题候选，未识别的条目仍保留为 unresolved。
            title=match[1].strip() if match else text.strip()
            page_label=match[2].casefold() if match else None
            candidates=[n for n in titles.get(title_key(title),[]) if positions[n['heading_anchor']]>positions[key]]
            destinations=labels.get(page_label,[])
            chosen=[n for n in candidates if n['page'] in destinations] if destinations else candidates
            status='linked' if len(chosen)==1 else 'ambiguous' if len(chosen)>1 else 'unresolved'
            links.append({'source':source,'title':title,'printed_page':page_label,
                          'target_anchor':chosen[0]['heading_anchor'] if status=='linked' else None,
                          'candidate_anchors':[n['heading_anchor'] for n in candidates],
                          'status':status,'basis':'exact_title_and_printed_page' if destinations else 'exact_title',
                          'semantics_verified':False})
    return links
