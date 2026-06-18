"""FastAPI 应用入口，负责挂载当前启用的后端路由。"""

import uvicorn
from fastapi import FastAPI

from api.chat import model_chat_route
from api.pages import router as pages_router
from api.search import router as search_router


app = FastAPI()
# 当前后端挂载聊天、页面刷新和联网搜索三个已启用路由；memory 仍是预留模块。
app.include_router(model_chat_route)
app.include_router(pages_router)
app.include_router(search_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
