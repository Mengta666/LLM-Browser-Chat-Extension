"""FastAPI 应用入口，负责挂载当前启用的后端路由。"""

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from api.chat import model_chat_route
from api.chats import router as chats_router
from api.memory import router as memory_router
from api.plans import router as plans_router
from api.pages import router as pages_router
from api.search import router as search_router
from storage.db import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时确保本地 SQLite 表已创建，便于新环境直接运行。"""
    db.init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.include_router(model_chat_route)
# 当前后端挂载聊天、历史、记忆、计划、页面刷新和联网搜索路由。
app.include_router(model_chat_route)
app.include_router(chats_router)
app.include_router(memory_router)
app.include_router(plans_router)
app.include_router(pages_router)
app.include_router(search_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
