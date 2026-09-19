"""带鉴权、容量控制和可终止任务的正文接口。"""

import asyncio
import hmac
import json
import multiprocessing
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import VERSION
from .reader import worker


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=4096)
    timeout_seconds: float = Field(default=15, ge=1, le=30)


@asynccontextmanager
async def lifespan(application):
    key = os.environ.get("WEB_READER_API_KEY", "").strip()
    if len(key) < 32 or not key.isascii():
        raise RuntimeError("WEB_READER_API_KEY must be at least 32 ASCII characters")
    concurrency = int(os.getenv("READER_CONCURRENCY", "3"))
    max_bytes = int(os.getenv("READER_MAX_BYTES", "2097152"))
    if not 1 <= concurrency <= 16 or not 65536 <= max_bytes <= 8388608:
        raise RuntimeError("Invalid reader resource limits")
    application.state.key = key
    application.state.slots = asyncio.Semaphore(concurrency)
    application.state.max_bytes = max_bytes
    yield


app = FastAPI(title="Browser Agent Web Reader", version=VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "web-reader", "version": VERSION, "api_version": "v1"}


async def run_job(request, args):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(sender, args.url, args.timeout_seconds,
                              request.app.state.max_bytes, 100_000), daemon=True)
    started = False
    try:
        process.start()
        started = True
        sender.close()
        async with asyncio.timeout(args.timeout_seconds):
            while not receiver.poll():
                if not process.is_alive():
                    return {"status": "error", "error_code": "worker_failed"}
                if await request.is_disconnected():
                    return {"status": "error", "error_code": "client_disconnected"}
                await asyncio.sleep(.025)
            return await asyncio.to_thread(receiver.recv)
    except TimeoutError:
        return {"status": "error", "error_code": "fetch_timeout"}
    except (EOFError, OSError):
        return {"status": "error", "error_code": "worker_failed"}
    finally:
        # 超时和断开必须终止实际下载/解析进程，不仅停止等待 Future。
        if started:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 1)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 1)
            process.close()
        receiver.close()
        sender.close()


@app.post("/v1/extract")
async def extract(request: Request):
    authorization = request.headers.get("Authorization", "")
    if not hmac.compare_digest(authorization.encode(), ("Bearer " + request.app.state.key).encode()):
        return JSONResponse({"status": "error", "error_code": "unauthorized"}, status_code=401)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            return JSONResponse({"status": "error", "error_code": "request_too_large"}, status_code=413)
    try:
        args = ExtractRequest.model_validate(json.loads(body))
    except (ValueError, ValidationError):
        return JSONResponse({"status": "error", "error_code": "invalid_request"}, status_code=422)
    try:
        await asyncio.wait_for(request.app.state.slots.acquire(), timeout=.05)
    except TimeoutError:
        return JSONResponse({"status": "error", "error_code": "reader_busy"}, status_code=429)
    try:
        result = await run_job(request, args)
        return JSONResponse(result)
    finally:
        request.app.state.slots.release()
