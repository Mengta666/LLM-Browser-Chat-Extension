import asyncio
import importlib.util
import multiprocessing
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app import main


AUTH = {"Authorization": "Bearer test-only-" + "a" * 40}


def slow_worker(*args):
    time.sleep(30)


def test_health_does_not_need_network_or_key():
    with TestClient(main.app) as client:
        response = client.get('/health')
        assert response.json()['api_version'] == 'v1'
        assert 'key' not in response.text


@pytest.mark.parametrize('payload', [{'url': 1}, {'url': ''}, {'url': 'https://example.org', 'headers': {}},
                                    {'url': 'https://example.org', 'timeout_seconds': 100}])
def test_validation_does_not_echo_input(payload):
    with TestClient(main.app) as client:
        response = client.post('/v1/extract', json=payload, headers=AUTH)
        assert response.status_code == 422 and response.json()['error_code'] == 'invalid_request'
        assert 'https://' not in response.text


def test_auth_and_body_limit():
    with TestClient(main.app) as client:
        assert client.post('/v1/extract', json={'url': 'https://example.org'}).status_code == 401
        assert client.post('/v1/extract', content='x' * 9000, headers=AUTH).status_code == 413


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.setenv('WEB_READER_API_KEY', '')
    with pytest.raises(RuntimeError, match='WEB_READER_API_KEY'):
        with TestClient(main.app):
            pass


def test_api_success_and_slot_release(monkeypatch):
    async def fake_job(request, args):
        return {'status': 'ok', 'content': 'synthetic text'}
    monkeypatch.setattr(main, 'run_job', fake_job)
    with TestClient(main.app) as client:
        for _ in range(5):
            assert client.post('/v1/extract', json={'url': 'https://example.org'}, headers=AUTH).json()['status'] == 'ok'


def test_real_child_is_terminated_on_timeout(monkeypatch):
    monkeypatch.setattr(main, 'worker', slow_worker)
    previous = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with TestClient(main.app) as client:
        response = client.post('/v1/extract', json={'url': 'https://example.org', 'timeout_seconds': 1}, headers=AUTH)
    assert response.json()['error_code'] == 'fetch_timeout'
    assert time.monotonic() - started < 6
    assert {child.pid for child in multiprocessing.active_children()} == previous


def test_real_child_is_terminated_on_disconnect(monkeypatch):
    monkeypatch.setattr(main, 'worker', slow_worker)
    previous = {child.pid for child in multiprocessing.active_children()}
    async def disconnected():
        return True
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(max_bytes=65536)),
                              is_disconnected=disconnected)
    result = asyncio.run(main.run_job(request, main.ExtractRequest(url='https://example.org')))
    assert result['error_code'] == 'client_disconnected'
    assert {child.pid for child in multiprocessing.active_children()} == previous


def test_busy_rejected_without_starting_worker(monkeypatch):
    with TestClient(main.app) as client:
        main.app.state.slots = asyncio.Semaphore(0)
        response = client.post('/v1/extract', json={'url': 'https://example.org'}, headers=AUTH)
        assert response.status_code == 429


def test_initializer_is_non_destructive_and_private(tmp_path):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location('deploy_init', root / 'deploy' / 'init.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / '.env.example').write_text('WEB_READER_API_KEY=\n', encoding='utf-8')
    (tmp_path / 'searxng').mkdir()
    (tmp_path / 'searxng' / 'settings.yml.example').write_text('secret: __GENERATED_SECRET__', encoding='utf-8')
    assert module.initialize(tmp_path) == ['.env']
    original = (tmp_path / '.env').read_bytes()
    assert len(original) > 60 and not (tmp_path / 'runtime').exists()
    assert module.initialize(tmp_path, True) == ['runtime/searxng/settings.yml']
    settings = (tmp_path / 'runtime/searxng/settings.yml').read_bytes()
    assert module.initialize(tmp_path, True) == []
    assert (tmp_path / '.env').read_bytes() == original
    assert (tmp_path / 'runtime/searxng/settings.yml').read_bytes() == settings
