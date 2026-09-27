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


def test_run_test_timeout_joins_all_errors_so_the_locator_log_is_kept(cfg, monkeypatch):
    bare = {"message": "Test timeout of 30000ms exceeded."}
    call_log = {
        "message": "Error: locator.click: Test timeout of 30000ms exceeded.\n"
        "Call log:\n  - waiting for getByTestId('logout')"
    }
    timed_out = {"status": "timedOut", "error": bare, "errors": [bare, call_log]}
    report = {
        "suites": [
            {"title": "slow.spec.ts", "specs": [
                {"title": "QA-1: slow path", "tests": [{"results": [timed_out]}]}
            ]}
        ]
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    message = result.error_message or ""
    assert message.count("Test timeout of 30000ms exceeded.") == 2  # bare + call-log line, no dupes
    assert "waiting for getByTestId('logout')" in message
    assert runner.classify_failure(message) == "locator"


def test_run_test_failed_empty_output_uses_default_message(cfg, monkeypatch):
    # returncode != 0, unparseable stdout, empty stderr -> the default failure message.
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"not json", stderr=b""))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.error_message == "Playwright run failed (no JSON report produced)"


def test_run_test_extracts_error_line_from_location(cfg, monkeypatch):
    failed_run = {
        "status": "failed",
        "error": {"message": "locator timeout", "location": {"line": 7, "column": 11}},
    }
    report = {
        "suites": [
            {"title": "x.spec.ts", "specs": [
                {"title": "QA-1: x", "tests": [{"results": [failed_run]}]}
            ]}
        ]
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.error_line == 7


def test_run_test_extracts_error_line_from_stack_fallback(cfg, monkeypatch):
    # No structured location — the line is parsed from the stack frame that names
    # the spec file ("<path>/<file>:<line>:<col>").
    failed_run = {
        "status": "failed",
        "error": {
            "message": "locator.click: Timeout 30000ms exceeded",
            "stack": "Error: ...\n    at /app/output/tests/QA-1-login.spec.ts:12:31",
        },
    }
    report = {
        "suites": [
            {"title": "x.spec.ts", "specs": [
                {"title": "QA-1: x", "tests": [{"results": [failed_run]}]}
            ]}
        ]
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.error_line == 12


def test_run_test_error_line_none_when_unavailable(cfg, monkeypatch):
    failed_run = {"status": "failed", "error": {"message": "boom, no location anywhere"}}
    report = {
        "suites": [
            {"title": "x.spec.ts", "specs": [
                {"title": "QA-1: x", "tests": [{"results": [failed_run]}]}
            ]}
        ]
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.error_line is None


def test_run_test_no_report_marks_did_run_false(cfg, monkeypatch):
    # Unparseable stdout = compile/collection error: the spec never executed, so the
    # orchestrator must route it to the Generator (did_run=False), not the Healer.
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"SyntaxError: unexpected token", stderr=b""))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.did_run is False


def test_run_test_compile_error_report_marks_did_run_false(cfg, monkeypatch):
    # A spec that fails to load/compile still gets a VALID JSON report from Playwright:
    # no suites, the error only in the top-level "errors" array, stderr empty. That is
    # still "never ran" (-> Generator retry) and the real error must be surfaced.
    report = {
        "suites": [],
        "errors": [
            {
                "message": "SyntaxError: QA-1-login.spec.ts: Unexpected token (3:12)",
                "location": {"file": "/app/output/tests/QA-1-login.spec.ts", "line": 3},
            },
            {"message": "Error: No tests found"},
        ],
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode(), stderr=b""))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.did_run is False
    assert result.failed_test is None
    msg = result.error_message or ""
    assert "SyntaxError: QA-1-login.spec.ts: Unexpected token" in msg
    assert "/app/output/tests/QA-1-login.spec.ts:3" in msg
    assert "No tests found" in msg


def test_run_test_failing_test_report_keeps_did_run_true_and_test_error(cfg, monkeypatch):
    # A spec that ran and failed: did_run stays True and the test's own error wins over
    # any top-level report errors.
    failed_run = {"status": "failed", "error": {"message": "expect(locator).toBeVisible failed"}}
    report = {
        "suites": [
            {"title": "x.spec.ts", "suites": [
                {"title": "Login", "specs": [
                    {"title": "QA-1: x", "tests": [{"results": [failed_run]}]}
                ]}
            ]}
        ],
        "errors": [{"message": "Error: unrelated worker teardown"}],
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.did_run is True
    assert result.failed_test == "QA-1: x"
    assert result.error_message == "expect(locator).toBeVisible failed"


def test_run_test_unparseable_uses_stderr_tail(cfg, monkeypatch):
    stderr = b"x" * 600 + b"Error: the real cause"
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"not json", stderr=stderr))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert (result.error_message or "").endswith("Error: the real cause")
    assert len(result.error_message or "") == 500


def test_run_test_parsed_report_failure_keeps_did_run_true(cfg, monkeypatch):
    failed_run = {"status": "failed", "error": {"message": "locator timeout"}}
    report = {
        "suites": [
            {"title": "x.spec.ts", "specs": [
                {"title": "QA-1: x", "tests": [{"results": [failed_run]}]}
            ]}
        ]
    }
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert result.did_run is True


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


# --- per-environment BASE_URL + pre-run navigation check -----------------------------


def test_run_test_exports_base_url_defaulting_to_the_primary(cfg, monkeypatch):
    monkeypatch.setenv("RUNNER_INHERIT_PROBE", "1")
    spawn = AsyncMock(return_value=_FakeProc(0, stdout=b'{"suites": []}'))
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(runner.run_test(cfg, _generated()))
    kwargs = spawn.call_args.kwargs
    assert kwargs["env"]["BASE_URL"] == cfg.staging_base_url
    assert kwargs["env"]["RUNNER_INHERIT_PROBE"] == "1"  # still inherits the process env
    assert "--output=test-results" in spawn.call_args.args


def test_run_test_uses_the_given_environment_and_results_dir(cfg, monkeypatch):
    spawn = AsyncMock(return_value=_FakeProc(0, stdout=b'{"suites": []}'))
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(
        runner.run_test(
            cfg, _generated(), base_url="https://qa2.example.internal",
            results_dir="test-results/env-2",
        )
    )
    assert spawn.call_args.kwargs["env"]["BASE_URL"] == "https://qa2.example.internal"
    assert "--output=test-results/env-2" in spawn.call_args.args


def test_run_test_trace_is_looked_up_in_the_runs_results_dir(cfg, monkeypatch):
    trace = cfg.output_dir / "test-results" / "env-2" / "QA-1" / "trace.zip"
    trace.parent.mkdir(parents=True)
    trace.write_bytes(b"zip")
    _patch_proc(monkeypatch, _FakeProc(1, stdout=b"not json", stderr=b"boom"))
    result = asyncio.run(runner.run_test(cfg, _generated(), results_dir="test-results/env-2"))
    assert result.trace_path == str(trace)


def test_offlist_goto_blocks_the_run_before_playwright_starts(cfg, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    code = "await page.goto('https://www.example.com/');"
    result = asyncio.run(runner.run_test(cfg, _generated(code=code)))
    spawn.assert_not_called()
    assert result.status == "error"
    assert result.blocked is True
    assert result.did_run is False
    assert "https://www.example.com" in (result.error_message or "")
    # The spec is still written to disk for the reviewer.
    assert (cfg.tests_dir / "QA-1-login.spec.ts").read_text() == code


def test_offlist_plan_url_blocks_the_run(cfg, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    plan = models.TestPlan(
        test_case_key="QA-1", title="t", target_url="https://prod.example.com/", steps=[]
    )
    result = asyncio.run(runner.run_test(cfg, _generated(code="await page.goto('/');"), plan=plan))
    spawn.assert_not_called()
    assert result.blocked and "plan target_url" in (result.error_message or "")


# --- classify_failure: realistic Playwright error text ----------------------------------------

_NOT_FOUND_GUARD = """Error: Add note button visible before click

expect(locator).toBeVisible() failed

Locator: getByTestId('add-note')
Expected: visible
Timeout: 5000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" with timeout 5000ms
  - waiting for getByTestId('add-note')
"""

_NOT_FOUND_LEGACY = """Error: Timed out 5000ms waiting for expect(locator).toBeVisible()

Locator: getByRole('button', { name: 'Save', exact: true })
Expected: visible
Received: <element(s) not found>
Call log:
  - expect.toBeVisible with timeout 5000ms
  - waiting for getByRole('button', { name: 'Save', exact: true })
"""

_ACTION_TIMEOUT = """Error: locator.click: Timeout 30000ms exceeded.
Call log:
  - waiting for getByRole('menuitem', { name: 'Log out', exact: true })
"""

_TEST_TIMEOUT_ACTION = """Error: locator.fill: Test timeout of 30000ms exceeded.
Call log:
  - waiting for locator('xpath=//input[@name="title"]')
"""

_STRICT = """Error: locator.click: Error: strict mode violation: \
getByRole('button', { name: 'Add' }) resolved to 2 elements:
    1) <button id="add-note">Add</button> aka getByTestId('add-note')
    2) <button>Add admin</button> aka getByRole('button', { name: 'Add admin' })
"""

_TO_HAVE_TEXT = """Error: expect(locator).toHaveText(expected) failed

Locator:  getByTestId('note-title')
Expected: "Groceries"
Received: "Untitled"
Timeout:  5000ms

Call log:
  - Expect "toHaveText" with timeout 5000ms
  - waiting for getByTestId('note-title')
    9 × locator resolved to <h2 data-testid="note-title">Untitled</h2>
      - unexpected value "Untitled"
"""

_TO_BE_DISABLED = """Error: Save stays disabled for an empty title

expect(locator).toBeDisabled() failed

Locator:  getByTestId('save-note')
Expected: disabled
Received: enabled
Timeout:  5000ms

Call log:
  - Expect "toBeDisabled" with timeout 5000ms
  - waiting for getByTestId('save-note')
    9 × locator resolved to <button data-testid="save-note">Save</button>
      - unexpected value "enabled"
"""

_TO_HAVE_URL = """Error: expect(page).toHaveURL(expected) failed

Expected: "http://localhost:3000/notes"
Received: "http://localhost:3000/login"
Timeout:  5000ms
"""

_TO_HAVE_COUNT = """Error: expect(locator).toHaveCount(expected) failed

Locator:  getByTestId('note-card')
Expected: 3
Received: 2
Timeout:  5000ms
"""

_LEGACY_TO_HAVE_VALUE = """Error: Timed out 5000ms waiting for expect(locator).toHaveValue(expected)

Locator: getByLabel('Email', { exact: true })
Expected string: "demo@demo.test"
Received string: ""
"""


def test_classify_failure_locator_kinds():
    for message in (
        _NOT_FOUND_GUARD,
        _NOT_FOUND_LEGACY,
        _ACTION_TIMEOUT,
        _TEST_TIMEOUT_ACTION,
        _STRICT,
        "TimeoutError: page.click: Timeout 5000ms exceeded.\n"
        "Call log:\n  - waiting for locator('#x')",
    ):
        assert runner.classify_failure(message) == "locator", message


def test_classify_failure_assertion_on_found_element():
    for message in (
        _TO_HAVE_TEXT, _TO_BE_DISABLED, _TO_HAVE_URL, _TO_HAVE_COUNT, _LEGACY_TO_HAVE_VALUE
    ):
        assert runner.classify_failure(message) == "assertion", message


def test_classify_failure_navigation_and_other():
    assert (
        runner.classify_failure("Error: page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:3000/")
        == "navigation"
    )
    assert (
        runner.classify_failure(
            'page.waitForURL: Timeout 30000ms exceeded.\nwaiting for navigation to "/notes"'
        )
        == "navigation"
    )
    assert runner.classify_failure("Test timeout of 30000ms exceeded.") == "other"
    assert runner.classify_failure("TypeError: Cannot read properties of undefined") == "other"
    assert runner.classify_failure(None) == "other"


def test_classify_failure_ignores_ansi_colors():
    colored = (
        "Error: \x1b[2mexpect(\x1b[22m\x1b[31mlocator\x1b[39m\x1b[2m).\x1b[22mtoBeVisible"
        "\x1b[2m()\x1b[22m failed\n\nLocator: getByTestId('x')\nExpected: visible\n"
        "Timeout: 5000ms\nError: element(s) not found\n"
    )
    assert runner.classify_failure(colored) == "locator"
    hidden = colored.replace("Error: element(s) not found", "Received: hidden")
    # Found but hidden is ambiguous (wrong locator vs missing prior step): no kind-specific push.
    assert runner.classify_failure(hidden) == "other"


def test_per_test_timeout_is_a_healable_failure_not_an_error(cfg, monkeypatch):
    # A test hitting Playwright's own per-test timeout is a normal failure (healable); only the
    # runner's whole-run RUN_TIMEOUT_S kill, a launch failure, or a blocked run is "error".
    timed_out = {"status": "timedOut", "error": {"message": _TEST_TIMEOUT_ACTION}}
    spec = {"title": "t", "tests": [{"results": [timed_out]}]}
    report = {"suites": [{"title": "x", "specs": [spec]}]}
    _patch_proc(monkeypatch, _FakeProc(1, stdout=json.dumps(report).encode()))
    result = asyncio.run(runner.run_test(cfg, _generated()))
    assert result.status == "failed"
    assert runner.classify_failure(result.error_message) == "locator"
