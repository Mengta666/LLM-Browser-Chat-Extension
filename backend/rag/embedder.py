"""Embedding 调用封装，负责把文本转换成向量。"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(__env_path)

__embedding_client = OpenAI(
    api_key=os.environ.get("EMBEDDING_API_KEY"),
    base_url=os.environ.get("EMBEDDING_BASE_URL"),
)


def _resolve_embedding_model(model: str | None) -> str:
    """优先使用显式传入的模型名，否则回退到环境变量。"""
    resolved_model = model if model is not None else os.environ.get("EMBEDDING_MODEL")
    if not isinstance(resolved_model, str) or not resolved_model.strip():
        raise RuntimeError("EMBEDDING_MODEL is not configured")
    return resolved_model.strip()


def embed_text(text: str, model: str | None = None) -> list[float]:
    """对单条文本生成 embedding 向量。"""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if not text.strip():
        raise ValueError("text must not be empty")

    response = __embedding_client.embeddings.create(
        input=text,
        model=_resolve_embedding_model(model),
    )
    if len(response.data) != 1:
        raise RuntimeError("embedding response size mismatch")

    embedding = response.data[0].embedding
    if not embedding:
        raise RuntimeError("embedding response is empty")
    return embedding


def embed_texts(texts: list[str], model: str | None = None) -> list[list[float]]:
    """对一组文本批量生成 embedding 向量。"""
    if not isinstance(texts, list):
        raise ValueError("texts must be a list")
    if not texts:
        raise ValueError("texts must not be empty")

    for index, text in enumerate(texts):
        if not isinstance(text, str):
            raise ValueError(f"text at index {index} must be a string")
        if not text.strip():
            raise ValueError(f"text at index {index} must not be empty")

    response = __embedding_client.embeddings.create(
        input=texts,
        model=_resolve_embedding_model(model),
    )
    if len(response.data) != len(texts):
        raise RuntimeError("embedding response size mismatch")

    embeddings = [data.embedding for data in response.data]
    if any(not embedding for embedding in embeddings):
        raise RuntimeError("embedding response contains empty vector")
    return embeddings


if __name__ == "__main__":
    print(len(embed_text("This is a sample text.")))
    print(len(embed_texts(["I love you", "I love coding"])))
