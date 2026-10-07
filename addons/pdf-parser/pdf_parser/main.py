import asyncio
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import SCHEMA_VERSION, VERSION
from .config import Settings
from .engine import ParseError, check_vlm
from .runner import ProcessRunner

log = logging.getLogger("pdf-parser")


def error(code, status):
    return JSONResponse({"status": "error", "error_code": code}, status_code=status)


def create_app(settings=None, runner_factory=ProcessRunner, check_upstream=check_vlm):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or Settings.from_env()
        app.state.slot = asyncio.Semaphore(1)
        app.state.runner = runner_factory(app.state.settings)
        try:
            await app.state.runner.start()
        except Exception as exc:
            code = exc.code if isinstance(exc, ParseError) else type(exc).__name__
            raise RuntimeError(f"Parser initialization failed: {code}") from None
        try:
            yield
        finally:
            await app.state.runner.stop()

    app = FastAPI(title="PDF Page Parser", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(request):
        return hmac.compare_digest(request.headers.get("authorization", "").encode(),
                                   ("Bearer " + request.app.state.settings.api_key).encode())

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "pdf-parser", "version": VERSION,
                "schema_version": SCHEMA_VERSION, "worker_ready": app.state.runner.alive()}

    @app.get("/ready")
    async def ready(request: Request):
        if not authorized(request):
            return error("unauthorized", 401)
        if not app.state.runner.alive():
            return error("worker_not_ready", 503)
        try:
            await asyncio.to_thread(check_upstream, app.state.settings)
        except ParseError as exc:
            return error(exc.code, 503)
        return {"status": "ready", "service": "pdf-parser", "version": VERSION,
                "schema_version": SCHEMA_VERSION, "pipeline": "PaddleOCR-VL-1.6",
                "layout_model": "PP-DocLayoutV3", "vlm_model": app.state.settings.vlm_model,
                "inference_verified": False}

    @app.post("/v1/parse/page")
    async def parse_page(request: Request):
        if not authorized(request):
            return error("unauthorized", 401)
        if request.headers.get("content-type", "").split(";", 1)[0] not in ("image/png", "image/jpeg"):
            return error("unsupported_media_type", 415)
        try:
            if set(request.query_params) - {"page_number"} or len(request.query_params.getlist("page_number")) > 1:
                raise ValueError
            page_number = int(request.query_params.get("page_number", "1"))
            if not 1 <= page_number <= 1_000_000:
                raise ValueError
            declared = request.headers.get("content-length")
            if declared is not None:
                if int(declared) < 0:
                    raise ValueError
                if int(declared) > app.state.settings.max_bytes:
                    return error("request_too_large", 413)
        except ValueError:
            return error("invalid_request", 422)
        try:
            await asyncio.wait_for(app.state.slot.acquire(), 0.05)
        except TimeoutError:
            return error("parser_busy", 429)
        started, request_id = time.monotonic(), uuid.uuid4().hex
        try:
            body = bytearray()
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > app.state.settings.max_bytes:
                        return error("request_too_large", 413)
            if not body:
                return error("empty_image", 422)
            result = await app.state.runner.parse(bytes(body), page_number, request.is_disconnected)
            result["request_id"] = request_id
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            code = result.get("error_code")
            status = 200 if result.get("status") == "ok" else (422 if code in {
                "invalid_image", "unsupported_image", "image_too_large", "image_orientation_not_normalized",
            } else 502)
            log.info("page_complete request_id=%s page=%s status=%s elapsed_ms=%s",
                     request_id, page_number, result["status"], result["elapsed_ms"])
            return JSONResponse(result, status_code=status)
        except ParseError as exc:
            return error(exc.code, 504 if exc.code == "page_timeout" else 503)
        except TimeoutError:
            return error("upload_timeout", 408)
        except (EOFError, OSError):
            return error("worker_unavailable", 503)
        finally:
            app.state.slot.release()

    return app


app = create_app()
