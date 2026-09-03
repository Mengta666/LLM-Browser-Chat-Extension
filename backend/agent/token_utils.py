"""共享 token 估算(复用 tiktoken cl100k_base,兜底字符启发式)。

供 agent/loop.py、api/chat.py、agent/memory/chat_compact.py 三处复用。
图片固定 1100 token 常量(粗略,避免 base64 dataURL 撑爆计数)。
"""

from typing import Any

try:
    import tiktoken
    _tok_enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _tok_enc = None

_IMG_TOKEN = 1100


def _count_text(text: str) -> int:
    if not text:
        return 0
    if _tok_enc is not None:
        try:
            return len(_tok_enc.encode(text))
        except Exception:
            pass
    zh = sum(1 for c in text if ord(c) > 127)
    return int((len(text) - zh) / 4 + zh / 1.5)


def _msg_text(m: dict[str, Any]) -> str:
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            p.get("text", "") for p in c
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _img_count(m: dict[str, Any]) -> int:
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, list):
        return sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
    return 0


def estimate_tokens(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """估算消息列表的 token 数。返回 {total, text, images, method}。"""
    text_tok = sum(_count_text(_msg_text(m)) for m in (messages or []))
    n_img = sum(_img_count(m) for m in (messages or []))
    return {
        "total": text_tok + n_img * _IMG_TOKEN,
        "text": text_tok,
        "images": n_img,
        "method": "tiktoken" if _tok_enc is not None else "heuristic",
    }


def estimate_text_tokens(text: str) -> int:
    """估算单段文本的 token 数(不含图片)。"""
    return _count_text(text)
