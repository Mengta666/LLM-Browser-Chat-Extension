"""页面身份 ID 生成规则的轻量回归测试。"""

import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from common.page_identity import build_page_identity, canonicalize_url


def test_canonicalize_url_normalizes_obvious_url_noise() -> None:
    """验证 URL 规范化会清理大小写、默认端口、fragment 和尾斜杠。"""
    assert canonicalize_url("HTTPS://Example.COM:443/articles/rag/#intro") == "https://example.com/articles/rag"
    assert canonicalize_url("https://example.com/articles/rag/") == "https://example.com/articles/rag"
    assert canonicalize_url("http://example.com:80/articles/rag") == "http://example.com/articles/rag"


def test_canonicalize_url_drops_common_tracking_query_params() -> None:
    """验证常见追踪 query 参数会被移除。"""
    assert (
        canonicalize_url("https://example.com/articles/rag?utm_source=newsletter&fbclid=abc&id=42")
        == "https://example.com/articles/rag?id=42"
    )


def test_canonicalize_url_keeps_unknown_query_params() -> None:
    """验证未知 query 参数会保留，避免误合并不同内容页。"""
    first = canonicalize_url("https://example.com/search?q=rag&page=1")
    second = canonicalize_url("https://example.com/search?q=rag&page=2")
    assert first == "https://example.com/search?q=rag&page=1"
    assert second == "https://example.com/search?q=rag&page=2"
    assert first != second


def test_build_page_identity_stabilizes_page_id_across_url_noise() -> None:
    """验证 URL 噪音不影响同一内容页的 page_id 和 snapshot_id。"""
    first = build_page_identity(
        "https://example.com/articles/rag/?utm_campaign=test#top",
        "same cleaned content",
    )
    second = build_page_identity(
        "https://EXAMPLE.com/articles/rag?gclid=123",
        "same cleaned content",
    )

    assert first["canonical_url"] == "https://example.com/articles/rag"
    assert second["canonical_url"] == "https://example.com/articles/rag"
    assert first["page_id"] == second["page_id"]
    assert first["snapshot_id"] == second["snapshot_id"]


def test_content_change_keeps_page_id_but_changes_snapshot_id() -> None:
    """验证同一逻辑页面内容变化时 page_id 不变而 snapshot_id 改变。"""
    old = build_page_identity("https://example.com/articles/rag#old", "old content")
    new = build_page_identity("https://example.com/articles/rag#new", "new content")

    assert old["page_id"] == new["page_id"]
    assert old["content_hash"] != new["content_hash"]
    assert old["snapshot_id"] != new["snapshot_id"]


if __name__ == "__main__":
    test_canonicalize_url_normalizes_obvious_url_noise()
    test_canonicalize_url_drops_common_tracking_query_params()
    test_canonicalize_url_keeps_unknown_query_params()
    test_build_page_identity_stabilizes_page_id_across_url_noise()
    test_content_change_keeps_page_id_but_changes_snapshot_id()
    print("PASS page identity")
