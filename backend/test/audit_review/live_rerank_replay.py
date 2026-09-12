"""读取本次测试库的真实片段，重放真实 reranker 查询；不修改知识库。"""

import json
from pathlib import Path

import requests
from dotenv import dotenv_values


def main():
    config = dotenv_values(Path(__file__).resolve().parents[2] / "config" / ".env")
    session = requests.Session()
    session.trust_env = False
    collection = config.get("QDRANT_MEMORY_COLLECTION") or "agent_memories"
    response = session.post(
        config["QDRANT_URL"].rstrip("/") + f"/collections/{collection}/points/scroll",
        headers={"api-key": config.get("QDRANT_API_KEY") or ""},
        json={
            "filter": {"must": [
                {"key": "kb_id", "match": {"value": "kb_171d1dfb"}},
                {"key": "memory_type", "match": {"value": "kb_chunk"}},
                {"key": "valid", "match": {"value": True}},
            ]},
            "limit": 100, "with_payload": True, "with_vector": False,
        }, timeout=8,
    )
    response.raise_for_status()
    chunks = [point["payload"] for point in response.json()["result"]["points"]]
    if not chunks:
        raise SystemExit("Test KB has no valid chunks")
    print(json.dumps({"event": "stored_chunks", "chunks": [
        {key: chunk.get(key) for key in ("source", "chunk_id", "prev_chunk_id", "next_chunk_id")}
        for chunk in chunks
    ]}), flush=True)
    queries = [
        "Atlas-R7 送料轴承 型号 存放库位 更换周期",
        "Atlas-R7 轴承 型号 库位",
        "Atlas-R7 送料轴承的型号、存放库位与更换周期分别是什么？",
        "Atlas-R7 送料轴承 型号 存放库位 更换周期",
        "Luma-X9 水泵 叶轮直径",
    ]
    for query in queries:
        response = session.post(
            config["KB_RERANK_API_URL"],
            headers={"Authorization": "Bearer " + (config.get("KB_RERANK_API_KEY") or "")},
            json={
                "model": config["KB_RERANK_MODEL"], "query": query,
                "documents": [chunk["content"] for chunk in chunks], "top_n": len(chunks),
            }, timeout=25,
        )
        response.raise_for_status()
        print(json.dumps({"event": "rerank_replay", "query": query, "scores": [
            {
                "source": chunks[item["index"]].get("source"),
                "chunk_id": chunks[item["index"]].get("chunk_id"),
                "score": item.get("relevance_score"),
            } for item in response.json().get("results", [])
        ]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
