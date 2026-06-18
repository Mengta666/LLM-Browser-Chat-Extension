"""Qdrant 向量存储封装。

该模块只处理向量 collection、point upsert、payload 过滤搜索和旧 snapshot 清理。
业务层的 page/chat 绑定关系保存在 SQLite，不在这里维护。
"""
import os
from typing import Any

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.http.models import UpdateResult
from dotenv import load_dotenv
from pathlib import Path

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

# 配置加载
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "browser_agent_chunks")
QDRANT_VECTOR_SIZE = int(os.getenv("QDRANT_VECTOR_SIZE", "1024"))
QDRANT_DISTANCE = os.getenv("QDRANT_DISTANCE", "Cosine")

client = QdrantClient(
    url=QDRANT_URL,
    api_key=QDRANT_API_KEY,
)


def _resolve_distance() -> models.Distance:
    """把环境变量中的距离名称转换成 Qdrant SDK 枚举。"""
    try:
        return models.Distance(QDRANT_DISTANCE)
    except ValueError as exc:
        raise ValueError(f"Unsupported QDRANT_DISTANCE: {QDRANT_DISTANCE}") from exc


def _normalize_vector_size(vector_size: int | None) -> int:
    """解析 collection 向量维度，默认使用环境变量配置。"""
    resolved_size = vector_size or QDRANT_VECTOR_SIZE
    if not isinstance(resolved_size, int) or resolved_size <= 0:
        raise ValueError("vector_size must be a positive integer")
    return resolved_size


def _get_collection_vector_params(collection_name: str) -> models.VectorParams:
    """读取 collection 当前向量配置，用于启动时校验维度和距离。"""
    collection_info = client.get_collection(collection_name=collection_name)
    vectors_config = collection_info.config.params.vectors
    if isinstance(vectors_config, dict):
        raise RuntimeError("Named vectors are not supported by current vector_store.py")
    return vectors_config


def _build_chunk_filter(
    snapshot_ids: list[str],
    embedding_model: str,
    chunker_version: str,
) -> models.Filter:
    """构造 snapshot/model/chunker 三维过滤条件，避免跨版本误召回。"""
    normalized_snapshot_ids = [item for item in dict.fromkeys(snapshot_ids) if item]
    if not normalized_snapshot_ids:
        raise ValueError("snapshot_ids must not be empty")
    if not embedding_model:
        raise ValueError("embedding_model must not be empty")
    if not chunker_version:
        raise ValueError("chunker_version must not be empty")

    return models.Filter(
        must=[
            models.FieldCondition(
                key="snapshot_id",
                match=models.MatchAny(any=normalized_snapshot_ids),
            ),
            models.FieldCondition(
                key="embedding_model",
                match=models.MatchValue(value=embedding_model),
            ),
            models.FieldCondition(
                key="chunker_version",
                match=models.MatchValue(value=chunker_version),
            ),
        ]
    )


def ensure_collection(vector_size: int | None = None) -> None:
    """确保 collection 存在，并校验维度和距离配置一致。"""
    resolved_size = _normalize_vector_size(vector_size)
    resolved_distance = _resolve_distance()
    collections = client.get_collections().collections
    collection_names = [c.name for c in collections]
    if QDRANT_COLLECTION not in collection_names:
        # Qdrant collection 的向量维度创建后不可随意变更，所以首次创建必须用真实维度。
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=models.VectorParams(
                size=resolved_size,
                distance=resolved_distance,
            )
        )
        print(f"Collection {QDRANT_COLLECTION} created.")
        return

    vector_params = _get_collection_vector_params(QDRANT_COLLECTION)
    if vector_params.size != resolved_size:
        raise RuntimeError(
            f"Collection {QDRANT_COLLECTION} vector size mismatch: "
            f"expected {resolved_size}, got {vector_params.size}"
        )
    if vector_params.distance != resolved_distance:
        raise RuntimeError(
            f"Collection {QDRANT_COLLECTION} distance mismatch: "
            f"expected {resolved_distance}, got {vector_params.distance}"
        )
    print(f"Collection {QDRANT_COLLECTION} already exists.")

def snapshot_exists(
    snapshot_id: str,
    embedding_model: str,
    chunker_version: str,
) -> bool:
    """
    判断 snapshot 是否已索引。
    通过在 payload 中匹配 snapshot_id, model, chunker_version 来判断。
    """
    if not snapshot_id:
        raise ValueError("snapshot_id must not be empty")
    try:
        # 只要同一 snapshot/model/chunker 下存在任意 point，就认为该快照已完成索引。
        count = client.count(
            collection_name=QDRANT_COLLECTION,
            count_filter=_build_chunk_filter([snapshot_id], embedding_model, chunker_version),
            exact=True,
        )
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            # collection 被删除或尚未创建时，业务层会在写入前重新 ensure_collection。
            return False
        raise
    return count.count > 0


def upsert_chunk_points(points: list[dict]) -> UpdateResult | None:
    """
    批量上传点
    points 格式: [
        {"id": "point_id_1", "vector": [0.1, ...], "payload": {"snapshot_id": "...", "chunk_id": "...", ...}},
        ...
    ]
    """
    if not points:
        return None

    normalized_points: list[models.PointStruct] = []
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"points[{index}] must be a dict")
        if "id" not in point:
            raise ValueError(f"points[{index}].id is required")

        vector = point.get("vector")
        if not isinstance(vector, list) or not vector:
            raise ValueError(f"points[{index}].vector must be a non-empty list")
        # 当前只支持单向量 collection，所以每个 point 的向量长度必须完全一致。
        if len(vector) != QDRANT_VECTOR_SIZE:
            raise ValueError(
                f"points[{index}].vector size mismatch: "
                f"expected {QDRANT_VECTOR_SIZE}, got {len(vector)}"
            )

        payload = point.get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"points[{index}].payload must be a dict")

        normalized_points.append(
            models.PointStruct(
                id=point["id"],
                vector=vector,
                payload=payload,
            )
        )

    operation_info = client.upsert(
        collection_name=QDRANT_COLLECTION,
        points=normalized_points,
        wait=True,
    )
    return operation_info


def search_chunks(
    query_vector: list[float],
    snapshot_ids: list[str],
    embedding_model: str,
    chunker_version: str,
    top_k: int = 10,
) -> list[dict]:
    """
    在指定的 snapshot_ids 范围内搜索最相似的 chunks
    :param query_vector:
    :param snapshot_ids:
    :param embedding_model:
    :param chunker_version:
    :param top_k:
    :return:
    """
    if not snapshot_ids:
        return []
    if not isinstance(query_vector, list) or not query_vector:
        raise ValueError("query_vector must be a non-empty list")
    if len(query_vector) != QDRANT_VECTOR_SIZE:
        raise ValueError(
            f"query_vector size mismatch: expected {QDRANT_VECTOR_SIZE}, got {len(query_vector)}"
        )
    if not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")

    search_results = client.query_points(
        collection_name=QDRANT_COLLECTION,
        query=query_vector,
        query_filter=_build_chunk_filter(snapshot_ids, embedding_model, chunker_version),
        limit=top_k,
        with_payload=True,
    )
    # 将 Qdrant 结果转换为简单字典列表；新版 SDK 返回 QueryResponse，结果在 points 里。
    hits = search_results.points if hasattr(search_results, "points") else []
    return [
        {
            "point_id": hit.id,
            "score": hit.score,
            "payload": hit.payload,
        }
        for hit in hits
    ]


def reset_collection():
    """彻底删除 collection。仅用于本地手工维护，不应在业务请求中调用。"""
    client.delete_collection(collection_name=QDRANT_COLLECTION)


def delete_snapshot_data(snapshot_id: str) -> UpdateResult | None:
    """根据单个 snapshot_id 删除所有相关 points。"""
    return delete_snapshots_data([snapshot_id])


def delete_snapshots_data(snapshot_ids: list[str]) -> UpdateResult | None:
    """根据 snapshot_id 批量删除相关 points。"""
    normalized_snapshot_ids = [item for item in dict.fromkeys(snapshot_ids) if item]
    if not normalized_snapshot_ids:
        return None

    result = client.delete(
        collection_name=QDRANT_COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="snapshot_id",
                        match=models.MatchAny(any=normalized_snapshot_ids)
                    )
                ]
            )
        )
    )
    print(f"All points for snapshots {normalized_snapshot_ids} have been cleared.")
    return result


def _build_self_test_points(
    test_snapshot_id: str,
    test_model: str,
    test_ver: str,
) -> list[dict[str, Any]]:
    """构造两个正交测试向量，供本文件手工自测使用。"""
    if QDRANT_VECTOR_SIZE < 2:
        raise ValueError("QDRANT_VECTOR_SIZE must be at least 2 for vector_store self-test")

    vector_1 = [0.0] * QDRANT_VECTOR_SIZE
    vector_1[0] = 1.0
    vector_2 = [0.0] * QDRANT_VECTOR_SIZE
    vector_2[1] = 1.0

    return [
        {
            "id": "vector_store_self_test_1",
            "vector": vector_1,
            "payload": {
                "snapshot_id": test_snapshot_id,
                "chunk_id": "chunk_1",
                "embedding_model": test_model,
                "chunker_version": test_ver,
            },
        },
        {
            "id": "vector_store_self_test_2",
            "vector": vector_2,
            "payload": {
                "snapshot_id": test_snapshot_id,
                "chunk_id": "chunk_2",
                "embedding_model": test_model,
                "chunker_version": test_ver,
            },
        },
    ]


if __name__ == "__main__":
    # 注意：当前 __main__ 会删除 collection，只能在本地明确需要重置向量库时运行。
    reset_collection()
    exit(0)
    # 1. 准备测试数据。自测不删除 collection，只覆盖固定测试 point。
    ensure_collection(vector_size=QDRANT_VECTOR_SIZE)
    print("collection ready")

    test_snapshot_id = "snap_test_123"
    test_model = "test-embed-v1"
    test_ver = "v1"

    mock_points = _build_self_test_points(test_snapshot_id, test_model, test_ver)

    # 2. 测试 Upsert
    print(upsert_chunk_points(mock_points))
    print("upsert ok")

    # 3. 测试 snapshot_exists
    exists = snapshot_exists(test_snapshot_id, test_model, test_ver)
    print(f"snapshot exists: {exists}")

    # 4. 测试 Search (Query 靠近 chunk1)
    query_vec = [0.0] * QDRANT_VECTOR_SIZE
    query_vec[0] = 0.9
    query_vec[1] = 0.1
    results = search_chunks(query_vec, [test_snapshot_id], test_model, test_ver)

    print(f"search result count: {len(results)}")
    if results:
        print(f"top result chunk_id: {results[0]['payload'].get('chunk_id')}")
        print(f"top score: {results[0]['score']}")
    delete_snapshot_data(test_snapshot_id)
