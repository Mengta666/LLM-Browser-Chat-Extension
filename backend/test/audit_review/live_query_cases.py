"""针对本次合成 KB 的真实问答用例；会调用已配置模型并创建单轮测试会话。

仅在服务和配置就绪、用户批准真实测试后手动运行。不被 pytest 自动执行。
输出只包含匹配结果、来源和聚合日志，不打印凭据或完整对话内容。
"""

import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

import requests
from dotenv import dotenv_values
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
KB_ID = "kb_171d1dfb"
BASE = "http://127.0.0.1:8000"
CASES = [
    {
        "id": "alarm",
        "question": "Atlas-R7 的 E731 告警含义是什么？处理前等待几分钟、复位按钮什么颜色、按住几秒？请勿混用 Boreal-T2 参数。",
        "patterns": [r"过滤器.*堵塞|堵塞.*过滤器", r"(?<!\d)17\s*分钟", r"紫色", r"(?<!\d)6\s*秒"],
        "sources": ["atlas-r7-operations.md"],
    },
    {
        "id": "parts",
        "question": "Atlas-R7 送料轴承的型号、存放库位与更换周期分别是什么？",
        "patterns": [r"RB[-－]208", r"C[-－]14", r"450\s*小时"],
        "sources": ["atlas-r7-parts.md"],
    },
    {
        "id": "other_model",
        "question": "Boreal-T2 出现 E731 后，应该等待多久？复位按钮是什么颜色，需要按住多久？只回答 Boreal-T2。",
        "patterns": [r"43\s*分钟", r"黄色", r"(?<!\d)3\s*秒"],
        "sources": ["boreal-t2-handbook.md"],
    },
    {
        "id": "interlock",
        "question": "Atlas-R7 的进料闸门全闭阈值与联锁延时分别是多少？请区分位置和时间单位。",
        "patterns": [r"1\.7\s*毫米", r"360\s*毫秒"],
        "sources": ["atlas-r7-interlock.md"],
    },
    {
        "id": "cross_document",
        "question": "请分别说明 Atlas-R7 的 E731 处理前等待时间，以及送料轴承的更换周期，并标明两项数据的文档来源。",
        "patterns": [r"(?<!\d)17\s*分钟", r"450\s*小时"],
        "sources": ["atlas-r7-operations.md", "atlas-r7-parts.md"],
    },
    {
        "id": "absent_model",
        "question": "Luma-X9 水泵的叶轮直径是多少毫米？请先检索本次绑定知识库；没有资料就明确说未提供，不要猜测或联网。",
        "patterns": [r"未提供|没有.*(?:资料|信息|提供)|未.*(?:找到|检索到|查到|提及)|无法.*(?:确定|回答)"],
        "sources": [],
    },
]


def main():
    config = dotenv_values(BACKEND / "config" / ".env")
    model = config.get("AGENT_MODEL") or config.get("MEMORY_MODEL")
    if not model:
        raise SystemExit("No configured test chat model")
    session = requests.Session()
    session.trust_env = False
    response = session.get(f"{BASE}/v1/kb/{KB_ID}/docs", timeout=5)
    response.raise_for_status()
    docs = response.json()
    required = {name for case in CASES for name in case["sources"]}
    indexed = {doc["filename"] for doc in docs if doc["status"] == "indexed"}
    if not required.issubset(indexed):
        raise SystemExit("Required test documents are not indexed")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:6]

    for case in CASES:
        chat_id = f"audit_rerank_{run_id}_{case['id']}"
        print(json.dumps({"event": "case_started", "case": case["id"], "chat_id": chat_id}), flush=True)
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        answer = ""
        steps = []
        errors = []
        prompt = (
            "这是合成文档检索测试，不是用户个人事实或实际设备操作。"
            "请使用当前绑定的知识库检索，严格根据其中资料回答，提供 [N] 引用，不调用联网搜索。\n"
            + case["question"]
        )
        try:
            with session.post(f"{BASE}/v1/chat/completions", json={
                "model": model, "messages": [{"role": "user", "content": prompt}],
                "stream": True, "chat_id": chat_id, "kb_id": KB_ID,
            }, stream=True, timeout=(5, 180)) as response:
                response.raise_for_status()
                for raw in response.iter_lines():
                    if not raw or not raw.startswith(b"data:"):
                        continue
                    payload = raw[5:].strip()
                    if payload == b"[DONE]":
                        break
                    data = json.loads(payload)
                    step = data.get("enhancement_step")
                    if step and step.get("status") == "done":
                        steps.append(step)
                        print(json.dumps({"event": "tool_done", "case": case["id"], "tool": step.get("type"), "result_count": step.get("result_count")}), flush=True)
                    for choice in data.get("choices", []):
                        answer += choice.get("delta", {}).get("content") or ""
                        if choice.get("finish_reason") == "error":
                            errors.append("sse_error")
        except Exception as exc:
            errors.append(type(exc).__name__)
        plain = re.sub(r"[*`_#]", "", answer)
        matches = [bool(re.search(pattern, plain, re.S)) for pattern in case["patterns"]]
        source_titles = sorted({s.get("title", "") for step in steps for s in step.get("sources", [])})
        source_match = all(name in source_titles for name in case["sources"])
        metrics = []
        try:
            logs = session.get(f"{BASE}/v1/logs/query", params={"channel": "kb", "event": "kb_search", "limit": 100}, timeout=5)
            logs.raise_for_status()
            for entry in logs.json():
                data = entry.get("data", {})
                if data.get("kb_id") == KB_ID and entry.get("timestamp", "") >= started_at:
                    metrics.append({key: data.get(key) for key in (
                        "raw_count", "reranked_count", "filtered_count", "expanded_count",
                        "rerank_enabled", "top_score", "avg_window", "elapsed_ms",
                    )})
        except Exception as exc:
            errors.append("log_" + type(exc).__name__)
        result = {
            "event": "case_result", "case": case["id"], "chat_id": chat_id,
            "elapsed_s": round(time.monotonic() - started, 2), "answer_chars": len(answer),
            "fact_matches": matches, "expected_sources_found": source_match,
            "sources": source_titles, "tool_calls": len(steps), "errors": errors,
            "answer_check_passed": all(matches) and source_match and bool(steps) and not errors,
            "search_metrics": metrics,
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
