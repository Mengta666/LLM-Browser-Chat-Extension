"""Chat 后端(OpenAI 兼容)+ 长期记忆注入 + 联网搜索。

链路:接收前端聊天请求 → 检索长期记忆并注入为一条 system → 转发 OpenAI 兼容模型
→ SSE 流式返回(标准 OpenAI 格式,前端零改)→ 对话结束后台异步抽取写入记忆。

联网搜索(批次 F):
- 自动搜索:SEARCH_ENABLED 时第一次 LLM 调用带 tools=[web_search],LLM 自主判断
  是否搜索;返回 tool_call → 调 SearXNG → 二次 LLM 调用带搜索结果生成回答
- 手动搜索:前端点"🔍"按钮 → ChatRequest.search_query 非空 → 直接搜索注入 system
- 降级:模型不支持 tools 时 catch 400/422 → 去 tools 重试

记忆能力复用 agent/memory 子系统(service 门面),全部 try/except 降级:
记忆是"锦上添花",Qdrant/embedding 不可用时 chat 照常对话。
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
    search_query: str = ""     # 手动搜索:非空时直接搜→注入→LLM(不走 function calling)


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

    只注入 core(全局常驻):用户身份/偏好等跨会话稳定事实。
    episodic 不注入——当前会话已有完整对话历史(chatMessages),
    episodic 注入是重复信息;episodic 只用于 promote 积累证据。
    """
    try:
        from agent.memory import service as memory_service
    except Exception:
        return None

    blocks: list[str] = []
    core_n = 0
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

    # 可观测
    try:
        _chat_log.info("chat_memory_injected", data={
            "chat_id": chat_id,
            "query_head": (query or "")[:60],
            "core_injected": core_n,
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
    抽取 core/episodic。chat 无站点,不涉及 url/success。
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
# 联网搜索(批次 F)
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from search.tools import (
        WEB_SEARCH_TOOL, handle_tool_call, format_search_results_for_manual,
    )
    from search import search_web, SEARCH_ENABLED as _SEARCH_ON
except ImportError:
    _SEARCH_ON = False
    WEB_SEARCH_TOOL = None


def _is_tools_unsupported(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in ("400", "422", "tool", "unsupported", "not support"))


def _sse_search_meta(results) -> str:
    """SSE 自定义 chunk:搜索结果元数据,供前端渲染引用面板。"""
    meta = []
    for i, r in enumerate(results):
        meta.append({"index": i + 1, "title": r.title, "url": r.url, "snippet": (r.snippet or "")[:200]})
    return _sse({
        "choices": [{"delta": {"content": ""}, "finish_reason": None, "index": 0}],
        "search_results": meta,
        "object": "chat.completion.chunk",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# 转发(流式 / 非流式)
# ═══════════════════════════════════════════════════════════════════════════════

def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_chat(model: str, messages: list[dict[str, Any]],
                user_text: str, chat_id: str,
                use_tools: bool = False,
                search_results: list = None) -> StreamingResponse:
    """SSE 流式转发。use_tools=True 时走 function calling 链路。"""

    def _gen():
        full_text = ""
        _sr = search_results or []

        # 手动搜索时先 yield 搜索元数据
        if _sr:
            yield _sse_search_meta(_sr)

        create_kwargs = dict(model=model, messages=messages, stream=True,
                             timeout=CHAT_LLM_TIMEOUT)
        if use_tools and WEB_SEARCH_TOOL:
            create_kwargs["tools"] = [WEB_SEARCH_TOOL]

        try:
            resp = _llm_client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            if use_tools and _is_tools_unsupported(exc):
                _chat_log.warn("tools_unsupported_fallback", data={
                    "chat_id": chat_id, "error": str(exc)[:120]})
                create_kwargs.pop("tools", None)
                resp = _llm_client.chat.completions.create(**create_kwargs)
            else:
                _chat_log.error("chat_stream_failed", data={
                    "chat_id": chat_id, "error": str(exc)[:200]})
                yield _sse({"choices": [{"delta": {"content": f"\n[对话出错: {str(exc)[:120]}]"},
                                         "finish_reason": "error", "index": 0}]})
                yield "data: [DONE]\n\n"
                return

        tool_calls_buf = {}

        try:
            for chunk in resp:
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue
                delta = choice.delta

                if choice.finish_reason == "tool_calls":
                    break

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buf:
                            tool_calls_buf[idx] = {"id": tc.id or "", "name": "", "arguments": ""}
                        if tc.function:
                            if tc.function.name:
                                tool_calls_buf[idx]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_buf[idx]["arguments"] += tc.function.arguments
                    continue

                if delta and delta.content:
                    full_text += delta.content
                    yield _sse(chunk.model_dump())
        except Exception as exc:
            _chat_log.error("chat_stream_failed", data={"chat_id": chat_id, "error": str(exc)[:200]})
            yield _sse({"choices": [{"delta": {"content": f"\n[对话出错: {str(exc)[:120]}]"},
                                     "finish_reason": "error", "index": 0}]})

        # ── 有 tool_call → 执行搜索 → 二次 LLM ──
        if tool_calls_buf:
            for tc_info in tool_calls_buf.values():
                if tc_info["name"] != "web_search":
                    continue
                query = ""
                try:
                    query = json.loads(tc_info["arguments"]).get("query", "")
                except Exception:
                    pass
                yield _sse({"choices": [{"delta": {"content": f"\n🔍 正在搜索: {query}\n"},
                                         "finish_reason": None, "index": 0}],
                            "object": "chat.completion.chunk"})

                tool_result_text, _sr = handle_tool_call(tc_info["name"], tc_info["arguments"])
                if _sr:
                    yield _sse_search_meta(_sr)

                assistant_msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tc_info["id"],
                        "type": "function",
                        "function": {
                            "name": tc_info["name"],
                            "arguments": tc_info["arguments"],
                        }
                    }]
                }
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc_info["id"],
                    "content": tool_result_text,
                }
                messages_2 = list(messages) + [assistant_msg, tool_msg]

                try:
                    resp2 = _llm_client.chat.completions.create(
                        model=model, messages=messages_2, stream=True,
                        timeout=CHAT_LLM_TIMEOUT)
                    for chunk2 in resp2:
                        if chunk2.choices and chunk2.choices[0].delta:
                            c = chunk2.choices[0].delta.content or ""
                            if c:
                                full_text += c
                        yield _sse(chunk2.model_dump())
                except Exception as exc:
                    yield _sse({"choices": [{"delta": {"content":
                        f"\n[搜索后生成回答失败: {str(exc)[:100]}]"},
                        "finish_reason": "error", "index": 0}]})
                break

        yield "data: [DONE]\n\n"
        if full_text.strip():
            _save_history(chat_id, user_text, full_text)
            _schedule_memory_write(user_text, full_text, chat_id)

    return StreamingResponse(_gen(), media_type="text/event-stream")


def sync_chat(model: str, messages: list[dict[str, Any]],
              user_text: str, chat_id: str,
              use_tools: bool = False) -> JSONResponse:
    """非流式转发。use_tools=True 时支持 function calling。"""
    create_kwargs = dict(model=model, messages=messages, stream=False,
                         timeout=CHAT_LLM_TIMEOUT)
    if use_tools and WEB_SEARCH_TOOL:
        create_kwargs["tools"] = [WEB_SEARCH_TOOL]
    try:
        resp = _llm_client.chat.completions.create(**create_kwargs)
    except Exception as exc:
        if use_tools and _is_tools_unsupported(exc):
            create_kwargs.pop("tools", None)
            resp = _llm_client.chat.completions.create(**create_kwargs)
        else:
            _chat_log.error("chat_sync_failed", data={"chat_id": chat_id, "error": str(exc)[:200]})
            return JSONResponse(status_code=502, content={"error": f"对话出错: {str(exc)[:160]}"})

    msg = resp.choices[0].message if resp.choices else None
    if msg and msg.tool_calls:
        tc = msg.tool_calls[0]
        if tc.function.name == "web_search":
            tool_result_text, _ = handle_tool_call(tc.function.name, tc.function.arguments)
            messages_2 = list(messages) + [
                msg.model_dump(),
                {"role": "tool", "tool_call_id": tc.id, "content": tool_result_text},
            ]
            try:
                resp = _llm_client.chat.completions.create(
                    model=model, messages=messages_2, stream=False,
                    timeout=CHAT_LLM_TIMEOUT)
            except Exception as exc:
                _chat_log.error("chat_sync_search_failed", data={
                    "chat_id": chat_id, "error": str(exc)[:200]})
                return JSONResponse(status_code=502, content={
                    "error": f"搜索后生成回答失败: {str(exc)[:160]}"})

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


# ═══════════════════════════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/chat/completions")
def chat_completions(item: ChatRequest):
    """OpenAI 兼容对话端点 + 长期记忆注入 + 联网搜索。"""
    messages = _inject_memory(item.messages, chat_id=item.chat_id)
    user_text = _extract_last_user_text(item.messages)
    _chat_log.info("chat_request", data={
        "chat_id": item.chat_id, "model": item.model, "stream": item.stream,
        "msg_count": len(item.messages), "injected": len(messages) > len(item.messages),
        "search_query": item.search_query[:60] if item.search_query else ""})

    # 手动搜索:search_query 非空 → 直接搜索注入 system → LLM(不走 tools)
    if item.search_query.strip():
        results = search_web(item.search_query.strip()) if _SEARCH_ON else []
        if not results:
            from search.searxng import search_searxng
            results = search_searxng(item.search_query.strip())
        search_system = format_search_results_for_manual(results)
        messages = [{"role": "system", "content": search_system}] + list(messages)
        if item.stream:
            return stream_chat(item.model, messages, user_text, item.chat_id,
                               search_results=results)
        return sync_chat(item.model, messages, user_text, item.chat_id)

    # 自动搜索:SEARCH_ENABLED 时带 tools
    if item.stream:
        return stream_chat(item.model, messages, user_text, item.chat_id,
                           use_tools=_SEARCH_ON)
    return sync_chat(item.model, messages, user_text, item.chat_id,
                     use_tools=_SEARCH_ON)
