"""页面身份标识生成模块。

本模块只负责生成稳定 ID，不访问数据库或向量库：
- page_id 表示规范化 URL 对应的逻辑页面。
- content_hash 表示清洗后正文的内容版本。
- snapshot_id 表示某个页面在某个正文版本下的快照。
- chunk_id / point_id 用于 chunk 展示和 Qdrant 写入去重。
"""

import os
import uuid

from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from dotenv import load_dotenv

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)


TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "gbraid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "yclid",
}
TRACKING_QUERY_PREFIXES = ("utm_",)


def _is_tracking_query_key(key: str) -> bool:
    """判断 query 参数是否属于常见追踪参数。"""
    normalized_key = key.strip().lower()
    return normalized_key in TRACKING_QUERY_KEYS or normalized_key.startswith(TRACKING_QUERY_PREFIXES)


def canonicalize_url(url) -> str:
    """返回用于生成 page_id 的规范化 URL。

    只做保守规范化：协议/域名小写、去掉 fragment、去掉默认端口、清理尾斜杠、
    移除常见追踪 query 参数。未知 query 参数会保留，避免把不同内容页面错误合并。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must not be empty")

    raw_url = url.strip()
    parts = urlsplit(raw_url)
    if not parts.scheme or not parts.netloc:
        return raw_url

    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return raw_url

    try:
        port = parts.port
    except ValueError:
        return raw_url
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None

    netloc = hostname
    if port is not None:
        netloc = f"{netloc}:{port}"

    path = quote(parts.path or "/", safe="/%:@")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_query_key(key)
    ]
    query = urlencode(query_pairs, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def build_page_id(canonical_url) -> str:
    """根据规范化 URL 生成逻辑页面 ID。"""
    raw = f"page:{os.getenv('PAGE_ID_VERSION')}\n{canonical_url}"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
    page_id = f"page_{digest}"
    return page_id


def build_content_hash(cleaned_text) -> str:
    """根据清洗后的正文生成内容 hash。

    入参必须是已经过 clean_page_text 处理的文本，避免同一正文因空白差异产生
    过多无意义版本。
    """
    cleaned_text = cleaned_text.strip()
    if len(cleaned_text) == 0:
        raise ValueError("build_content_hash error: empty text")
    raw = f"content:{os.getenv('CONTENT_HASH_VERSION')}\n{cleaned_text}"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
    content_hash = f"content_{digest}"
    return content_hash


def build_snapshot_id(page_id, content_hash) -> str:
    """生成内容快照 ID，代表“某个页面的某个正文版本”。"""
    raw = f"snapshot:{os.getenv('SNAPSHOT_ID_VERSION')}\n{page_id}\n{content_hash}"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
    snapshot_id = f"snap_{digest}"
    return snapshot_id


def build_chunk_id(snapshot_id, chunk_index) -> str:
    """生成业务层 chunk ID，用于 sources、trace 和 Qdrant payload。"""
    chunk_id = f"chunk_{snapshot_id}_{chunk_index:06d}"
    return chunk_id


def build_point_id(snapshot_id, embedding_model, chunker_version, chunk_index) -> str:
    """生成稳定的 Qdrant point id。

    point_id 必须同时包含 snapshot、embedding 模型、chunker 版本和 chunk 下标。
    这样重复 upsert 同一个 chunk 时会覆盖旧点，不会产生重复向量。
    """
    raw = (
        f"point:{os.getenv('POINT_ID_VERSION')}\n"
        f"{snapshot_id}\n"
        f"{embedding_model}\n"
        f"{chunker_version}\n"
        f"{chunk_index}"
    )
    # UUID5 要求命名空间本身也是 UUID；这里先把环境变量转成固定命名空间。
    namespace_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, os.getenv('POINT_NAMESPACE', 'default_ns'))
    point_id = str(uuid.uuid5(namespace_uuid, raw))
    return point_id


def build_page_identity(url, cleaned_text) -> dict:
    """一次性生成页面索引链路需要的所有身份字段。"""
    canonical_url = canonicalize_url(url)
    page_id = build_page_id(canonical_url)
    content_hash = build_content_hash(cleaned_text)
    snapshot_id = build_snapshot_id(page_id, content_hash)

    return {
        "canonical_url": canonical_url,
        "page_id": page_id,
        "content_hash": content_hash,
        "snapshot_id": snapshot_id,
    }
