"""限定公网目标、固定连接地址的 HTML 下载与正文提取。"""

import http.client
import ipaddress
import re
import socket
import ssl
import time
import zlib
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import trafilatura


class ReadError(Exception):
    pass


def resolve_target(url):
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ReadError("invalid_url")
    if re.search(r"[\x00-\x20\x7f\\]", url):
        raise ReadError("invalid_url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ReadError("blocked_url")
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if "%" in host:
            raise ReadError("blocked_url")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port != (443 if parsed.scheme == "https" else 80):
            raise ReadError("blocked_port")
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (ValueError, UnicodeError):
        raise ReadError("invalid_url") from None
    except socket.gaierror:
        raise ReadError("dns_error") from None
    approved = []
    for family, socktype, proto, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        effective = getattr(ip, "ipv4_mapped", None) or ip
        if not effective.is_global or effective.is_multicast or effective.is_unspecified:
            raise ReadError("blocked_address")
        # IPv6 过渡地址可能把公网外观映射到另一目标，首版不支持这些路由。
        if ip.version == 6 and (ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96")):
            raise ReadError("blocked_address")
        entry = (family, socktype, proto, address)
        if entry not in approved:
            approved.append(entry)
    if not approved:
        raise ReadError("dns_error")
    authority = f"[{host}]" if ":" in host else host
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="%/?@:!$&'()*+,;=-._~")
    canonical = urlunsplit((parsed.scheme, authority, path, query, ""))
    if len(canonical) > 4096:
        raise ReadError("invalid_url")
    return canonical, host, port, approved


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, addresses, secure, deadline):
        super().__init__(host, port, timeout=max(.1, deadline - time.monotonic()))
        self.addresses = addresses
        self.secure = secure
        self.deadline = deadline

    def connect(self):
        # 直接连接已校验的 sockaddr，不让 HTTP 库再次解析 DNS；TLS 仍校验原域名。
        for family, socktype, proto, address in self.addresses:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ReadError("fetch_timeout")
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(min(5, remaining))
                sock.connect(address)
                if self.secure:
                    context = ssl.create_default_context()
                    context.set_alpn_protocols(["http/1.1"])
                    sock = context.wrap_socket(sock, server_hostname=self.host)
                self.sock = sock
                return
            except (OSError, ssl.SSLError):
                sock.close()
        raise ReadError("connection_error")


def download(url, deadline, max_bytes):
    target = url
    for redirect in range(4):
        canonical, host, port, addresses = resolve_target(target)
        parsed = urlsplit(canonical)
        connection = PinnedConnection(host, port, addresses, parsed.scheme == "https", deadline)
        try:
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={
                "Host": parsed.netloc,
                "User-Agent": "BrowserAgent-WebReader/0.1 (+public-page-extraction)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == 3:
                    raise ReadError("redirect_limit")
                target = urljoin(canonical, location)
                continue
            if response.status != 200:
                raise ReadError("http_" + str(response.status))
            mime = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if mime not in ("text/html", "application/xhtml+xml"):
                raise ReadError("unsupported_content_type")
            length = response.getheader("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ReadError("page_too_large")
            encoding = (response.getheader("Content-Encoding") or "identity").lower()
            if encoding not in ("identity", "gzip", "deflate"):
                raise ReadError("unsupported_encoding")
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding != "identity" else None
            output = bytearray()
            transferred = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReadError("fetch_timeout")
                if connection.sock:
                    connection.sock.settimeout(min(5, remaining))
                block = response.read1(32768)
                if not block:
                    break
                transferred += len(block)
                if transferred > max_bytes:
                    raise ReadError("page_too_large")
                if decoder:
                    block = decoder.decompress(block, max_bytes - len(output) + 1)
                    if decoder.unconsumed_tail:
                        raise ReadError("page_too_large")
                output.extend(block)
                if len(output) > max_bytes:
                    raise ReadError("page_too_large")
            if decoder and not decoder.eof:
                raise ReadError("invalid_encoding")
            return bytes(output), canonical
        except ReadError:
            raise
        except (TimeoutError, socket.timeout):
            raise ReadError("fetch_timeout") from None
        except (OSError, http.client.HTTPException, zlib.error):
            raise ReadError("download_error") from None
        finally:
            connection.close()
    raise ReadError("redirect_limit")


def extract_html(html, final_url, max_chars):
    from .structure import serialize_body

    document = trafilatura.bare_extraction(
        html, url=final_url, include_comments=False, include_tables=True,
        include_links=False, include_formatting=False, with_metadata=True, output_format="xml",
    )
    if document is None or document.body is None:
        raise ReadError("empty_content")
    result = serialize_body(document.body, max_chars)
    content = result["content"]
    if not content:
        raise ReadError("content_limit_exceeded" if result["truncated"] else "empty_content")
    title = (document.title or "")[:500]
    # 仅对短小的挑战/登录页面拒绝，不按正文提到 captcha 等词误杀技术文章。
    if len(content) < 2000 and re.match(
        r"^(just a moment|access denied|attention required|verify (you|your)|sign in|log in|登录|安全验证|人机验证)\b",
        title.strip(), re.I,
    ):
        raise ReadError("interstitial_page")
    return {"title": title, **result, "extracted_date": (document.date or "")[:64] or None}


def read_page(url, timeout_seconds, max_bytes, max_chars):
    started = time.monotonic()
    html, final_url = download(url, started + timeout_seconds, max_bytes)
    result = extract_html(html, final_url, max_chars)
    return {"status": "ok", "url": url, "final_url": final_url, **result,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": round((time.monotonic() - started) * 1000)}


def worker(sender, url, timeout_seconds, max_bytes, max_chars):
    try:
        sender.send(read_page(url, timeout_seconds, max_bytes, max_chars))
    except ReadError as exc:
        sender.send({"status": "error", "error_code": str(exc)})
    except Exception:
        sender.send({"status": "error", "error_code": "extraction_error"})
    finally:
        sender.close()
