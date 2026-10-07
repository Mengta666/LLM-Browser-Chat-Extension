import asyncio
import contextlib
import multiprocessing
import os

from .engine import ParseError


def worker(connection, settings):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(name, None)
    # 上游库可能把输入/配置写到日志。隔离进程仅通过受控协议返回状态。
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        from .engine import PaddleEngine
        engine = PaddleEngine(settings)
        connection.send({"status": "ready"})
        while True:
            payload, page_number = connection.recv()
            try:
                result = engine.parse(payload, page_number)
            except ParseError as exc:
                result = {"status": "error", "error_code": exc.code}
            except Exception as exc:
                result = {"status": "error", "error_code": "pipeline_failed",
                          "error_type": type(exc).__name__}
            connection.send(result)
    except ParseError as exc:
        connection.send({"status": "error", "error_code": exc.code})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        with contextlib.suppress(EOFError, BrokenPipeError):
            connection.send({"status": "error", "error_code": "pipeline_start_failed",
                             "error_type": type(exc).__name__})
    finally:
        connection.close()


class ProcessRunner:
    def __init__(self, settings, target=worker):
        self.settings, self.target = settings, target
        self.process = None
        self.connection = None
        self.ready = False

    def alive(self):
        return bool(self.ready and self.process and self.process.is_alive())

    async def receive(self, disconnected=None):
        while not self.connection.poll():
            if not self.process.is_alive():
                raise ParseError("worker_exited")
            if disconnected and await disconnected():
                raise ParseError("client_disconnected")
            await asyncio.sleep(0.025)
        return await asyncio.to_thread(self.connection.recv)

    async def start(self, disconnected=None):
        if self.alive():
            return
        await self.stop()
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=self.target, args=(child, self.settings), daemon=True)
        try:
            self.process.start()
            child.close()
            async with asyncio.timeout(self.settings.startup_timeout):
                message = await self.receive(disconnected)
            if message.get("status") != "ready":
                raise ParseError(message.get("error_code", "pipeline_start_failed"))
            self.ready = True
        except TimeoutError:
            child.close()
            await self.stop()
            raise ParseError("worker_start_timeout") from None
        except BaseException:
            child.close()
            await self.stop()
            raise

    async def parse(self, content, page_number, disconnected=None):
        try:
            async with asyncio.timeout(self.settings.page_timeout):
                await self.start(disconnected)
                await asyncio.to_thread(self.connection.send, (content, page_number))
                return await self.receive(disconnected)
        except TimeoutError:
            await self.stop()
            raise ParseError("page_timeout") from None
        except (EOFError, OSError):
            await self.stop()
            raise ParseError("worker_exited") from None
        except (ParseError, asyncio.CancelledError):
            await self.stop()
            raise

    async def stop(self):
        self.ready = False
        process, self.process = self.process, None
        if process and process.pid:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 2)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 2)
            process.close()
        if self.connection:
            self.connection.close()
            self.connection = None
