"""文本原件解码与来源坐标；读取时不规范化换行。"""

import codecs
import hashlib
import os
import re
from pathlib import Path


def decode(data):
    if data.startswith(codecs.BOM_UTF8):
        encoding = 'utf-8-sig'
    elif data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = 'utf-32'
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = 'utf-16'
    else:
        encoding = 'utf-8'
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        encoding = os.getenv('KB_TEXT_FALLBACK_ENCODING', '').strip()
        if not encoding:
            raise ValueError('文本不是有效 UTF-8；请转为 UTF-8，或明确配置 KB_TEXT_FALLBACK_ENCODING') from None
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            raise ValueError('文本无法使用指定编码解码') from None
    if not text.strip():
        raise ValueError('文件内容为空')
    if '\x00' in text:
        raise ValueError('文本包含空字符，可能使用了错误编码或不是文本文件')
    if len(text) > int(os.getenv('KB_TEXT_MAX_CHARS', '10000000')):
        raise ValueError('文本超过 KB_TEXT_MAX_CHARS 上限')
    return text, encoding


def line_starts(text):
    return [0, *[m.end() for m in re.finditer(r'\r\n|\r|\n', text)]]


def original(doc):
    from rag.document_config import document_path
    path = document_path(doc['doc_id'], 'original.' + doc['file_type'])
    if not doc['original_path'] or Path(doc['original_path']).resolve() != path or not path.is_file():
        raise ValueError('完整原文件未保留，请重新上传；不会用检索片段拼接原文')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != doc['sha256']:
        raise ValueError('original_file_changed')
    return decode(data)
