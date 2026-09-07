"""KB 文档解析:PDF / Markdown / 纯文本 → 正文字符串。

策略:
- PDF:先检测是否为扫描件(单页平均字符数 < threshold),是则拒绝;否则用
  pymupdf4llm 直出 GitHub-flavored Markdown。
- MD / TXT:直接 UTF-8 读取。
- 其他扩展名:拒绝。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def is_scanned_pdf(path: str | Path, threshold: int = 10) -> bool:
    """单页平均字符数 < threshold 判定为扫描件(无文本层)。"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        if len(doc) == 0:
            return True
        total = sum(len(page.get_text("text").strip()) for page in doc)
        doc.close()
        return (total / len(doc)) < threshold
    except Exception:
        return False


def parse(file_path: str | Path, file_type: str) -> str:
    """解析文档,返回纯文本(Markdown 格式)。

    file_type: "pdf" / "md" / "txt"
    失败时抛 ValueError(含原因,供上层记录到 kb_docs.error_msg)。
    """
    path = Path(file_path)
    ft = file_type.lower().lstrip(".")

    if ft == "pdf":
        return _parse_pdf(path)
    elif ft in ("md", "markdown"):
        return _read_text(path)
    elif ft == "txt":
        return _read_text(path)
    else:
        raise ValueError(f"不支持的文件类型: {file_type}")


def _parse_pdf(path: Path) -> str:
    try:
        import pymupdf4llm
    except ImportError as exc:
        raise ValueError("pymupdf4llm 未安装,无法解析 PDF") from exc

    from agent.memory.config import KB_SCANNED_PDF_CHAR_THRESHOLD
    if is_scanned_pdf(path, KB_SCANNED_PDF_CHAR_THRESHOLD):
        raise ValueError("检测到扫描件 PDF(无文本层),暂不支持。请先用 OCR 工具处理后再上传。")

    try:
        text = pymupdf4llm.to_markdown(str(path))
    except Exception as exc:
        raise ValueError(f"PDF 解析失败: {exc}") from exc

    if not text or not text.strip():
        raise ValueError("PDF 解析结果为空,可能是扫描件或加密文档。")

    return text


def _read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        raise ValueError(f"文件读取失败: {exc}") from exc

    if not text or not text.strip():
        raise ValueError("文件内容为空。")

    return text
