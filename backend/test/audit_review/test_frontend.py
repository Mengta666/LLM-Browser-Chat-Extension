"""前端真实函数：虚拟时钟验证截止时间，lxml 验证生成的 HTML 结构。

这不是 Chrome/CSP 端到端测试，不据此宣称可执行 XSS。
"""

import json
import subprocess
from pathlib import Path

import pytest
from lxml import html


@pytest.fixture(scope="module")
def frontend():
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("frontend_harness.cjs"))],
        capture_output=True, encoding="utf-8", check=True, timeout=30,
    )
    return json.loads(result.stdout)


def test_stream_within_deadline_control(frontend):
    result = frontend["fastBackground"]
    assert result["errorName"] == ""
    assert result["answers"] == ["final-answer"]
    assert result["progressCount"] == 2


def test_panel_within_deadline_control(frontend):
    result = frontend["fastPanel"]
    assert result["finalizedAt"] == [100000]
    assert result["history"][-1]["content"] == "final-answer"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F3: active SSE progress does not extend the 120s total timeout")
def test_active_multiround_stream_can_finish_after_120_seconds(frontend):
    result = frontend["slowBackground"]
    assert result["progressCount"] == 2
    assert result["answers"] == ["final-answer"], result


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F3: panel stops listening at 120s despite tool progress")
def test_active_panel_waits_for_final_answer(frontend):
    result = frontend["slowPanel"]
    assert result["history"], result


def test_normal_citation_control(frontend):
    document = html.fromstring(frontend["normalCitation"])
    assert document.xpath("//a/@href") == ["https://example.invalid/source"]
    assert not document.xpath("//*[@id='audit-injected']")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F8: source URL escapes href and injects additional markup")
def test_citation_url_cannot_create_html_elements(frontend):
    document = html.fromstring(frontend["injectedCitation"])
    assert not document.xpath("//*[@id='audit-injected']"), frontend["injectedCitation"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F8: whole-HTML citation substitution corrupts existing attributes")
def test_citation_substitution_does_not_change_existing_href(frontend):
    document = html.fromstring(frontend["attributeCitation"])
    assert document.xpath("//a/@href") == ["https://example.invalid/?q=[1]"]
