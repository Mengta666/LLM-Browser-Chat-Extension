import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def tmp_path():
    path = Path(tempfile.gettempdir()) / ("web-reader-test-" + uuid4().hex)
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    import socket
    original = socket.socket.connect
    def guarded(sock, address):
        caller = sys._getframe(1)
        if caller.f_globals.get('__name__') == 'socket' and caller.f_code.co_name == '_fallback_socketpair':
            return original(sock, address)
        raise AssertionError("Offline test attempted a connection")
    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setenv("WEB_READER_API_KEY", "test-only-" + "a" * 40)
