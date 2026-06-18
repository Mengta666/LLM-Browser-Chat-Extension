"""基于 embedding 余弦相似度的基础召回实现。"""

from copy import deepcopy

import numpy as np

from .embedder import embed_text, embed_texts


def cosine_similarity(vec1: list, vec2: list) -> float:
    """计算两个向量之间的余弦相似度。"""
    vec1 = np.array(vec1)
    vec2 = np.array(vec2)
    dot_product = np.dot(vec1, vec2)
    model_vec1 = np.linalg.norm(vec1)
    model_vec2 = np.linalg.norm(vec2)
    return dot_product / (model_vec1 * model_vec2) if model_vec2 != 0 and model_vec1 != 0 else 0.0


def retrieve_chunks(query, chunks, top_k=3, model=None) -> list[dict]:
    """对候选 chunk 打分并返回相似度最高的前 k 条。"""
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    query = query.strip()
    if not query:
        return []

    if not isinstance(chunks, list):
        raise ValueError("chunks must be a list")
    if not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k must be > 0")

    valid_chunks = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        content = chunk.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(chunk.get("chunk_index"), int):
            continue
        if not isinstance(chunk.get("start"), int):
            continue
        if not isinstance(chunk.get("end"), int):
            continue
        valid_chunks.append(chunk)

    if not valid_chunks:
        return []

    # 查询和候选分开向量化，便于后续替换模型或增加缓存。
    query = embed_text(query, model=model)
    vectors = embed_texts([chunk["content"] for chunk in valid_chunks], model=model)

    similarities = [cosine_similarity(query, vector) for vector in vectors]
    chunks_clone = deepcopy(valid_chunks)
    for index, chunk in enumerate(chunks_clone):
        chunk["score"] = similarities[index]

    # 分数优先，chunk_index 作为稳定的次排序键。
    chunks_clone = sorted(
        chunks_clone,
        key=lambda x: (-float(x.get("score", -1.0)), x.get("chunk_index", 10 ** 9)),
    )
    if top_k >= len(chunks_clone):
        return chunks_clone
    return chunks_clone[:top_k]
