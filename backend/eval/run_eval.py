"""聊天链路回归脚本，支持流式与非流式统一校验。"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1/chat/completions"
DEFAULT_MODEL = "cyankiwi/gemma-4-31B-it-AWQ-4bit"
DEFAULT_CASES_PATH = Path(__file__).with_name("eval_cases.json")
SUCCESS_STATUS = 200
SOURCE_REQUIRED_KEYS = {"source_id", "url", "title", "content", "score"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    """从 JSON 文件加载回归用例。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("eval_cases.json must be a JSON array")
    return data


def parse_json_or_text(raw_text: str) -> Any:
    """优先按 JSON 解析，失败时回退成原始文本。"""
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text


def prepare_request(request_payload: dict[str, Any], default_model: str) -> dict[str, Any]:
    """为单个用例补齐默认模型名。"""
    payload = deepcopy(request_payload)
    if payload.get("model") in {None, "", "__DEFAULT_MODEL__"}:
        payload["model"] = default_model
    return payload


def http_post(endpoint: str, payload: dict[str, Any], accept: str, timeout: int) -> tuple[int, Any, str]:
    """发送普通 HTTP POST 请求，并返回状态码、解析结果和原始文本。"""
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={
            "accept": accept,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8")
            return response.getcode(), parse_json_or_text(raw_text), raw_text
    except urllib.error.HTTPError as exc:
        raw_text = exc.read().decode("utf-8", errors="replace")
        return exc.code, parse_json_or_text(raw_text), raw_text


def run_non_stream(endpoint: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    """执行一次非流式聊天请求。"""
    status, body, raw_text = http_post(endpoint, payload, "application/json", timeout)
    return {
        "status": status,
        "body": body,
        "raw_text": raw_text,
        "assistant_text": extract_assistant_text(body),
        "sources": extract_sources(body),
        "done": True,
        "chunk_event_count": 0,
    }


def run_stream(endpoint: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    """执行一次 SSE 流式聊天请求，并聚合正文与 sources 事件。"""
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={
            "accept": "text/event-stream",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            assistant_parts: list[str] = []
            sources: list[dict[str, Any]] = []
            raw_events: list[str] = []
            done = False
            chunk_event_count = 0

            while True:
                line = response.readline()
                if not line:
                    break

                decoded = line.decode("utf-8").strip()
                if not decoded or not decoded.startswith("data: "):
                    continue

                data_text = decoded[6:]
                raw_events.append(data_text)

                if data_text == "[DONE]":
                    done = True
                    break

                payload_json = json.loads(data_text)
                # sources 事件在流式正文结束后单独下发。
                if payload_json.get("type") == "sources":
                    sources = payload_json.get("sources", [])
                    continue

                chunk_event_count += 1
                delta = (
                    payload_json.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if delta:
                    assistant_parts.append(delta)

            return {
                "status": response.getcode(),
                "body": {"events": raw_events},
                "raw_text": "\n".join(raw_events),
                "assistant_text": "".join(assistant_parts),
                "sources": sources,
                "done": done,
                "chunk_event_count": chunk_event_count,
            }
    except urllib.error.HTTPError as exc:
        raw_text = exc.read().decode("utf-8", errors="replace")
        body = parse_json_or_text(raw_text)
        return {
            "status": exc.code,
            "body": body,
            "raw_text": raw_text,
            "assistant_text": extract_assistant_text(body),
            "sources": extract_sources(body),
            "done": False,
            "chunk_event_count": 0,
        }


def extract_assistant_text(body: Any) -> str:
    """从非流式响应中提取 assistant 文本。"""
    if not isinstance(body, dict):
        return str(body)

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return json.dumps(body, ensure_ascii=False)

    message = choices[0].get("message", {})
    if isinstance(message, dict):
        content = message.get("content", "")
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    return json.dumps(body, ensure_ascii=False)


def extract_sources(body: Any) -> list[dict[str, Any]]:
    """从响应体中提取 sources 数组。"""
    if isinstance(body, dict) and isinstance(body.get("sources"), list):
        return body["sources"]
    return []


def validate_sources_structure(sources: list[dict[str, Any]]) -> list[str]:
    """校验每条 source 是否包含必要字段。"""
    errors: list[str] = []
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"sources[{index}] is not an object")
            continue

        missing_keys = SOURCE_REQUIRED_KEYS - set(source.keys())
        if missing_keys:
            errors.append(f"sources[{index}] missing keys: {sorted(missing_keys)}")
    return errors


def build_assertion_text(result: dict[str, Any]) -> str:
    """为 must_contain / must_not_contain 断言构造文本目标。"""
    if result["status"] == SUCCESS_STATUS and result["assistant_text"]:
        return result["assistant_text"]
    return result["raw_text"]


def validate_case(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """按预期配置检查单个回归用例是否通过。"""
    errors: list[str] = []
    expect = case["expect"]
    expected_status = expect["status"]
    expected_stream = expect["stream"]

    if result["status"] != expected_status:
        errors.append(f"expected status {expected_status}, got {result['status']}")
        return errors

    if expected_stream and result["status"] == SUCCESS_STATUS and not result["done"]:
        errors.append("stream response did not receive [DONE]")

    if result["status"] == SUCCESS_STATUS:
        if expected_stream:
            if result["chunk_event_count"] <= 0:
                errors.append("stream response did not emit any content chunk")
        else:
            body = result["body"]
            if not isinstance(body, dict):
                errors.append("non-stream response body is not JSON object")
            else:
                choices = body.get("choices")
                if not isinstance(choices, list) or not choices:
                    errors.append("non-stream response choices is empty")

        source_errors = validate_sources_structure(result["sources"])
        errors.extend(source_errors)

        # 用例可声明 sources 必须存在、必须为空，或忽略不校验。
        source_mode = expect.get("sources", "ignore")
        source_count_min = expect.get("source_count_min", 0)

        if source_mode == "present":
            minimum = max(1, source_count_min)
            if len(result["sources"]) < minimum:
                errors.append(f"expected at least {minimum} sources, got {len(result['sources'])}")
        elif source_mode == "empty" and result["sources"]:
            errors.append(f"expected sources to be empty, got {len(result['sources'])}")

    assertion_text = build_assertion_text(result)
    for text in expect.get("must_contain", []):
        if text not in assertion_text:
            errors.append(f'must_contain failed: "{text}"')

    for text in expect.get("must_not_contain", []):
        if text in assertion_text:
            errors.append(f'must_not_contain failed: "{text}"')

    return errors


def summarize_request(payload: dict[str, Any]) -> str:
    """输出简短请求摘要，便于失败时定位。"""
    return (
        f"task_type={payload.get('task_type')} "
        f"stream={payload.get('stream')} "
        f"use_current_page={payload.get('use_current_page')} "
        f"messages={len(payload.get('messages', []))}"
    )


def main() -> int:
    """命令行入口，逐个执行并汇总回归结果。"""
    parser = argparse.ArgumentParser(description="Run chat regression cases.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--only", default="")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    if args.only:
        # 支持按名称子串筛选需要重跑的用例。
        cases = [case for case in cases if args.only in case["name"]]

    if not cases:
        print("No eval cases matched.")
        return 1

    passed = 0
    failed = 0

    for index, case in enumerate(cases, start=1):
        payload = prepare_request(case["request"], args.model)
        expect_stream = case["expect"]["stream"]
        result = run_stream(args.endpoint, payload, args.timeout) if expect_stream else run_non_stream(args.endpoint, payload, args.timeout)
        errors = validate_case(case, result)

        prefix = f"[{index:02d}/{len(cases):02d}] {case['name']}"
        if not errors:
            print(f"PASS {prefix}")
            passed += 1
            continue

        failed += 1
        print(f"FAIL {prefix}")
        print(f"  request: {summarize_request(payload)}")
        for error in errors:
            print(f"  - {error}")

    print(f"\nSummary: passed={passed}, failed={failed}, total={len(cases)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
