import importlib.util
from email.message import Message
from io import BytesIO
from pathlib import Path
import urllib.error
import urllib.request

import pytest


def test_deployment_checker_does_not_forward_auth_on_redirect():
    path = Path(__file__).resolve().parents[3] / 'deploy' / 'check.py'
    spec = importlib.util.spec_from_file_location('deploy_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    class RedirectServer(urllib.request.HTTPHandler):
        def http_open(self, request):
            calls.append(request.full_url)
            assert len(calls) == 1, 'Redirect must not be followed'
            headers = Message()
            headers['Location'] = 'http://other.invalid/'
            response = urllib.response.addinfourl(BytesIO(b''), headers, request.full_url, 302)
            response.msg = 'Found'
            return response
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), module.NoRedirect(), RedirectServer())
    request = urllib.request.Request('http://reader.invalid/v1/extract',
                                     headers={'Authorization': 'Bearer synthetic-test'})
    with pytest.raises(urllib.error.HTTPError):
        opener.open(request)
    assert calls == ['http://reader.invalid/v1/extract']
