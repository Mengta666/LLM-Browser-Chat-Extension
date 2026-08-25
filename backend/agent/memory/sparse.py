"""中文稀疏向量编码(BM25 用),对齐 FastEmbed 的 hash 方案。

FastEmbed 的 SimpleTokenizer 按空白切分,中文会整句塌成一个 token 且无中文
stemmer —— 中文场景 BM25 开箱即废。故这里用 jieba 分词,term id 用
abs(mmh3.hash(term))(与 FastEmbed compute_token_id 同源,可与其生态互通)。
只产出 term frequency;IDF 权重由 Qdrant 服务端 Modifier.IDF 计算。

doc 端与 query 端必须用同一 sparse_encode(query value 同为 tf)。
jieba/mmh3 任一不可用时降级为空稀疏向量,检索自动退化为纯 dense,不崩。
"""

from __future__ import annotations

import re

from qdrant_client import models

try:
    import jieba
    import mmh3
    _SPARSE_AVAILABLE = True
    # 模块级预热 jieba 词典(避免首次分词的 IO 抖动)
    jieba.initialize()
except Exception:  # 依赖缺失/加载失败 → 降级
    _SPARSE_AVAILABLE = False

# 中文常见虚词停用(轻量内置表;不追求完备,只去最高频噪声)
_STOP_WORDS = {
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "也", "都", "要",
    "这", "那", "个", "上", "下", "你", "他", "她", "它", "们", "把", "被",
    "着", "过", "与", "及", "或", "等", "吧", "呢", "啊", "吗", "让", "给",
}

_CLEAN_RE = re.compile(r"[^\w一-鿿]+")


def sparse_available() -> bool:
    """稀疏编码是否可用(jieba+mmh3 就绪)。"""
    return _SPARSE_AVAILABLE


def sparse_encode(text: str) -> dict[int, float]:
    """jieba 分词 → {term_id: tf}。term_id = abs(mmh3.hash(term))。

    不用 stemmer、只 unigram、先剥标点、去停用词。空文本/不可用时返回 {}。
    """
    if not _SPARSE_AVAILABLE:
        return {}
    cleaned = _CLEAN_RE.sub(" ", str(text or ""))
    if not cleaned.strip():
        return {}
    tf: dict[int, float] = {}
    for raw in jieba.cut(cleaned):
        token = raw.strip().lower()
        if not token or token in _STOP_WORDS:
            continue
        term_id = abs(mmh3.hash(token))
        tf[term_id] = tf.get(term_id, 0.0) + 1.0
    return tf


def to_sparse_vector(text: str) -> models.SparseVector:
    """把文本编码成 Qdrant SparseVector;空/不可用时 indices=values=[](纯 dense 降级)。"""
    tf = sparse_encode(text)
    return models.SparseVector(indices=list(tf.keys()), values=list(tf.values()))
