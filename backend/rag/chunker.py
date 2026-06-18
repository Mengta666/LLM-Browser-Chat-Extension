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
