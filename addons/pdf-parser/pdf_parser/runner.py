import asyncio
import contextlib
import multiprocessing
import os

from .engine import ParseError


def worker(connection, settings):
    """在隔离子进程中初始化解析引擎，通过管道接收页面并回传受控结果。"""
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
        """保存解析配置并初始化子进程及通信管道状态。"""
        self.settings, self.target = settings, target
        self.process = None
        self.connection = None
        self.ready = False

    def alive(self):
        """判断当前解析子进程是否仍在运行。"""
        return bool(self.ready and self.process and self.process.is_alive())

    async def receive(self, disconnected=None):
        """异步等待子进程回复，同时检查截止时间、进程退出和客户端断开。"""
        while not self.connection.poll():
            if not self.process.is_alive():
                raise ParseError("worker_exited")
            if disconnected and await disconnected():
                raise ParseError("client_disconnected")
            await asyncio.sleep(0.025)
        return await asyncio.to_thread(self.connection.recv)

    async def start(self, disconnected=None):
        """启动解析子进程，并在启动超时前等待就绪握手。"""
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
        """提交单页解析任务；遇到超时、取消或异常时停止当前子进程。"""
        try:
            async with asyncio.timeout(self.settings.page_timeout):
                await self.start(disconnected)
                await asyncio.to_thread(self.connection.send, (content, page_number))
                return await self.receive(disconnected)
        except TimeoutError:
            # 超时后终止工作进程并关闭管道，避免迟到的本页结果被下一页请求接收。
            await self.stop()
            raise ParseError("page_timeout") from None
        except (EOFError, OSError):
            await self.stop()
            raise ParseError("worker_exited") from None
        except (ParseError, asyncio.CancelledError):
            await self.stop()
            raise

    async def stop(self):
        """关闭通信管道并终止解析子进程，必要时强制结束未退出的进程。"""
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
