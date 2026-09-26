"""共享 token 估算(复用 tiktoken cl100k_base,兜底字符启发式)。

供 agent/loop.py、api/chat.py、agent/memory/chat_compact.py 三处复用。
图片固定 1100 token 常量(粗略,避免 base64 dataURL 撑爆计数)。
"""

import json
import hashlib
import math
import time
from typing import Any

import httpx

try:
    import tiktoken
    _tok_enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    _tok_enc = None

_IMG_TOKEN = 1100
REQUEST_COUNT_MODE = 'cl100k_request_estimate_1.25x' if _tok_enc is not None else 'heuristic_request_estimate_1.25x'
_tokenizer_client = httpx.Client(trust_env=False, follow_redirects=False)


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
        return sum(1 for p in c if isinstance(p, dict) and p.get("type") in ("image_url", "chat_attachment"))
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


def request_tokens(messages: list[dict], tools=None) -> int:
    metadata = [{key: value for key, value in message.items() if key != 'content'} for message in messages]
    raw = (estimate_tokens(messages)['total'] + len(messages) * 8
           + estimate_text_tokens(json.dumps(metadata, ensure_ascii=False))
           + estimate_text_tokens(json.dumps(tools, ensure_ascii=False) if tools else ''))
    return (raw * 5 + 3) // 4


class RequestTokenCounter:
    """单轮完整聊天请求计数；模型匹配时调用 vLLM，其余情况保守估算。"""

    def __init__(self, model: str, *, deadline=None, time_budget=8.0):
        from agent.memory import config as C
        self.model = model
        self.deadline = deadline
        self.url = C.CHAT_TOKENIZER_URL.strip()
        self.api_key = C.CHAT_TOKENIZER_API_KEY
        self.timeout = max(.1, min(5, C.CHAT_TOKENIZER_TIMEOUT))
        self.ratio = max(1.0, C.CHAT_TOKENIZER_SAFETY_RATIO)
        self.model_window = None
        self.last = {'count_mode': REQUEST_COUNT_MODE, 'tokenizer_fallback': 'not_configured'}
        self._disabled = ('not_configured' if not self.url or not C.CHAT_TOKENIZER_MODEL else
                          'model_mismatch' if model != C.CHAT_TOKENIZER_MODEL else '')
        self._cache = {}
        self._remaining = time_budget

    def __call__(self, messages, tools=None):
        reason = self._disabled
        # 多模态预处理与纯文本分词不同，不把缺少图片开销的计数当作精确结果。
        if any(isinstance(m.get('content'), list) and any(p.get('type') != 'text' for p in m['content'])
               for m in messages):
            reason = 'multimodal_estimate'
        payload = {'model': self.model, 'messages': messages, 'add_generation_prompt': True}
        if tools:
            payload['tools'] = tools
        return self._count(payload, lambda: request_tokens(messages, tools), reason=reason)

    def count_text(self, text):
        return self._count({'model': self.model, 'prompt': text, 'add_special_tokens': False},
                           lambda: estimate_text_tokens(text), body=True, reason=self._disabled)

    def _count(self, payload, fallback, *, reason='', body=False):
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).digest()
        if not reason and key in self._cache:
            count, self.last = self._cache[key]
            return count
        if not reason:
            remaining = min(self._remaining, self.deadline - time.monotonic() if self.deadline is not None else float('inf'))
            if remaining <= 0:
                reason = self._disabled = 'tokenizer_time_budget'
            else:
                started = time.monotonic()
                try:
                    headers = {'Authorization': 'Bearer ' + self.api_key} if self.api_key else {}
                    response = _tokenizer_client.post(self.url, json=payload, headers=headers,
                                                      timeout=min(self.timeout, remaining))
                    response.raise_for_status()
                    data = response.json()
                    raw, window = data.get('count'), data.get('max_model_len')
                    if type(raw) is not int or raw < (0 if body else 1) or type(window) is not int or window <= 0:
                        raise ValueError('invalid_tokenizer_response')
                    self.model_window = min(self.model_window or window, window)
                    ratio = 1.0 if body else self.ratio
                    count = math.ceil(raw * ratio)
                    self.last = {'count_mode': 'vllm_tokenize_text' if body else 'vllm_tokenize', 'input_tokens_raw': raw,
                                 'token_safety_margin': count - raw, 'token_safety_ratio': ratio,
                                 'tokenizer_model_window': self.model_window, 'tokenizer_fallback': ''}
                    if len(self._cache) >= 64:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[key] = (count, self.last)
                    return count
                except (httpx.HTTPError, ValueError, AttributeError) as exc:
                    reason = self._disabled = (f'http_{exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError)
                                               else type(exc).__name__)
                finally:
                    self._remaining -= time.monotonic() - started
        mode = ('cl100k_text_estimate' if _tok_enc is not None else 'heuristic_text_estimate') if body else REQUEST_COUNT_MODE
        self.last = {'count_mode': mode, 'tokenizer_fallback': reason}
        return fallback()
