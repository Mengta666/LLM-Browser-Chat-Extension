"""显式检查所选服务；不会修改配置，不会自动调用模型。"""

import argparse
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_settings(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def check(settings, reader_url=None, search_url=None, page_url=None):
    results = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    if reader_url:
        try:
            with opener.open(reader_url.rstrip("/") + "/health", timeout=5) as response:
                health = json.load(response)
            ok = health.get("service") == "web-reader" and health.get("api_version") == "v1"
            results.append({"check": "reader_health", "ok": ok, "version": health.get("version")})
            if page_url:
                request = urllib.request.Request(reader_url.rstrip("/") + "/v1/extract",
                    data=json.dumps({"url": page_url, "timeout_seconds": 15}).encode(),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings.get("WEB_READER_API_KEY", "")})
                with opener.open(request, timeout=18) as response:
                    extracted = json.load(response)
                results.append({"check": "reader_extract", "ok": extracted.get("status") == "ok",
                                "content_length": extracted.get("content_length", 0),
                                "truncated": extracted.get("truncated", False),
                                "structure_version": extracted.get("structure_version"),
                                "block_count": len(extracted.get("blocks") or []),
                                "error_code": extracted.get("error_code")})
        except (OSError, ValueError) as exc:
            results.append({"check": "reader", "ok": False, "error_type": type(exc).__name__})
    if search_url:
        try:
            url = search_url + ("&" if "?" in search_url else "?") + urllib.parse.urlencode({"q": "Python documentation", "format": "json"})
            with opener.open(url, timeout=30) as response:
                found = json.load(response)
            results.append({"check": "search", "ok": bool(found.get("results")),
                            "result_count": len(found.get("results", [])),
                            "failed_engine_count": len(found.get("unresponsive_engines", []))})
        except (OSError, ValueError) as exc:
            results.append({"check": "search", "ok": False, "error_type": type(exc).__name__})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parent / ".env")
    parser.add_argument("--reader-url", help="正文服务基址，如 http://127.0.0.1:19081")
    parser.add_argument("--search-url", help="SearXNG 完整 /search 地址；仅显式指定才检查")
    parser.add_argument("--url", help="显式选择要抓取的公开网页；不指定则只检查健康状态")
    args = parser.parse_args()
    if not args.reader_url and not args.search_url:
        parser.error("Choose --reader-url and/or --search-url")
    if args.url and not args.reader_url:
        parser.error("--url requires --reader-url")
    report = check(load_settings(args.env_file), args.reader_url, args.search_url, args.url)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(0 if report and all(item["ok"] for item in report) else 1)
