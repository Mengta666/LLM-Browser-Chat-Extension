"""本轮检索来源登记与稳定引用编号，不做语义复核。"""

import hashlib
from search.urls import normalize_web_url
from search.excerpts import history_excerpt


class EvidenceLedger:
    def __init__(self, start_index=1, initial_sources=None):
        self.next_index = start_index
        self.sources = {s["index"]: dict(s) for s in (initial_sources or [])}
        self.keys = {}
        for index, source in self.sources.items():
            if not source.get('kb_id'):
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
            is_kb = tool_name == "kb_search"
            original = row.get('url', '')
            final = row.get('final_url') or original
            identity = normalize_web_url(final)
            key = (kb_id, row.get("doc_id"), row.get("index_run_id"), row.get("chunk_idx", row.get("chunk_id")),
                   tuple(row.get("window_chunk_ids", [])), digest) if is_kb else ('web', identity or final)
            # 缺少文档 ID 的旧数据不能仅按同名文件合并。
            if is_kb and not row.get("doc_id"):
                key += (self.next_index,)
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
                        "window_chunk_ids": row.get("window_chunk_ids", []), "evidence_id": digest} if is_kb else {}),
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
