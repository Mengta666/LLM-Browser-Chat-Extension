"""聊天接口手工联调用脚本。"""

import argparse
import json
import urllib.error
import urllib.request
from typing import Any


DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1/chat/completions"
DEFAULT_MODEL = "cyankiwi/gemma-4-31B-it-AWQ-4bit"
SOURCE_REQUIRED_KEYS = {"source_id", "url", "title", "content", "score"}


def build_payload(model: str, stream: bool) -> dict[str, Any]:
    """构造一份固定的手工测试请求体。"""
    return {
        "model": model,
        "stream": stream,
        "task_type": "explain",
        "focus_text": "RAG",
        "query_text": "请结合页面内容解释，并带上引用。",
        "use_current_page": True,
        "chat_id": "chat_manual_rag",
        "page_context_id": "manual-rag-page",
        "current_page": {
            "url": "https://example.com/rag-intro",
            "title": "RAG 简介",
            "content": (
                "RAG 是检索增强生成。它会先从外部知识中召回相关片段，"
                "再把这些片段和用户问题一起交给模型生成答案。这样做可以减少幻觉，"
                "并让回答更贴近指定资料。"
            ),
        },
        "messages": [
            {
                "role": "user",
                "content": "请解释一下 RAG。",
            }
        ],
    }


def validate_sources(sources: list[dict[str, Any]]) -> None:
    """校验 sources 至少具备前端当前依赖的字段。"""
    for index, source in enumerate(sources):
        missing = SOURCE_REQUIRED_KEYS - set(source.keys())
        if missing:
            raise ValueError(f"sources[{index}] missing keys: {sorted(missing)}")


def post_json(endpoint: str, payload: dict[str, Any]) -> None:
    """发送非流式请求并打印完整 JSON 响应。"""
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}")
        print(exc.read().decode("utf-8", errors="replace"))
        return

    validate_sources(body.get("sources", []))
    print(json.dumps(body, ensure_ascii=False, indent=2))


def post_stream(endpoint: str, payload: dict[str, Any]) -> None:
    """发送流式请求，并单独收集末尾的 sources 事件。"""
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "accept": "text/event-stream",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            assistant_parts: list[str] = []
            sources: list[dict[str, Any]] = []

            while True:
                line = response.readline()
                if not line:
                    break

                decoded = line.decode("utf-8").strip()
                if not decoded or not decoded.startswith("data: "):
                    continue

                data_text = decoded[6:]
                if data_text == "[DONE]":
                    break

                payload_json = json.loads(data_text)
                # sources 事件不直接拼到正文，而是单独保存。
                if payload_json.get("type") == "sources":
                    sources = payload_json.get("sources", [])
                    continue

                delta = (
                    payload_json.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if delta:
                    assistant_parts.append(delta)
                    print(delta, end="")
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}")
        print(exc.read().decode("utf-8", errors="replace"))
        return

    print("\n")
    validate_sources(sources)
    print(json.dumps({"sources": sources}, ensure_ascii=False, indent=2))


def main() -> None:
    """命令行入口，可切换流式或非流式测试。"""
    parser = argparse.ArgumentParser(description="Manual point-to-point chat test.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    payload = build_payload(args.model, args.stream)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("")

    if args.stream:
        post_stream(args.endpoint, payload)
    else:
        post_json(args.endpoint, payload)


if __name__ == "__main__":
    main()
