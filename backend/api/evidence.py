"""本轮检索来源登记与稳定引用编号，不做语义复核。"""

import hashlib
import json
from search.urls import normalize_web_url
from search.excerpts import history_excerpt


def kb_evidence_rows(payload):
    documents = {d['ref']: d for d in payload.get('documents', [])}
    for row in payload.get('evidence', []):
        document = documents.get(row.get('doc_ref'), {})
        yield {**{k: v for k, v in document.items() if k != 'ref'}, **row,
               **({'warnings': list(dict.fromkeys([*document.get('warnings', []), *row.get('warnings', [])]))}
                  if document.get('warnings') or row.get('warnings') else {})}


class EvidenceLedger:
    def __init__(self, start_index=1, initial_sources=None):
        self.next_index = start_index
        self.sources = {s["index"]: dict(s) for s in (initial_sources or [])}
        self.keys = {}
        for index, source in self.sources.items():
            if source.get('kb_id') and source.get('ledger_key'):
                self.keys[source['ledger_key']] = index
            elif not source.get('kb_id'):
                for url in (source.get('url'), source.get('original_url')):
                    identity = normalize_web_url(url) if url else ''
                    if identity:
                        self.keys[('web', identity)] = index

    def register(self, tool_name, kb_id, results, query):
        numbered, sources = [], {}
        for result in results:
            row = result if isinstance(result, dict) else vars(result)
            content = str(row.get("content", "") if row.get("context_status") else (row.get("content") or row.get("snippet") or ""))
            digest = hashlib.sha256(content.encode()).hexdigest()[:20]
            is_kb = tool_name in ("kb_search", "kb_read_document")
            original = row.get('url', '')
            final = row.get('final_url') or original
            identity = normalize_web_url(final)
            key = json.dumps([kb_id, row.get("doc_id"), row.get("parse_id"), row.get("index_run_id"),
                              row.get("chunk_idx", row.get("chunk_id")), row.get("locations", []),
                              row.get("window_chunk_ids", []), digest], sort_keys=True) if is_kb else ('web', identity or final)
            if is_kb and row.get('parse_id') and row.get('locations'):
                locations = sorted((p['block_id'], p['start'], p['end']) for p in row['locations'])
                key = json.dumps([kb_id, row.get('doc_id'), row['parse_id'], locations, digest], sort_keys=True)
            # 缺少文档 ID 的旧数据不能仅按同名文件合并。
            if is_kb and not row.get("doc_id"):
                key += str(self.next_index)
            index = self.keys.get(key)
            if index is None and not is_kb:
                index = self.keys.get(('web', normalize_web_url(original)))
            if index is None:
                index = self.next_index
                self.next_index += 1
                self.keys[key] = index
                title = row.get("source", "未知文档") if is_kb else row.get("title", "")
                self.sources[index] = {
                    "index": index, "title": title,
                    "url": f"kb://{kb_id}/{row.get('doc_id') or title}" if is_kb else row.get("url", ""),
                    "snippet": content[:200], "content": content, "questions": [],
                    **({"kb_id": kb_id, "doc_id": row.get("doc_id", ""),
                        "index_run_id": row.get("index_run_id", ""), "chunk_id": row.get("chunk_idx", row.get("chunk_id")),
                        "window_chunk_ids": row.get("window_chunk_ids", []), "evidence_id": digest,
                        "parse_id": row.get('parse_id', ''), "locations": row.get('locations', []),
                        "structure_id":row.get('structure_id',''),"node_id":row.get('node_id',''),
                        "node_path":row.get('node_path',[]),"bundle_id":row.get('bundle_id',''),
                        "read_kind": row.get('read_kind', 'preview'), "warnings": row.get('warnings', []),
                        "ledger_key": key} if is_kb else {}),
                }
            source = self.sources[index]
            if not is_kb:
                self.keys[key] = index
                self.keys[('web', normalize_web_url(original) or original)] = index
                if row.get('content_source') == 'page' or source.get('content_source') != 'page':
                    source.update(url=final, original_url=original, title=row.get('title', ''),
                                  content=content, snippet=content[:200], excerpt=history_excerpt(content),
                                  content_source=row.get('content_source', 'snippet'),
                                  read_status=row.get('read_status', 'not_requested'),
                                  read_error=row.get('read_error', ''),
                                  truncated=row.get('truncated', False),
                                  extracted_date=row.get('extracted_date'), fetched_at=row.get('fetched_at'))
                    for field in ('context_status', 'context_tokens', 'selected_blocks', 'structure_mode', 'content_length'):
                        if field in row:
                            source[field] = row[field]
            question = row.get("question") or query
            if question not in source["questions"]:
                source["questions"].append(question)
            numbered.append({**row, "citation_index": index})
            sources[index] = {k: v for k, v in source.items() if k != "content"}
        return numbered, list(sources.values())
