"""仅规范化 URL 身份，不代替正文服务的网络安全校验。"""

from urllib.parse import urlsplit, urlunsplit


def normalize_web_url(url):
    """规范化 URL 的域名、默认端口和片段标识，用于来源去重而非访问授权。"""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return ""
        host = parsed.hostname.encode("idna").decode().lower().rstrip(".")
        authority = f"[{host}]" if ":" in host else host
        if parsed.port and parsed.port != (443 if parsed.scheme == "https" else 80):
            authority += f":{parsed.port}"
        return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        return ""
