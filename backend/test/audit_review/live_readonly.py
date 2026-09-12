"""对已启动的本地后端只做 GET 检查；只输出状态和计数，不输出内容。"""

import json
import urllib.error
import urllib.request


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    failed = False
    for endpoint in ("/openapi.json", "/v1/kb", "/v1/sessions/list"):
        try:
            with opener.open("http://127.0.0.1:8000" + endpoint, timeout=5) as response:
                data = json.load(response)
                if endpoint == "/openapi.json":
                    count = len(data.get("paths", {}))
                elif isinstance(data, list):
                    count = len(data)
                else:
                    count = data.get("count")
                print(json.dumps({"endpoint": endpoint, "status": response.status, "count": count}))
        except (urllib.error.URLError, ValueError) as exc:
            failed = True
            print(json.dumps({"endpoint": endpoint, "error_type": type(exc).__name__}))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
