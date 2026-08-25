"""FastAPI 应用入口，负责挂载当前启用的后端路由。"""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.agent import router as agent_router
from api.chat import router as chat_router
from api.logs import router as logs_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router)
app.include_router(chat_router)
app.include_router(logs_router)

# 记忆管理路由(用户 CRUD):记忆子系统依赖 qdrant-client,缺失时跳过挂载,不影响 agent/chat
try:
    from api.memory import router as memory_router
    app.include_router(memory_router)
except Exception:
    pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
