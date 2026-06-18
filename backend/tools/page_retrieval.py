"""当前网页 RAG 编排层。

该模块连接页面 ID、SQLite、embedding、Qdrant 和召回结果格式化：
- 普通发送优先复用 page.latest_snapshot_id。
- 刷新快照会把同一 page 的所有 chat 绑定切换到最新 snapshot。
- 召回阶段只读取当前 chat 绑定的 snapshot_id 列表。
"""

import os
from typing import Any

from common.page_identity import (
    build_chunk_id,
    build_page_identity,
    build_point_id,
)
from rag.chunker import chunk_text
from rag.cleaner import clean_page_text
from rag.embedder import embed_text, embed_texts
from rag import vector_store
from storage.db import db


CHUNKER_VERSION = os.getenv("CHUNKER_VERSION", "v1")
PAGE_CHUNK_SIZE = int(os.getenv("PAGE_CHUNK_SIZE", "800"))
PAGE_CHUNK_OVERLAP = int(os.getenv("PAGE_CHUNK_OVERLAP", "100"))


def _resolve_embedding_model() -> str:
    """读取当前索引和召回统一使用的 embedding 模型名。"""
    embedding_model = os.getenv("EMBEDDING_MODEL", "").strip()
    if not embedding_model:
        raise RuntimeError("EMBEDDING_MODEL is not configured")
    return embedding_model


def _get_page_value(current_page: Any, key: str, default: str = "") -> str:
    """兼容 dict 和 Pydantic 对象两种 current_page 输入。"""
    if isinstance(current_page, dict):
        value = current_page.get(key, default)
    else:
        value = getattr(current_page, key, default)
    return str(value or "").strip()


def _normalize_current_page(current_page: Any) -> dict[str, str]:
    """校验并清洗前端传入的当前页面快照。"""
    if current_page is None:
        raise ValueError("current_page is required")

    url = _get_page_value(current_page, "url")
    title = _get_page_value(current_page, "title")
    content = _get_page_value(current_page, "content")
    cleaned_text = clean_page_text(content)

    if not url:
        raise ValueError("current_page.url is required")
    if not cleaned_text:
        raise ValueError("current_page.content is required")

    return {
        "url": url,
        "title": title,
        "cleaned_text": cleaned_text,
    }


def _snapshot_is_indexed(snapshot_id: str, embedding_model: str) -> bool:
    """判断某个 snapshot 是否已经写入当前 Qdrant collection。"""
    try:
        return vector_store.snapshot_exists(
            snapshot_id=snapshot_id,
            embedding_model=embedding_model,
            chunker_version=CHUNKER_VERSION,
        )
    except Exception:
        # Qdrant 不可达时让调用方在真正索引/检索阶段拿到原始错误。
        raise


def _bind_existing_snapshot(
    chat_id: str,
    page_id: str,
    snapshot_id: str,
    page_context_id: str,
    reuse_reason: str,
) -> dict[str, Any]:
    """把已有 snapshot 绑定到当前 chat，并返回统一的索引统计。"""
    db.upsert_chat_page(
        chat_id=chat_id,
        page_id=page_id,
        snapshot_id=snapshot_id,
        page_context_id=page_context_id,
    )
    return {
        "page_id": page_id,
        "snapshot_id": snapshot_id,
        "indexed_from_cache": True,
        "reuse_reason": reuse_reason,
        "chunk_count": 0,
        "indexed_chunk_count": 0,
    }


def _cleanup_snapshot_vectors(snapshot_ids: list[str]) -> dict[str, Any]:
    """清理旧 snapshot 的向量数据，失败时只记录错误，不阻断新索引使用。"""
    normalized_snapshot_ids = [item for item in dict.fromkeys(snapshot_ids) if item]
    if not normalized_snapshot_ids:
        return {
            "replaced_snapshot_ids": [],
            "deleted_snapshot_ids": [],
            "vector_cleanup_error": "",
        }

    try:
        vector_store.delete_snapshots_data(normalized_snapshot_ids)
    except Exception as exc:
        return {
            "replaced_snapshot_ids": normalized_snapshot_ids,
            "deleted_snapshot_ids": [],
            "vector_cleanup_error": str(exc),
        }

    return {
        "replaced_snapshot_ids": normalized_snapshot_ids,
        "deleted_snapshot_ids": normalized_snapshot_ids,
        "vector_cleanup_error": "",
    }


def _replace_latest_snapshot_for_page(
    chat_id: str,
    page_context_id: str,
    page: dict[str, str],
    identity: dict[str, str],
    embedding_model: str,
    cleanup_old_vectors: bool,
) -> dict[str, Any]:
    """把新 snapshot 设置为 page 最新版本，并按需清理旧向量。"""
    old_snapshot_ids = db.replace_latest_snapshot_for_page(
        chat_id=chat_id,
        page_id=identity["page_id"],
        canonical_url=identity["canonical_url"],
        title=page["title"],
        latest_snapshot_id=identity["snapshot_id"],
        content_hash=identity["content_hash"],
        url=page["url"],
        chunker_version=CHUNKER_VERSION,
        embedding_model=embedding_model,
        page_context_id=page_context_id,
    )

    if cleanup_old_vectors:
        return _cleanup_snapshot_vectors(old_snapshot_ids)

    return {
        "replaced_snapshot_ids": old_snapshot_ids,
        "deleted_snapshot_ids": [],
        "vector_cleanup_error": "",
    }


def _build_chunk_points(
    chunks: list[dict],
    vectors: list[list[float]],
    identity: dict[str, str],
    page: dict[str, str],
    embedding_model: str,
) -> list[dict[str, Any]]:
    """把 chunk 和 embedding 组装成 Qdrant point。"""
    if len(chunks) != len(vectors):
        raise RuntimeError("chunk and embedding count mismatch")

    points: list[dict[str, Any]] = []
    for chunk, vector in zip(chunks, vectors):
        chunk_index = int(chunk["chunk_index"])
        # chunk_id 面向业务展示；point_id 面向 Qdrant 写入去重。
        chunk_id = build_chunk_id(identity["snapshot_id"], chunk_index)
        point_id = build_point_id(
            snapshot_id=identity["snapshot_id"],
            embedding_model=embedding_model,
            chunker_version=CHUNKER_VERSION,
            chunk_index=chunk_index,
        )

        points.append(
            {
                "id": point_id,
                "vector": vector,
                "payload": {
                    "source_type": "current_page",
                    "page_id": identity["page_id"],
                    "snapshot_id": identity["snapshot_id"],
                    "content_hash": identity["content_hash"],
                    "canonical_url": identity["canonical_url"],
                    "url": page["url"],
                    "title": page["title"],
                    "chunk_id": chunk_id,
                    "chunk_index": chunk_index,
                    "start": int(chunk["start"]),
                    "end": int(chunk["end"]),
                    "content": chunk["content"],
                    "embedding_model": embedding_model,
                    "chunker_version": CHUNKER_VERSION,
                },
            }
        )

    return points


def _index_new_snapshot(
    chat_id: str,
    page_context_id: str,
    page: dict[str, str],
    identity: dict[str, str],
    embedding_model: str,
    reuse_reason: str,
    cleanup_old_vectors: bool = False,
) -> dict[str, Any]:
    """为当前页面内容创建新 snapshot，并写入 Qdrant 与 SQLite。"""
    chunks = chunk_text(
        page["cleaned_text"],
        chunk_size=PAGE_CHUNK_SIZE,
        overlap=PAGE_CHUNK_OVERLAP,
    )
    if not chunks:
        raise ValueError("current_page.content does not produce any chunks")

    vectors = embed_texts([chunk["content"] for chunk in chunks], model=embedding_model)
    if not vectors:
        raise RuntimeError("embedding result is empty")

    vector_size = len(vectors[0])
    # collection 维度必须和 embedding 结果一致，否则 Qdrant 无法写入。
    vector_store.ensure_collection(vector_size=vector_size)
    points = _build_chunk_points(chunks, vectors, identity, page, embedding_model)
    vector_store.upsert_chunk_points(points)

    replace_stats = _replace_latest_snapshot_for_page(
        chat_id=chat_id,
        page_context_id=page_context_id,
        page=page,
        identity=identity,
        embedding_model=embedding_model,
        cleanup_old_vectors=cleanup_old_vectors,
    )

    return {
        "page_id": identity["page_id"],
        "snapshot_id": identity["snapshot_id"],
        "canonical_url": identity["canonical_url"],
        "content_hash": identity["content_hash"],
        "indexed_from_cache": False,
        "reuse_reason": reuse_reason,
        "chunk_count": len(chunks),
        "indexed_chunk_count": len(points),
        **replace_stats,
    }


def _reuse_indexed_snapshot_as_latest(
    chat_id: str,
    page_context_id: str,
    page: dict[str, str],
    identity: dict[str, str],
    embedding_model: str,
    reuse_reason: str,
    cleanup_old_vectors: bool = False,
) -> dict[str, Any]:
    """复用已经存在于 Qdrant 的 snapshot，并把它切换成 page 最新版本。"""
    replace_stats = _replace_latest_snapshot_for_page(
        chat_id=chat_id,
        page_context_id=page_context_id,
        page=page,
        identity=identity,
        embedding_model=embedding_model,
        cleanup_old_vectors=cleanup_old_vectors,
    )
    return {
        "page_id": identity["page_id"],
        "snapshot_id": identity["snapshot_id"],
        "canonical_url": identity["canonical_url"],
        "content_hash": identity["content_hash"],
        "indexed_from_cache": True,
        "reuse_reason": reuse_reason,
        "chunk_count": 0,
        "indexed_chunk_count": 0,
        **replace_stats,
    }


def index_or_reuse_page(
    chat_id: str,
    page_context_id: str,
    current_page: Any,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """索引当前页或复用已有快照，并绑定到当前 chat。

    普通发送：优先复用 page.latest_snapshot_id，保证旧 chat 再次启用时也跟随最新网页数据。
    强制刷新：跳过 latest 复用，按当前页面正文生成的新 snapshot 替换全局 latest。
    """
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_chat_id:
        raise ValueError("chat_id is required")

    embedding_model = _resolve_embedding_model()
    page = _normalize_current_page(current_page)
    identity = build_page_identity(page["url"], page["cleaned_text"])

    db.upsert_chat(normalized_chat_id)
    vector_store.ensure_collection(vector_size=vector_store.QDRANT_VECTOR_SIZE)

    if not force_refresh:
        # 普通发送不看旧 chat 绑定，先使用该 URL 的全局 latest snapshot。
        latest_snapshot = db.get_latest_snapshot(identity["page_id"])
        if latest_snapshot and _snapshot_is_indexed(latest_snapshot["snapshot_id"], embedding_model):
            result = _bind_existing_snapshot(
                chat_id=normalized_chat_id,
                page_id=identity["page_id"],
                snapshot_id=latest_snapshot["snapshot_id"],
                page_context_id=page_context_id,
                reuse_reason="latest_snapshot",
            )
            result.update(
                {
                    "canonical_url": identity["canonical_url"],
                    "content_hash": str(latest_snapshot.get("content_hash") or identity["content_hash"]),
                }
            )
            return result

    if force_refresh and _snapshot_is_indexed(identity["snapshot_id"], embedding_model):
        # 当前正文版本已经在向量库里，只需要切 DB 指针并清理旧版本。
        return _reuse_indexed_snapshot_as_latest(
            chat_id=normalized_chat_id,
            page_context_id=page_context_id,
            page=page,
            identity=identity,
            embedding_model=embedding_model,
            reuse_reason="force_refresh_existing_snapshot",
            cleanup_old_vectors=True,
        )

    if _snapshot_is_indexed(identity["snapshot_id"], embedding_model):
        # 非强制场景下，如果当前内容版本已经索引过，复用它但不清理旧版本。
        return _reuse_indexed_snapshot_as_latest(
            chat_id=normalized_chat_id,
            page_context_id=page_context_id,
            page=page,
            identity=identity,
            embedding_model=embedding_model,
            reuse_reason="existing_snapshot",
            cleanup_old_vectors=False,
        )

    return _index_new_snapshot(
        chat_id=normalized_chat_id,
        page_context_id=page_context_id,
        page=page,
        identity=identity,
        embedding_model=embedding_model,
        reuse_reason="force_refresh" if force_refresh else "new_snapshot",
        cleanup_old_vectors=force_refresh,
    )


def _hit_to_source(index: int, hit: dict[str, Any]) -> dict[str, Any]:
    """把 Qdrant 命中结果转换成前端引用卡片需要的 source 结构。"""
    payload = hit.get("payload") or {}
    return {
        "source_id": f"S{index}",
        "url": str(payload.get("url", "")),
        "title": str(payload.get("title", "")),
        "content": str(payload.get("content", "")),
        "score": float(hit.get("score", 0.0) or 0.0),
        "chunk_id": str(payload.get("chunk_id", "")),
        "snapshot_id": str(payload.get("snapshot_id", "")),
        "page_id": str(payload.get("page_id", "")),
    }


def retrieve_page_context(
    chat_id: str,
    query: str,
    top_k: int = 10,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按 chat 已绑定的页面快照召回相关 chunk。"""
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_chat_id:
        raise ValueError("chat_id is required")

    normalized_query = clean_page_text(query)
    if not normalized_query:
        return [], {
            "snapshot_count": 0,
            "retrieved_source_count": 0,
            "retrieved_chunk_ids": [],
        }

    # chat_pages 是召回范围的唯一来源，避免跨 chat 或跨页面误召回。
    snapshot_ids = db.list_chat_snapshot_ids(normalized_chat_id)
    if not snapshot_ids:
        return [], {
            "snapshot_count": 0,
            "retrieved_source_count": 0,
            "retrieved_chunk_ids": [],
        }

    embedding_model = _resolve_embedding_model()
    query_vector = embed_text(normalized_query, model=embedding_model)
    vector_store.ensure_collection(vector_size=len(query_vector))
    hits = vector_store.search_chunks(
        query_vector=query_vector,
        snapshot_ids=snapshot_ids,
        embedding_model=embedding_model,
        chunker_version=CHUNKER_VERSION,
        top_k=top_k,
    )

    sources = [_hit_to_source(index, hit) for index, hit in enumerate(hits, start=1)]
    return sources, {
        "snapshot_count": len(snapshot_ids),
        "retrieved_source_count": len(sources),
        "retrieved_chunk_ids": [source["chunk_id"] for source in sources if source["chunk_id"]],
    }


if __name__ == "__main__":
    db.init_db()
    demo_page = {
        "url": "https://example.com/rag-demo",
        "title": "RAG Demo",
        "content": "RAG 是检索增强生成。它会先召回相关片段，再交给模型生成答案。",
    }
    index_result = index_or_reuse_page(
        chat_id="chat_manual_page_retrieval",
        page_context_id="pagectx_manual",
        current_page=demo_page,
    )
    print(index_result)
    retrieved_sources, retrieval_stats = retrieve_page_context(
        chat_id="chat_manual_page_retrieval",
        query="什么是 RAG？",
        top_k=3,
    )
    print(retrieval_stats)
    print(retrieved_sources)
