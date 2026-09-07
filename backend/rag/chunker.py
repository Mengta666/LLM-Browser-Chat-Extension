"""文本切块工具，把长文本切成可检索的重叠片段。"""

from .cleaner import *


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[dict]:
    """按固定窗口和重叠长度切分文本，并保留原始位置。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and < chunk_size")

    text = clean_page_text(text)
    if not text:
        return []

    length = len(text)
    if length <= chunk_size:
        return [
            {
                "chunk_index": 0,
                "content": text,
                "start": 0,
                "end": length,
            }
        ]

    result: list[dict] = []
    chunk_index: int = 0
    for chunk_point in range(0, length, chunk_size - overlap):
        result.append(
            {
                "chunk_index": chunk_index,
                "content": text[chunk_point : chunk_point + chunk_size],
                "start": chunk_point,
                "end": chunk_point + chunk_size if chunk_point + chunk_size < length else length,
            }
        )
        chunk_index += 1
    return result


def chunk_document(text: str, chunk_size: int = 512, chunk_overlap: int = 64) -> list[dict]:
    """递归语义切片(KB 文档专用),返回 [{text, start, end}, ...],保留原文位置。

    chunk_size / chunk_overlap 单位是 tokens(tiktoken cl100k_base)。
    分隔符按优先级尝试:段落 → 句子(中文标点优先)→ 逗号 → 空格 → 字符硬切。
    中文分隔符显式传入,否则遇中文无空格会掉到字符级硬切、断句中间。
    """
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError as exc:
        raise ImportError(
            "langchain-text-splitters 未安装,无法切片。pip install langchain-text-splitters>=0.3.0"
        ) from exc

    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        model_name="gpt-4",                  # tiktoken cl100k_base
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=[
            "\n\n", "\n",                    # 段落 / 换行
            "。", "!", "?",                   # 中文句号 / 叹号 / 问号
            "!", "?", ".",                   # 英文
            "；", ";",                        # 分号
            "，", ",",                        # 逗号
            " ",                             # 空格
            "",                              # 字符级硬切(最后兜底)
        ],
        is_separator_regex=False,
    )

    # create_documents 返回 [Document(page_content=..., metadata={}), ...]
    docs = splitter.create_documents([text])

    chunks = []
    offset = 0
    for doc in docs:
        chunk_text = doc.page_content
        # 简化:假设 chunk 按原文顺序出现,用 str.find 逐个定位
        start = text.find(chunk_text, offset)
        if start == -1:
            # overlap 可能导致重复,从头再找一次
            start = text.find(chunk_text)
        end = start + len(chunk_text) if start != -1 else offset
        chunks.append({
            "text": chunk_text,
            "start": start if start != -1 else offset,
            "end": end,
        })
        offset = end

    return chunks

