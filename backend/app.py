"""FastAPI 应用入口，负责挂载当前启用的后端路由。"""

import os
import time

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.agent import router as agent_router
from api.chat import router as chat_router
from api.logs import router as logs_router
from observability.logger import get_logger

_log = get_logger("system")
_start_ts = time.monotonic()

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

# 记忆管理路由
_modules_loaded = ["agent", "chat", "logs"]
_modules_failed: list[str] = []

try:
    from api.memory import router as memory_router
    app.include_router(memory_router)
    _modules_loaded.append("memory")
except Exception as e:
    _modules_failed.append(f"memory: {str(e)[:80]}")

try:
    from api.sessions import router as sessions_router
    app.include_router(sessions_router)
    _modules_loaded.append("sessions")
except Exception as e:
    _modules_failed.append(f"sessions: {str(e)[:80]}")

try:
    from agent.memory import rethink as _rethink
    _rethink.start_rethink_daemon()
    _modules_loaded.append("rethink_daemon")
except Exception as e:
    _modules_failed.append(f"rethink_daemon: {str(e)[:80]}")

_log.info("app_startup", data={
    "pid": os.getpid(),
    "modules_loaded": _modules_loaded,
    "modules_failed": _modules_failed,
})
if _modules_failed:
    _log.warn("app_startup_partial", data={"failed": _modules_failed})


@app.on_event("shutdown")
async def _on_shutdown():
    uptime = int((time.monotonic() - _start_ts))
    _log.info("app_shutdown", data={"uptime_seconds": uptime, "pid": os.getpid()})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
