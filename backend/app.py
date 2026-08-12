"""FastAPI 应用入口，负责挂载当前启用的后端路由。"""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.agent import router as agent_router
from api.logs import router as logs_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    from api.chat import model_chat_route
    from api.pages import router as pages_router
    from api.search import router as search_router
    app.include_router(model_chat_route)
    app.include_router(pages_router)
    app.include_router(search_router)
except Exception as e:
    print(f"[WARN] 聊天/搜索/页面模块加载失败（缺少依赖配置），已跳过: {e}")

app.include_router(agent_router)
app.include_router(logs_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
