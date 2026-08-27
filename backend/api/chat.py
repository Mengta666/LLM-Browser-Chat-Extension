"""Chat 后端(OpenAI 兼容)+ 长期记忆注入。

链路:接收前端聊天请求 → 检索长期记忆并注入为一条 system → 转发 OpenAI 兼容模型
→ SSE 流式返回(标准 OpenAI 格式,前端零改)→ 对话结束后台异步抽取写入记忆。

记忆能力复用 agent/memory 子系统(service 门面),全部 try/except 降级:
记忆是"锦上添花",Qdrant/embedding 不可用时 chat 照常对话。

模块拆分(不做旧版 607 行巨函数):
- _extract_last_user_text:取最后一条 user 文本(记忆检索的 query)
- _build_memory_system:检索记忆 → 拼装一条 system 注入消息(空则 None)
- _inject_memory:把 system 注入消息插到 messages 最前
- _schedule_memory_write:后台线程抽取写入(复用 write_chat_memory)
- stream_chat / sync_chat:流式 / 非流式转发,主体不重复
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import APIRouter
from fastapi.responses import StreamingResponse, JSONResponse
from openai import OpenAI
from pydantic import BaseModel

from observability.logger import get_logger

_chat_log = get_logger("chat")

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

MODEL_BASE_URL = os.getenv("MODEL_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CHAT_LLM_TIMEOUT = 120
# 会话标题模型:复用记忆抽取模型(网关实际存在的,避免默认 gpt-4o 在无该模型的网关失败)
CHAT_TITLE_MODEL = os.getenv("MEMORY_MODEL") or os.getenv("AGENT_MODEL") or "gpt-4o"

_llm_client = OpenAI(base_url=MODEL_BASE_URL, api_key=OPENAI_API_KEY)

# 写入去抖:攒 N 轮才抽取一次(对齐 mem0 滚动窗口,降 token 成本)。
# chat_id → 自上次写入以来的累计轮数。有界字典防内存膨胀(仿 loop.py MAX_SESSIONS)。
from agent.memory.config import CHAT_WRITE_EVERY_N_TURNS
_turn_counter: dict[str, int] = {}
_turn_lock = threading.Lock()
_MAX_TRACKED_CHATS = 500

router = APIRouter(prefix="/v1", tags=["Chat 对话"])


class ChatRequest(BaseModel):
    model: str = "gpt-4o"
    messages: list[dict[str, Any]] = []
    stream: bool = True
    chat_id: str = ""          # 前端会话标识(可选,用于日志关联)


# ═══════════════════════════════════════════════════════════════════════════════
# 记忆注入(检索 → 拼 system → 插到最前)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_last_user_text(messages: list[dict[str, Any]]) -> str:
    """取最后一条 user 消息的纯文本(作为记忆检索 query)。

    content 可能是 str,也可能是多模态数组([{type:text,...},{type:image_url,...}]);
    多模态时只取 text 片段拼接。
    """
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            return " ".join(texts).strip()
    return ""


def _build_memory_system(query: str, chat_id: str = "") -> Optional[str]:
    """检索长期记忆 → 拼装成一条 system 注入文本。无记忆/异常返回 None。

    分层(对齐 MemGPT):
    - core 层(persona+preference):常驻全量注入(量小、都重要,不过闸门),全局。
    - episodic 层:仅本会话(chat_id 隔离)、与 query 相关才注入(过双闸门 + 三因子重排)。
    复用 agent/memory service 门面,全程降级。落结构化日志供可观测(命中/注入/拦截)。
    """
    try:
        from agent.memory import service as memory_service
    except Exception:
        return None

    blocks: list[str] = []
    core_n = 0
    ep_n = 0
    ep_hits: list[dict[str, Any]] = []
    # core:常驻(scroll 全量,不依赖 query,全局)
    try:
        core = memory_service.get_core_memories()
        if core:
            core_n = len(core)
            core_block = memory_service.build_core_block(core)
            if core_block:
                blocks.append(core_block)
    except Exception:
        pass

    # episodic:按需(仅本会话,过相关性闸门 + 三因子重排,空则不拼)
    if query:
        try:
            episodic = memory_service.recall_episodic(query, chat_id=chat_id)
            if episodic:
                ep_hits = episodic
                ep_block = memory_service.build_episodic_block(episodic)
                if ep_block:
                    ep_n = len(episodic)
                    blocks.append(ep_block)
        except Exception:
            pass

    # 可观测:记录本轮记忆注入情况(命中 episodic 的 id + 分数、core/episodic 注入数)
    try:
        _chat_log.info("chat_memory_injected", data={
            "chat_id": chat_id,
            "query_head": (query or "")[:60],
            "core_injected": core_n,
            "episodic_injected": ep_n,
            "episodic_hits": [
                {"memory_id": h.get("memory_id", ""),
                 "cosine": round(float(h.get("cosine", 0.0)), 3),
                 "score3": round(float(h.get("_score3", 0.0)), 3)}
                for h in ep_hits[:5]],
        })
    except Exception:
        pass

    if not blocks:
        return None
    return "以下是关于该用户的长期记忆,回答时请参考(若与当前问题无关可忽略):\n\n" + "\n\n".join(blocks)


def _inject_memory(messages: list[dict[str, Any]], chat_id: str = "") -> list[dict[str, Any]]:
    """把记忆注入为一条 system,插到 messages 最前。无记忆则原样返回。"""
    query = _extract_last_user_text(messages)
    mem_system = _build_memory_system(query, chat_id=chat_id)
    if not mem_system:
        return messages
    return [{"role": "system", "content": mem_system}] + list(messages)


# ═══════════════════════════════════════════════════════════════════════════════
# 记忆写入(攒 N 轮批量,后台异步)
# ═══════════════════════════════════════════════════════════════════════════════

def _should_write_now(chat_id: str) -> bool:
    """N 轮去抖:累计到 CHAT_WRITE_EVERY_N_TURNS 轮才返回 True 并清零。

    chat_id 为空(前端未传)时退化为"每轮都写"(无法去抖)。有界字典防膨胀。
    """
    if not chat_id:
        return True
    with _turn_lock:
        # 容量护栏:超上限清空(简单淘汰,计数丢失只是多写一次,无害)
        if len(_turn_counter) > _MAX_TRACKED_CHATS:
            _turn_counter.clear()
        n = _turn_counter.get(chat_id, 0) + 1
        if n >= CHAT_WRITE_EVERY_N_TURNS:
            _turn_counter[chat_id] = 0
            return True
        _turn_counter[chat_id] = n
        return False


def _schedule_memory_write(user_text: str, assistant_text: str, chat_id: str = "") -> None:
    """一轮对话结束后,按 N 轮去抖决定是否触发后台抽取写入。绝不阻塞、绝不抛。

    攒够 N 轮才写(对齐 mem0 滚动窗口降成本);触发时调 write_chat_memory
    抽取 persona/preference/episodic。chat 无站点,不涉及 url/success。
    """
    if not user_text and not assistant_text:
        return
    if not _should_write_now(chat_id):
        return

    def _worker():
        try:
            from agent.memory import service as memory_service
            # 修抽取丢数据 bug:N 轮去抖 + 单对抽取会丢前几轮事实,
            # 用会话历史最近 ~2N 条拼 history_summary 作上下文补回(形参全链路已就绪)。
            history_summary = _recent_history_summary(chat_id)
            result = memory_service.write_chat_memory(
                user_text, assistant_text,
                history_summary=history_summary, chat_id=chat_id)
            _chat_log.info("chat_memory_written", data={
                "chat_id": chat_id,
                "applied": result.get("applied", []),
                "facts": len(result.get("facts", [])),
                "skipped_hash": result.get("skipped_hash", 0)})
        except Exception as exc:
            _chat_log.warn("chat_memory_write_failed",
                           data={"chat_id": chat_id, "error": str(exc)[:160]})

    try:
        threading.Thread(target=_worker, daemon=True, name=f"chat-mem-{chat_id or 'x'}").start()
    except Exception:
        pass


def _recent_history_summary(chat_id: str, max_pairs: int = 2 * CHAT_WRITE_EVERY_N_TURNS) -> str:
    """取本会话最近若干条消息拼成抽取上下文(缓解 N 轮去抖 + 单对抽取丢前几轮事实)。

    从 chat_store 读(会话历史已按 chat_id 落库);失败或空返回空串(退化为无上下文抽取)。
    只作上下文,不是抽取目标——抽取仍聚焦最新一对(见 build_chat_extract_user_prompt)。
    """
    if not chat_id:
        return ""
    try:
        from storage import chat_store
        msgs = chat_store.get_messages(chat_id)
        if not msgs:
            return ""
        recent = msgs[-max_pairs * 2:] if max_pairs > 0 else msgs
        lines = []
        for m in recent:
            role = "用户" if m.get("role") == "user" else "助手"
            content = str(m.get("content", "")).strip()
            if content:
                lines.append(f"{role}:{content[:200]}")
        return "\n".join(lines)
    except Exception:
        return ""


def _generate_title(user_text: str, assistant_text: str) -> str:
    """用 LLM 给会话起个简短标题(6-12 字)。失败返回空串(调用方降级为截断标题)。"""
    try:
        snippet = f"用户:{user_text[:200]}\n助手:{assistant_text[:200]}"
        resp = _llm_client.chat.completions.create(
            model=CHAT_TITLE_MODEL,
            messages=[
                {"role": "system", "content": "根据对话给一个简短标题,6-12个字,概括主题。只输出标题本身,不要引号、不要标点、不要解释。"},
                {"role": "user", "content": snippet},
            ],
            timeout=30,
        )
        title = (resp.choices[0].message.content or "").strip().strip('"「」')
        return title[:30]
    except Exception:
        return ""


def _save_history(chat_id: str, user_text: str, assistant_text: str) -> None:
    """把这一轮对话存进会话历史(供会话列表 + 续谈)。绝不阻塞、绝不抛。

    与记忆写入不同:历史**每轮都存**(续谈需完整对话,不去抖)。chat_id 空则跳过
    (无法归属会话)。存储失败静默——历史是附加能力,不能拖垮 chat。
    会话首次创建时,后台用 LLM 起一个语义标题(失败则保留截断标题)。
    """
    if not chat_id:
        return

    def _worker():
        try:
            from storage import chat_store
            is_new = chat_store.ensure_session(chat_id, first_user_text=user_text)
            if user_text.strip():
                chat_store.add_message(chat_id, "user", user_text)
            if assistant_text.strip():
                chat_store.add_message(chat_id, "assistant", assistant_text)
            # 新会话:LLM 起语义标题(截断标题作兜底,已由 ensure_session 落好)
            if is_new:
                title = _generate_title(user_text, assistant_text)
                if title:
                    chat_store.set_title(chat_id, title)
        except Exception as exc:
            _chat_log.warn("chat_history_save_failed",
                           data={"chat_id": chat_id, "error": str(exc)[:160]})

    try:
        threading.Thread(target=_worker, daemon=True, name=f"chat-hist-{chat_id or 'x'}").start()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# 转发(流式 / 非流式)
# ═══════════════════════════════════════════════════════════════════════════════

def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_chat(model: str, messages: list[dict[str, Any]],
                user_text: str, chat_id: str) -> StreamingResponse:
    """SSE 流式转发:标准 OpenAI 格式逐块吐 + [DONE];累积全文,结束后台写入记忆。"""
    def _gen():
        full_text = ""
        try:
            resp = _llm_client.chat.completions.create(
                model=model, messages=messages, stream=True, timeout=CHAT_LLM_TIMEOUT)
            for chunk in resp:
                full_text += (chunk.choices[0].delta.content or "") if chunk.choices else ""
                yield _sse(chunk.model_dump())
        except Exception as exc:
            _chat_log.error("chat_stream_failed", data={"chat_id": chat_id, "error": str(exc)[:200]})
            yield _sse({"choices": [{"delta": {"content": f"\n[对话出错: {str(exc)[:120]}]"},
                                     "finish_reason": "error", "index": 0}]})
        finally:
            yield "data: [DONE]\n\n"
            if full_text.strip():
                _save_history(chat_id, user_text, full_text)
                _schedule_memory_write(user_text, full_text, chat_id)

    return StreamingResponse(_gen(), media_type="text/event-stream")


def sync_chat(model: str, messages: list[dict[str, Any]],
              user_text: str, chat_id: str) -> JSONResponse:
    """非流式转发:一次拿全,返回标准 OpenAI JSON;结束后台写入记忆。"""
    try:
        resp = _llm_client.chat.completions.create(
            model=model, messages=messages, stream=False, timeout=CHAT_LLM_TIMEOUT)
        payload = resp.model_dump()
        text = ""
        try:
            text = resp.choices[0].message.content or ""
        except Exception:
            pass
        if text.strip():
            _save_history(chat_id, user_text, text)
            _schedule_memory_write(user_text, text, chat_id)
        return JSONResponse(payload)
    except Exception as exc:
        _chat_log.error("chat_sync_failed", data={"chat_id": chat_id, "error": str(exc)[:200]})
        return JSONResponse(status_code=502, content={"error": f"对话出错: {str(exc)[:160]}"})


# ═══════════════════════════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/chat/completions")
def chat_completions(item: ChatRequest):
    """OpenAI 兼容对话端点 + 长期记忆注入。stream 由请求体决定。"""
    messages = _inject_memory(item.messages, chat_id=item.chat_id)
    user_text = _extract_last_user_text(item.messages)
    _chat_log.info("chat_request", data={
        "chat_id": item.chat_id, "model": item.model, "stream": item.stream,
        "msg_count": len(item.messages), "injected": len(messages) > len(item.messages)})

    if item.stream:
        return stream_chat(item.model, messages, user_text, item.chat_id)
    return sync_chat(item.model, messages, user_text, item.chat_id)
