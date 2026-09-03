"""记忆管理 API(用户可见/可编辑/可删,对齐 ChatGPT/Claude 的记忆控制)。

针对 chat 记忆(CHAT_USER_ID 命名空间):列出、看单条、改内容、删(默认软删可回溯)、
手动新增。删除默认走时间失效(invalidate,可回溯);hard=true 才物理删。

所有写操作在 Qdrant 不可用时返回 503,不影响 chat 主流程(chat 侧自身降级)。

批次 E · P2:新增 POST /v1/memory/rethink SSE 端点——LLM 全库扫 core 判 conflicts/expired/merges。
三触发共用一把并发锁(rethink.try_acquire),API 端点被前端狂点 → 拒绝重入。
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.memory import vector as V
from agent.memory import rethink as R
from agent.memory.config import (
    CHAT_USER_ID, SCOPE_GLOBAL,
    MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC,
    SUBJECT_VOCAB,
)
from rag.embedder import embed_text
from observability.logger import get_logger

_mem_log = get_logger("memory_api")

router = APIRouter(prefix="/v1/memory", tags=["记忆管理"])

_CHAT_TYPES = {MEMORY_TYPE_CORE, MEMORY_TYPE_EPISODIC}
# 手动可新建的类型:只允许全局类 core。
# episodic 是系统从对话自动抽的会话内事件,手动造会成"全局 episodic"(任何会话都召不回)死数据。
_CREATABLE_TYPES = {MEMORY_TYPE_CORE}

# ── subject 推断 LLM 客户端(复用 MEMORY_MODEL)──────────────────────
_env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=_env_path)

def _infer_subject(content: str, vocab: list[str]) -> str:
    """用 LLM 从 content 推断 subject 短语,优先复用 vocab 中已有的短语。

    失败时返回空串(不阻塞写入)。
    """
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=os.getenv("MODEL_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        )
        model = os.getenv("MEMORY_MODEL") or os.getenv("AGENT_MODEL") or "gpt-4o"
        vocab_hint = f"\n已有短语(优先从中选一个):{', '.join(vocab[:20])}" if vocab else ""
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content":
                    "你是记忆管理器。给出一条记忆的「主题短语」:中文,≤16字,标准化表达。"
                    "如「回答语言偏好」「编程语言」「用户身份」「项目:订单迁移」。"
                    "若已有短语中有合适的,直接复用那个短语。"
                    "只输出短语本身,不加任何解释。"},
                {"role": "user", "content": f"记忆内容:{content}{vocab_hint}"},
            ],
            timeout=10,
        )
        raw = (resp.choices[0].message.content or "").strip().strip("「」【】“”‘’\"'").strip()
        return raw[:64]
    except Exception:
        return ""


class MemoryCreate(BaseModel):
    content: str
    memory_type: str = MEMORY_TYPE_CORE
    subject: str = ""   # 留空时后端自动推断


class MemoryPatch(BaseModel):
    content: str


def _view(payload: dict[str, Any]) -> dict[str, Any]:
    """裁剪成前端友好的记忆视图(不暴露向量/hash 等内部字段)。

    批次 E · P3:白名单扩 subject / superseded_by / expires_at,供前端"按主题分组"、
    "已被替代灰化"、"到期时间提示"三种展示。
    """
    return {
        "memory_id": payload.get("memory_id", ""),
        "content": payload.get("content", ""),
        "memory_type": payload.get("memory_type", ""),
        "created_at": payload.get("created_at", ""),
        "updated_at": payload.get("updated_at", ""),
        "valid": payload.get("valid", True),
        "invalid_at": payload.get("invalid_at", ""),
        # 批次 E · P3 新加
        "subject": payload.get("subject", ""),
        "superseded_by": payload.get("superseded_by", ""),
        "expires_at": payload.get("expires_at", ""),
    }


@router.get("/list")
def list_memories(memory_type: Optional[str] = Query(None),
                  include_invalid: bool = Query(False),
                  include_episodic: bool = Query(False)) -> dict[str, Any]:
    """列出 chat 记忆。内存缓存:写操作主动 invalidate,命中 <1ms;TTL 5 分钟兜底。"""
    from agent.memory import list_cache
    key = list_cache.cache_key(memory_type, include_invalid, include_episodic)
    cached = list_cache.get(key)
    if cached is not None:
        return cached
    try:
        items = V.scroll_memories(
            user_id=CHAT_USER_ID, memory_type=memory_type, scope=SCOPE_GLOBAL,
            limit=1000, include_invalid=include_invalid)
    except Exception as exc:
        raise HTTPException(503, f"记忆库不可用: {str(exc)[:160]}")
    if not include_episodic and memory_type is None:
        items = [m for m in items if m.get("memory_type") == MEMORY_TYPE_CORE]
    views = [_view(m) for m in items]
    views.sort(key=lambda m: str(m.get("created_at", "")), reverse=True)
    result = {"memories": views, "count": len(views)}
    list_cache.set(key, result)
    return result


@router.get("/{memory_id}")
def get_memory(memory_id: str) -> dict[str, Any]:
    try:
        payload = V.get_memory(memory_id)
    except Exception as exc:
        raise HTTPException(503, f"记忆库不可用: {str(exc)[:160]}")
    if payload is None:
        raise HTTPException(404, f"记忆 {memory_id} 不存在")
    return _view(payload)


@router.post("")
def create_memory(item: MemoryCreate) -> dict[str, Any]:
    """用户手动新增一条记忆(对齐 ChatGPT『记住…』)。只允许全局类 core。"""
    content = item.content.strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    if item.memory_type not in _CREATABLE_TYPES:
        raise HTTPException(
            400, "episodic 由系统从对话自动生成,不支持手动新增;手动记忆请用 core")
    mtype = item.memory_type

    # subject:用户填了就用,没填则调 LLM 推断(复用现有 vocab 保证短语收敛)
    subject = item.subject.strip()[:64]
    if not subject:
        subject = _infer_subject(content, SUBJECT_VOCAB)

    try:
        payload = V.insert_memory(
            content, vector=embed_text(content),
            memory_type=mtype, scope=SCOPE_GLOBAL, domain="",
            user_id=CHAT_USER_ID, confidence=0.7, verified=True,
            subject=subject,
        )
    except Exception as exc:
        raise HTTPException(503, f"写入失败: {str(exc)[:160]}")
    _mem_log.info("memory_created", data={
        "memory_id": payload.get("memory_id"), "type": mtype,
        "subject": subject, "subject_inferred": not item.subject.strip(),
    })
    return _view(payload)


@router.patch("/{memory_id}")
def patch_memory(memory_id: str, item: MemoryPatch) -> dict[str, Any]:
    """编辑记忆正文(内部重算 embedding)。"""
    content = item.content.strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    try:
        updated = V.update_memory(memory_id, content, vector=embed_text(content))
    except Exception as exc:
        raise HTTPException(503, f"更新失败: {str(exc)[:160]}")
    if updated is None:
        raise HTTPException(404, f"记忆 {memory_id} 不存在")
    _mem_log.info("memory_patched", data={"memory_id": memory_id})
    return _view(updated)


@router.delete("/{memory_id}")
def delete_memory(memory_id: str, hard: bool = Query(False)) -> dict[str, Any]:
    """删除记忆:默认软删(失效,可回溯);hard=true 物理删除。"""
    try:
        if hard:
            V.delete_memory(memory_id)
            _mem_log.info("memory_deleted", data={"memory_id": memory_id, "hard": True})
            return {"memory_id": memory_id, "deleted": True, "hard": True}
        result = V.invalidate_memory(memory_id)
        if result is None:
            raise HTTPException(404, f"记忆 {memory_id} 不存在")
        _mem_log.info("memory_invalidated", data={"memory_id": memory_id})
        return {"memory_id": memory_id, "invalidated": True, "hard": False}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"删除失败: {str(exc)[:160]}")


# ═══════════════════════════════════════════════════════════════════════════════
# 批次 E · P2 · rethink 一键整理 SSE 端点
# ═══════════════════════════════════════════════════════════════════════════════

def _sse_event(event: str, data: dict[str, Any]) -> str:
    """SSE 事件格式化:event 名 + JSON data。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/rethink")
def rethink_memory() -> StreamingResponse:
    """一键整理 core 记忆(SSE 流式)——LLM 全库扫 → 判 conflicts/expired/merges → 落库。

    进行中拒绝重入:同一 user 已有 rethink 在跑时,SSE 首事件返 error {"code":"in_progress"}
    并断连。三触发(API/daemon/写后)共用 rethink.try_acquire 同一把锁。

    事件序列:
      event: start        {"total_core": N}
      event: scanning     {"progress": "..."}
      event: llm_call     {"progress": "..."}
      event: applied      {"kind": "conflicts"|"expired"|"merges", ...细节}
      event: done         {"conflicts": n, "expired": m, "merges": k, "elapsed_ms": t, "total_core": N}
      data: [DONE]

    错误路径:
      event: error        {"code": "in_progress", "started_at", "elapsed_ms", "message"}
      event: error        {"code": "scan_failed"|"llm_failed", "message"}
      data: [DONE]
    """
    user_id = CHAT_USER_ID

    # 先试着获取锁——获取成功则占位,失败(已在进行中)则 SSE 返 error
    existing = R.try_acquire(user_id)

    def _gen():
        try:
            if existing:
                # 已在进行中,首事件报错
                yield _sse_event("error", {
                    "code": "in_progress",
                    "started_at": existing["started_at"],
                    "elapsed_ms": existing["elapsed_ms"],
                    "message": f"整理已在进行中,已耗时 {existing['elapsed_ms'] // 1000} 秒,请稍候",
                })
                yield "data: [DONE]\n\n"
                _mem_log.info("rethink_rejected", data={
                    "reason": "in_progress",
                    "elapsed_ms": existing["elapsed_ms"],
                })
                return

            _mem_log.info("rethink_started", data={"user_id": user_id})
            try:
                for ev in R.rethink_core_stream(user_id=user_id):
                    yield _sse_event(ev["event"], ev.get("data", {}))
            finally:
                # 只有真正获取到锁才 release(existing 分支已 return)
                R.release(user_id)
                _mem_log.info("rethink_released", data={"user_id": user_id})
            yield "data: [DONE]\n\n"
        except Exception as exc:
            # 兜底:异常也要 [DONE] 让前端 EventSource 关连接
            try:
                yield _sse_event("error", {"code": "exception", "message": str(exc)[:200]})
                yield "data: [DONE]\n\n"
            except Exception:
                pass

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get("/rethink/status")
def rethink_status() -> dict[str, Any]:
    """查询当前 rethink 是否在进行中(不获取锁)。前端 modal 打开时可先查此接口。"""
    info = R.is_in_progress(CHAT_USER_ID)
    if info:
        return {"in_progress": True, **info}
    return {"in_progress": False}
