"""Unit tests for the Playwright test runner — fully local (no npx, no browser).

``asyncio.create_subprocess_exec`` is monkeypatched to an ``AsyncMock`` returning a
fake process, so no real Playwright run happens. Coroutines are driven with
``asyncio.run`` (no pytest-asyncio).

The runner module is imported as ``runner`` (not ``test_runner``) so pytest does not
mistake the imported module for a test module.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from ai_test_gen import models
from ai_test_gen import test_runner as runner


class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b"", *, hang=False, kill_exc=None):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self._kill_exc = kill_exc
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)  # cancelled by wait_for's timeout
        return self._stdout, self._stderr

    def kill(self):
        if self._kill_exc is not None:
            raise self._kill_exc
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_proc(monkeypatch, proc):
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    return proc


def _generated(code="// test", file_name="QA-1-login.spec.ts"):
    return models.GeneratedTest(file_name=file_name, code=code, description="login happy path")


def test_run_test_passed(cfg, monkeypatch):
    _patch_proc(monkeypatch, _FakeProc(0, stdout=b'{"suites": []}'))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "passed"
    # The spec was written to disk under tests_dir.
    assert (cfg.tests_dir / "QA-1-login.spec.ts").read_text() == "// test"


def test_run_test_failed_parses_json(cfg, monkeypatch):
    failed_run = {"status": "failed", "error": {"message": "locator timeout: #login-submit"}}
    report = {
        "suites": [
            {"title": "login.spec.ts", "suites": [  # nested suites → exercises recursion
                {"title": "Login", "specs": [
                    {"title": "QA-1: logs in", "tests": [{"results": [failed_run]}]}
                ]}
            ]}
        ]
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.failed_test == "QA-1: logs in"
    assert "locator timeout" in (result.error_message or "")


def test_run_test_failed_unparseable_falls_back_to_stderr(cfg, monkeypatch):
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"not json", stderr=b"Error: boom happened"))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.failed_test is None
    assert "boom happened" in (result.error_message or "")


def test_run_test_timeout_returns_error_and_kills(cfg, monkeypatch):
    monkeypatch.setattr(runner, "RUN_TIMEOUT_S", 0.01)
    proc = _patch_proc(monkeypatch, _FakeProc(0, hang=True))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "error"
    assert "timed out" in (result.error_message or "")
    assert proc.killed is True


def test_run_test_launch_failure_returns_error(cfg, monkeypatch):
    monkeypatch.setattr(
        runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("npx not found")),
    )
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "error"
    assert "Could not launch" in (result.error_message or "")


def test_run_test_timeout_suppresses_processlookuperror_on_kill(cfg, monkeypatch):
    # If the process already exited, proc.kill() raises ProcessLookupError; the runner
    # must swallow it and still report a clean timeout error rather than propagating.
    monkeypatch.setattr(runner, "RUN_TIMEOUT_S", 0.01)
    _patch_proc(monkeypatch, _FakeProc(0, hang=True, kill_exc=ProcessLookupError()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "error"
    assert "timed out" in (result.error_message or "")


def test_run_test_timedout_status_is_parsed(cfg, monkeypatch):
    timed_out = {"status": "timedOut", "error": {"message": "Test timeout of 30000ms exceeded"}}
    report = {
        "suites": [
            {"title": "slow.spec.ts", "specs": [
                {"title": "QA-1: slow path", "tests": [{"results": [timed_out]}]}
            ]}
        ]
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.failed_test == "QA-1: slow path"
    assert "30000ms" in (result.error_message or "")


def test_run_test_failed_empty_output_uses_default_message(cfg, monkeypatch):
    # returncode != 0, unparseable stdout, empty stderr -> the default failure message.
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"not json", stderr=b""))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.error_message == "Playwright run failed (no JSON report produced)"


def test_run_test_failed_surfaces_trace_path(cfg, monkeypatch):
    # trace: 'retain-on-failure' leaves test-results/**/trace.zip; the runner must
    # surface the newest one so the MR/result summary can point a reviewer at it.
    trace = cfg.output_dir / "test-results" / "QA-1-login" / "trace.zip"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_bytes(b"zip")
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"not json", stderr=b"boom"))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.trace_path == str(trace)


def test_run_test_failed_without_trace_has_none(cfg, monkeypatch):
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"not json", stderr=b"boom"))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.trace_path is None
