"""页面相关 API 模块。

当前已提供“立即刷新当前页面索引”的接口。页面知识库管理等能力后续再扩展。
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from tools.page_retrieval import index_or_reuse_page


router = APIRouter(prefix="/api/pages", tags=["页面 API"])


class CurrentPage(BaseModel):
    """前端传入的当前页面快照。"""

    url: str
    title: str = ""
    content: str
    selected_text: str = ""


class RefreshSnapshotRequest(BaseModel):
    """立即刷新页面索引的请求体。"""

    chat_id: str
    page_context_id: str = ""
    current_page: CurrentPage
    force_refresh: bool = True


@router.post("/refresh_snapshot")
def refresh_snapshot(item: RefreshSnapshotRequest) -> dict[str, Any]:
    """立即刷新当前 URL/page 的最新 snapshot 索引。

    endpoint 语义固定为刷新索引，所以内部始终按 force_refresh=True 执行；
    请求体里的 force_refresh 只保留给前端/调试侧表达调用意图。
    """
    if not item.chat_id.strip():
        raise HTTPException(status_code=400, detail="chat_id is required")
    if not item.current_page.url.strip():
        raise HTTPException(status_code=400, detail="current_page.url is required")
    if not item.current_page.content.strip():
        raise HTTPException(status_code=400, detail="current_page.content is required")

    try:
        return index_or_reuse_page(
            chat_id=item.chat_id.strip(),
            page_context_id=item.page_context_id,
            current_page=item.current_page,
            force_refresh=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"refresh_snapshot error: {exc}") from exc
