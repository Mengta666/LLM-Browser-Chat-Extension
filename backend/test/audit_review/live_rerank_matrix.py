"""仅针对既有合成 KB 的精排评测：读取向量载荷，调用已配置的远程 reranker。

不上传、不删除、不调用聊天接口、不创建会话；只打印合成测试查询及分数。
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import dotenv_values


CASES = [
    ("alarm_natural", "Atlas-R7 出现 E731 后需要等待多久，按哪个颜色的复位按钮、按多久？", ["atlas-r7-operations.md"]),
    ("alarm_keywords", "Atlas-R7 E731 等待时间 按钮颜色 按压时长", ["atlas-r7-operations.md"]),
    ("alarm_english", "For Atlas-R7 E731, how long should I wait and which reset button should I hold for how many seconds?", ["atlas-r7-operations.md"]),
    ("boreal_natural", "Boreal-T2 的 E731 表示什么故障，复位前要等待多少分钟？", ["boreal-t2-handbook.md"]),
    ("boreal_keywords", "Boreal-T2 E731 故障含义 复位前等待时间", ["boreal-t2-handbook.md"]),
    ("parts_natural", "Atlas-R7 备用电容的容量是多少？输送带厚度低于多少需要更换？", ["atlas-r7-parts.md"]),
    ("parts_keywords", "Atlas-R7 备用电容容量 输送带厚度 更换阈值", ["atlas-r7-parts.md"]),
    ("parts_english", "What is the spare capacitor capacity and minimum conveyor belt thickness for Atlas-R7?", ["atlas-r7-parts.md"]),
    ("calibration_natural", "Atlas-R7 校准标准块编号是什么，允许测量偏差是多少？", ["atlas-r7-operations.md"]),
    ("calibration_keywords", "Atlas-R7 校准 标准块编号 测量偏差", ["atlas-r7-operations.md"]),
    ("interlock_natural", "Atlas-R7 进料闸门全闭位置阈值和联锁延时分别是多少？", ["atlas-r7-interlock.md"]),
    ("cross_document", "Atlas-R7 校准用什么标准块，送料轴承又是什么型号？", ["atlas-r7-operations.md", "atlas-r7-parts.md"]),
    ("split_calibration", "Atlas-R7 校准用什么标准块？", ["atlas-r7-operations.md"]),
    ("split_bearing", "Atlas-R7 送料轴承是什么型号？", ["atlas-r7-parts.md"]),
    ("absent_device", "Luma-X9 水泵叶轮直径是多少毫米？", []),
    ("absent_fact", "Atlas-R7 主控芯片的时钟频率是多少 MHz？", []),
]


def main():
    config = dotenv_values(Path(__file__).resolve().parents[2] / "config" / ".env")
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        config["QDRANT_URL"].rstrip("/") + f"/collections/{config.get('QDRANT_MEMORY_COLLECTION') or 'agent_memories'}/points/scroll",
        headers={"api-key": config.get("QDRANT_API_KEY") or ""},
        json={"filter": {"must": [
            {"key": "kb_id", "match": {"value": "kb_171d1dfb"}},
            {"key": "memory_type", "match": {"value": "kb_chunk"}},
            {"key": "valid", "match": {"value": True}},
        ]}, "limit": 100, "with_payload": True, "with_vector": False}, timeout=8,
    )
    response.raise_for_status()
    chunks = sorted([point["payload"] for point in response.json()["result"]["points"]],
                    key=lambda chunk: (chunk["source"], chunk["chunk_id"]))
    assert len(chunks) == 6, "Expected exactly the six known synthetic test chunks"
    assert {chunk["source"] for chunk in chunks} == {
        "atlas-r7-operations.md", "atlas-r7-parts.md", "boreal-t2-handbook.md", "atlas-r7-interlock.md",
    }, "Only the known synthetic corpus may be evaluated"
    threshold = float(config.get("KB_RECALL_MIN_SCORE") or 0.5)
    top_k = int(config.get("KB_RERANK_TOP_K") or 5)
    print(json.dumps({"event": "matrix_start", "timestamp": datetime.now(timezone.utc).isoformat(),
                      "model": config["KB_RERANK_MODEL"], "cases": len(CASES), "repeats": 2,
                      "chunks": len(chunks), "threshold": threshold, "top_k": top_k}), flush=True)
    totals = {"positive_runs": 0, "all_expected_sources_retained": 0, "negative_runs": 0, "negative_empty": 0}
    for repeat in (1, 2):
        for case_id, query, expected in CASES:
            started = time.monotonic()
            response = session.post(config["KB_RERANK_API_URL"],
                headers={"Authorization": "Bearer " + (config.get("KB_RERANK_API_KEY") or "")},
                json={"model": config["KB_RERANK_MODEL"], "query": query,
                      "documents": [chunk["content"] for chunk in chunks], "top_n": len(chunks)}, timeout=25)
            response.raise_for_status()
            results = sorted(response.json()["results"], key=lambda row: row["relevance_score"], reverse=True)
            scores = [{"source": chunks[row["index"]]["source"], "chunk_id": chunks[row["index"]]["chunk_id"],
                       "score": row["relevance_score"]} for row in results]
            retained = [row for row in scores[:top_k] if row["score"] >= threshold]
            found = {row["source"] for row in retained}
            passed = set(expected).issubset(found) if expected else not retained
            if expected:
                totals["positive_runs"] += 1
                totals["all_expected_sources_retained"] += int(passed)
            else:
                totals["negative_runs"] += 1
                totals["negative_empty"] += int(passed)
            print(json.dumps({"event": "matrix_case", "case": case_id, "repeat": repeat, "query": query,
                              "expected": expected, "retained": retained, "scores": scores,
                              "pass": passed, "elapsed_s": round(time.monotonic() - started, 3)}, ensure_ascii=False), flush=True)
    print(json.dumps({"event": "matrix_summary", **totals}), flush=True)


if __name__ == "__main__":
    main()
