import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def tmp_path():
    """为当前测试创建独立的临时目录。"""
    path = Path(tempfile.gettempdir()) / ("web-reader-test-" + uuid4().hex)
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """为测试安装网络访问守卫，阻止意外访问外部网络。"""
    import socket
    original = socket.socket.connect
    def guarded(sock, address):
        """仅放行标准库创建 socketpair 的内部连接，阻止测试发起其他连接。"""
        caller = sys._getframe(1)
        if caller.f_globals.get('__name__') == 'socket' and caller.f_code.co_name == '_fallback_socketpair':
            return original(sock, address)
        raise AssertionError("Offline test attempted a connection")
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setenv("WEB_READER_API_KEY", "test-only-" + "a" * 40)
