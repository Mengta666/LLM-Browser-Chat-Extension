"""针对本地后端运行一次串行计划模式验收流程。

脚本遵循前端顺序：创建计划 -> 批准计划 -> 执行一次有状态聊天 -> 完成计划。
遇到首个失败会立即退出，不并发发送模型请求。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_MODEL = "cyankiwi/gemma-4-31B-it-AWQ-4bit"
DEFAULT_OBJECTIVE = (
    "Analyze RAG and GraphRAG in LLM applications. Cover technical principles, "
    "key differences, applicable scenarios, risks, and selection guidance."
)
EXECUTE_PROMPT = (
    "Start executing the approved plan. Complete all unfinished steps in one pass "
    "and output the actual result. Do not only execute the first step, and do not "
    "only restate the plan."
)


class LivePlanTestError(RuntimeError):
    """计划模式 live 测试的显式失败异常。"""

    pass


def assert_true(condition: bool, message: str) -> None:
    """使用可读错误替代裸 assert。"""
    if not condition:
        raise LivePlanTestError(message)


def build_url(base_url: str, path: str) -> str:
    """拼接 base URL 和接口路径。"""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """发送 JSON 请求并返回对象形式 JSON 响应。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        build_url(base_url, path),
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise LivePlanTestError(f"{method} {path} failed with HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise LivePlanTestError(f"{method} {path} failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LivePlanTestError(f"{method} {path} returned invalid JSON: {body[:500]}") from exc
    if not isinstance(parsed, dict):
        raise LivePlanTestError(f"{method} {path} returned non-object JSON")
    return parsed


def stream_chat(
    base_url: str,
    payload: dict[str, Any],
    timeout: int = 600,
) -> dict[str, Any]:
    """调用流式聊天接口并聚合最终正文和 sources。"""
    request = urllib.request.Request(
        build_url(base_url, "/v1/chat/completions"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    final_text = ""
    chunk_text = ""
    sources: list[dict[str, Any]] = []
    seen_done = False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            while True:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text or not text.startswith("data: "):
                    continue
                data_text = text[6:]
                if data_text == "[DONE]":
                    seen_done = True
                    break
                try:
                    event = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "final_answer":
                    final_text = str(event.get("content") or "")
                    continue
                if event.get("type") == "sources":
                    event_sources = event.get("sources")
                    if isinstance(event_sources, list):
                        sources = event_sources
                    continue
                choices = event.get("choices")
                if isinstance(choices, list) and choices:
                    delta = choices[0].get("delta") or {}
                    if isinstance(delta, dict):
                        chunk_text += str(delta.get("content") or "")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise LivePlanTestError(f"chat stream failed with HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise LivePlanTestError(f"chat stream failed: {exc}") from exc

    resolved_text = final_text or chunk_text
    assert_true(seen_done, "chat stream did not send [DONE]")
    assert_true(bool(resolved_text.strip()), "chat execution returned empty text")
    return {"content": resolved_text, "sources": sources}


def extract_plan_markdown(plan: dict[str, Any]) -> str:
    """从计划响应中提取当前版本 Markdown。"""
    revision = plan.get("current_revision") or {}
    return str(revision.get("plan_markdown") or "").strip()


def assert_plan_not_duplicated_in_chat(base_url: str, chat_id: str, plan: dict[str, Any]) -> None:
    """确认计划 Markdown 没有被重复写进聊天助手消息。"""
    messages = request_json(base_url, "GET", f"/api/chats/{chat_id}/messages").get("messages")
    assert_true(isinstance(messages, list), "chat messages response missing messages list")
    plan_markdown = extract_plan_markdown(plan)
    assistant_messages = [
        str(message.get("display_content") or "")
        for message in messages
        if message.get("role") == "assistant"
    ]
    if plan_markdown:
        assert_true(
            all(plan_markdown not in content for content in assistant_messages),
            "plan markdown was duplicated into chat assistant messages",
        )


def summarize_steps(plan: dict[str, Any]) -> list[dict[str, str]]:
    """提取计划步骤摘要用于最终 JSON 输出。"""
    result = []
    for step in plan.get("steps") or []:
        result.append(
            {
                "title": str(step.get("title") or ""),
                "status": str(step.get("status") or ""),
                "updated_by": str(step.get("updated_by") or ""),
            }
        )
    return result


def run_flow(args: argparse.Namespace) -> dict[str, Any]:
    """串联完整计划验收流程，并在每步做关键断言。"""
    chat_id = args.chat_id or f"chat_plan_live_{uuid.uuid4().hex[:12]}"
    context_options = {
        "use_current_page": False,
        "use_web_search": bool(args.use_web_search),
        "force_refresh_page": False,
        "web_search_query": args.web_search_query,
    }

    create_payload = {
        "model": args.model,
        "objective": args.objective,
        "context_options": context_options,
    }
    print(f"[1/6] create plan: {chat_id}")
    created = request_json(args.base_url, "POST", f"/api/chats/{chat_id}/plans", create_payload)
    plan = created.get("plan") or {}
    plan_id = str(plan.get("plan_id") or "")
    assert_true(bool(plan_id), "create plan response missing plan_id")
    assert_true(plan.get("status") == "draft", f"expected draft plan, got {plan.get('status')}")
    assert_true(bool(plan.get("current_revision")), "created plan missing current_revision")
    assert_plan_not_duplicated_in_chat(args.base_url, chat_id, plan)

    print(f"[2/6] approve plan: {plan_id}")
    approved = request_json(args.base_url, "POST", f"/api/plans/{plan_id}/approve")
    approved_plan = approved.get("plan") or {}
    task_memory = approved.get("task_memory") or {}
    assert_true(approved_plan.get("status") == "executing", "approve did not set plan to executing")
    assert_true(task_memory.get("memory_type") == "task_state", "approve did not create task_state memory")
    assert_true(task_memory.get("task_status") == "open", "task_state is not open after approve")
    assert_true(task_memory.get("plan_id") == plan_id, "task_state is not bound to plan_id")
    assert_plan_not_duplicated_in_chat(args.base_url, chat_id, approved_plan)

    execution_query = args.execute_prompt
    if args.include_plan_steps:
        step_lines = [
            f"{index}. {step.get('title', '')}: {step.get('detail', '')}"
            for index, step in enumerate(approved_plan.get("steps") or [], start=1)
        ]
        if step_lines:
            execution_query = execution_query + "\n\nPlan steps:\n" + "\n".join(step_lines)

    chat_payload = {
        "model": args.model,
        "stream": True,
        "chat_id": chat_id,
        "current_turn": {
            "task_type": "chat",
            "query_text": execution_query,
            "focus_text": "",
        },
        "context_options": {
            "use_current_page": False,
            "use_web_search": bool(args.use_web_search),
            "force_refresh_page": False,
            "web_search_query": args.web_search_query or args.objective,
        },
    }
    print("[3/6] execute approved plan with one stateful chat stream")
    execution = stream_chat(args.base_url, chat_payload)

    if args.pause_after_execution > 0:
        time.sleep(args.pause_after_execution)

    print("[4/6] complete plan")
    completed = request_json(args.base_url, "POST", f"/api/plans/{plan_id}/complete")
    completed_plan = completed.get("plan") or {}
    assert_true(completed_plan.get("status") == "done", "complete did not set plan to done")
    assert_true(bool(completed_plan.get("completed_at")), "completed plan missing completed_at")
    steps = completed_plan.get("steps") or []
    assert_true(bool(steps), "completed plan missing steps")
    assert_true(all(step.get("status") == "done" for step in steps), "not all steps are done")
    assert_true(
        all(step.get("updated_by") == "assistant" for step in steps),
        "not all steps were marked done by assistant",
    )

    print("[5/6] verify no active plan remains")
    active = request_json(args.base_url, "GET", f"/api/chats/{chat_id}/plans/active")
    assert_true(active.get("plan") is None, "done plan is still returned as active")

    print("[6/6] verify persisted plan")
    fetched = request_json(args.base_url, "GET", f"/api/plans/{plan_id}")
    fetched_plan = fetched.get("plan") or {}
    assert_true(fetched_plan.get("status") == "done", "fetched plan is not done")

    return {
        "chat_id": chat_id,
        "plan_id": plan_id,
        "title": completed_plan.get("title"),
        "status": completed_plan.get("status"),
        "steps": summarize_steps(completed_plan),
        "execution_chars": len(execution["content"]),
        "source_count": len(execution["sources"]),
    }


def main() -> None:
    """命令行入口，执行 live 流程并打印通过结果。"""
    parser = argparse.ArgumentParser(description="Run one serial live plan acceptance flow.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--objective", default=DEFAULT_OBJECTIVE)
    parser.add_argument("--execute-prompt", default=EXECUTE_PROMPT)
    parser.add_argument("--use-web-search", action="store_true")
    parser.add_argument("--web-search-query", default="")
    parser.add_argument("--include-plan-steps", action="store_true")
    parser.add_argument("--pause-after-execution", type=float, default=0.0)
    args = parser.parse_args()

    result = run_flow(args)
    print(json.dumps({"passed": True, **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
