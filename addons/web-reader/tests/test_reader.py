import gzip
import socket
import time
from types import SimpleNamespace

import pytest

from app import reader


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


@pytest.mark.parametrize("url,code", [
    ("file:///etc/passwd", "blocked_url"), ("ftp://example.com/a", "blocked_url"),
    ("https://user:pass@example.com/", "blocked_url"), ("https://example.com:8443/", "blocked_port"),
    ("https://example.com/\r\nX:yes", "invalid_url"), ("https://example.com\\@127.0.0.1", "invalid_url"),
    ("https://[fe80::1%25eth0]/", "blocked_url"), ("https://example.com:bad/", "invalid_url"),
])
def test_reject_invalid_targets(url, code):
    """验证无效或不允许的目标地址会被拒绝。"""
    with pytest.raises(reader.ReadError, match=code):
        reader.resolve_target(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.8.125.20", "169.254.169.254", "192.168.1.1", "172.16.0.1",
                               "0.0.0.0", "100.64.0.1", "224.0.0.1", "::1", "fc00::1", "fe80::1",
                               "::ffff:127.0.0.1", "2002:7f00:1::", "64:ff9b::7f00:1"])
def test_dns_targets_must_be_public(monkeypatch, ip):
    """验证 DNS 解析结果必须是允许访问的公网地址。"""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET6 if ':' in ip else socket.AF_INET,
        socket.SOCK_STREAM, 6, "", (ip, 443))])
    with pytest.raises(reader.ReadError, match="blocked_address"):
        reader.resolve_target("https://example.org/")


def test_mixed_dns_answer_is_blocked(monkeypatch):
    """验证 DNS 同时返回公网与非公网地址时拒绝整个目标。"""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC + [(2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(reader.ReadError, match="blocked_address"):
        reader.resolve_target("https://example.org/")


def test_unicode_url_and_fragment(monkeypatch):
    """验证含 Unicode 字符的 URL 会被规范化并移除片段。"""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC)
    url, host, port, addresses = reader.resolve_target("https://EXAMPLE.org/说明?q=中文#title")
    assert host == "example.org" and port == 443
    assert "#" not in url and "%" in url and len(addresses) == 1


def test_encoded_url_length_is_bounded(monkeypatch):
    """验证 URL 编码后的长度仍受上限约束。"""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: PUBLIC)
    with pytest.raises(reader.ReadError, match="invalid_url"):
        reader.resolve_target("https://example.org/" + "中" * 1000)


def test_connect_pins_address_but_preserves_tls_hostname(monkeypatch):
    """验证连接使用已校验 IP，同时保留原始主机名进行 TLS 校验。"""
    calls = []
    sock = SimpleNamespace(settimeout=lambda x: None, connect=lambda addr: calls.append(addr), close=lambda: None)
    monkeypatch.setattr(socket, "socket", lambda *a: sock)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: pytest.fail("Second DNS lookup"))
    context = SimpleNamespace(set_alpn_protocols=lambda x: None,
        wrap_socket=lambda value, server_hostname: calls.append(server_hostname) or value)
    monkeypatch.setattr(reader.ssl, "create_default_context", lambda: context)
    conn = reader.PinnedConnection("example.org", 443, [(2, 1, 6, ("93.184.216.34", 443))], True, time.monotonic() + 5)
    conn.connect()
    assert calls == [("93.184.216.34", 443), "example.org"]


class Response:
    def __init__(self, body=b"<html>content</html>", status=200, headers=None):
        """初始化用于下载测试的响应状态、响应头和正文游标。"""
        self.body, self.status = body, status
        self.headers = {"Content-Type": "text/html", **(headers or {})}
    def getheader(self, name):
        """读取模拟响应中的指定响应头。"""
        return self.headers.get(name)
    def read1(self, size):
        """按指定大小读取模拟响应正文并推进读取位置。"""
        part, self.body = self.body[:size], self.body[size:]
        return part


def install_transport(monkeypatch, responses):
    """安装可记录请求的模拟连接，避免下载测试访问真实网络。"""
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: PUBLIC if host != "127.0.0.1" else [(2, 1, 6, "", (host, 80))])
    class Connection:
        sock = None
        def __init__(self, *args):
            """记录模拟连接的初始化参数。"""
            calls.append(args)
        def request(self, *args, **kwargs):
            """记录模拟连接收到的 HTTP 请求。"""
            calls.append(kwargs)
        def getresponse(self):
            """返回预设的模拟 HTTP 响应。"""
            return responses.pop(0)
        def close(self):
            """提供模拟连接的关闭接口。"""
            pass
    monkeypatch.setattr(reader, "PinnedConnection", Connection)
    return calls


def test_redirect_is_revalidated_before_connect(monkeypatch):
    """验证每次重定向都在建立新连接前重新校验目标。"""
    calls = install_transport(monkeypatch, [Response(status=302, headers={"Location": "http://127.0.0.1/secret"})])
    with pytest.raises(reader.ReadError, match="blocked_address"):
        reader.download("https://example.org", time.monotonic() + 3, 10000)
    assert len(calls) == 2


@pytest.mark.parametrize("response,code", [
    (Response(status=403), "http_403"),
    (Response(headers={"Content-Type": "application/pdf"}), "unsupported_content_type"),
    (Response(headers={"Content-Encoding": "br"}), "unsupported_encoding"),
    (Response(headers={"Content-Length": "100000"}), "page_too_large"),
    (Response(body=b"a" * 10001), "page_too_large"),
    (Response(body=gzip.compress(b"a" * 10001), headers={"Content-Encoding": "gzip"}), "page_too_large"),
])
def test_download_errors(monkeypatch, response, code):
    """验证下载失败场景转换为预期的受控错误。"""
    install_transport(monkeypatch, [response])
    with pytest.raises(reader.ReadError, match=code):
        reader.download("https://example.org/", time.monotonic() + 3, 10000)


def test_download_gzip_and_no_credential_forwarding(monkeypatch):
    """验证 gzip 解压及下载请求不会携带不应转发的凭证。"""
    content = b"<html><body>article</body></html>"
    calls = install_transport(monkeypatch, [Response(body=gzip.compress(content), headers={"Content-Encoding": "gzip"})])
    body, final = reader.download("https://example.org/", time.monotonic() + 3, 10000)
    assert body == content and final == "https://example.org/"
    assert "Authorization" not in calls[1]["headers"] and "Cookie" not in calls[1]["headers"]


@pytest.mark.parametrize("language", ["en", "zh"])
def test_actual_extractor_keeps_article_and_table(language):
    """验证实际提取器在不同语言下保留文章正文和表格。"""
    paragraph = "Calibration requires isolation of power before replacing the sensor. " if language == 'en' else "这是软件测试文档，校准前必须断电，完成更换后记录测试结果。"
    html = f'<html><head><title>Sensor guide</title><meta property="article:published_time" content="2025-04-03"></head><body><article><h1>Sensor guide</h1><p>{paragraph * 15}</p><table><tr><th>Part</th><th>Interval</th></tr><tr><td>SYNTH-73</td><td>840 hours</td></tr></table></article></body></html>'
    result = reader.extract_html(html.encode(), "https://example.org/guide", 10000)
    assert paragraph.strip() in result['content']
    assert 'SYNTH-73' in result['content'] and '840' in result['content']
    assert result['extracted_date'] == '2025-04-03'
    assert not result['truncated']


def test_empty_and_truncated_extraction():
    """验证空正文和达到长度上限的正文具有正确输出。"""
    with pytest.raises(reader.ReadError, match="empty_content"):
        reader.extract_html(b"<html><script>render()</script></html>", "https://example.org", 1000)
    result = reader.extract_html(('<html><body><article><p>' + 'Synthetic test text. ' * 100 + '</p></article></body></html>').encode(), "https://example.org", 100)
    assert result['truncated'] and len(result['content']) <= 100 and result['content_length'] > 100
    assert result['content'].endswith('Synthetic test text.')


def test_metadata_length_is_bounded(monkeypatch):
    """验证提取结果中的元数据长度受限。"""
    from lxml import etree
    monkeypatch.setattr(reader.trafilatura, 'bare_extraction', lambda *a, **kw:
        SimpleNamespace(body=etree.fromstring('<body><p>Synthetic article</p></body>'), title='T' * 10000, date='D' * 1000))
    result = reader.extract_html(b'<html></html>', 'https://example.org', 1000)
    assert len(result['title']) == 500 and len(result['extracted_date']) == 64


def test_extraction_does_not_resolve_external_entities(tmp_path):
    """验证正文提取不会解析外部实体。"""
    target = tmp_path / 'synthetic-entity.txt'
    target.write_text('SYNTHETIC_EXTERNAL_ENTITY_MARKER', encoding='utf-8')
    html = (f'<!DOCTYPE html [<!ENTITY external SYSTEM "{target.as_uri()}">]>'
            '<html><body><article><p>&external;</p><p>' + 'Synthetic public article. ' * 100 +
            '</p></article></body></html>').encode()
    result = reader.extract_html(html, 'https://example.org', 10000)
    assert 'SYNTHETIC_EXTERNAL_ENTITY_MARKER' not in result['content']
