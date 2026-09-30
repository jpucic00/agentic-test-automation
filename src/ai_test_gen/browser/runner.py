"""Execute a generated Playwright test and return a structured result.

Writes the generated ``.spec.ts`` into ``output/tests/`` and runs it with the Node
Playwright harness in ``output/`` (``npx playwright test`` + the JSON reporter), then
parses the report into a :class:`~ai_test_gen.core.models.TestRunResult`.

Phase 1.D — task ``j18du5c`` (AI_TEST_GENERATION_GUIDE.md §3.11). Two things the
guide's template lacks:

- A **hard timeout** (``asyncio.wait_for`` around ``proc.communicate()``): a hung
  Playwright run would otherwise block the whole pipeline forever. On timeout the
  process is killed and the result is ``status="error"``.
- ``run_test`` **never raises on a test failure** — a failing test is a healable state
  the orchestrator hands to the Healer, not an exception.

The generated test logs itself in with the disposable staging dummy creds from
``project_context.md`` (context-driven auth), so the runner needs no credentials or
storage state — the subprocess inherits this process's environment for ``PATH`` / node,
plus ``BASE_URL``: the environment this run targets, which ``output/playwright.config.ts``
maps to ``use.baseURL`` so baseURL-relative ``page.goto('/…')`` calls follow the run.

Before Playwright starts, the spec's literal ``goto(...)`` targets and the plan's recorded
URLs are checked against the navigation allow-list (``allowlist.preflight_violations``); a
violation returns ``status="error"`` with ``blocked=True`` and nothing is executed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from ..core.config import Config
from ..core.models import GeneratedTest, TestPlan, TestRunResult
from ..guardrails.allowlist import preflight_violations

# Hard cap on a single Playwright run. A hung browser/test must not wedge the
# pipeline; on expiry the process is killed and the run is reported as an error.
RUN_TIMEOUT_S = 300

# Cap on the top-level report errors surfaced as error_message (compile/load errors
# carry a code frame; several can stack up).
REPORT_ERRORS_MAX_CHARS = 2000


async def run_test(
    config: Config,
    test: GeneratedTest,
    *,
    plan: TestPlan | None = None,
    base_url: str | None = None,
    results_dir: str = "test-results",
) -> TestRunResult:
    """Write ``test`` to disk, run it via Playwright, and parse the result.

    ``base_url`` is the environment this run targets (default: the primary
    ``config.staging_base_url``), exported as ``BASE_URL``. ``results_dir`` is the
    Playwright ``--output`` folder under ``output/`` — secondary environments use their
    own so they don't wipe the primary run's trace. ``plan`` adds its recorded URLs to the
    pre-run allow-list check.

    Returns a :class:`~ai_test_gen.core.models.TestRunResult`. Does not raise when the test
    itself fails (that is healable); only infrastructure problems (timeout, the runner
    failing to launch) and a refused pre-run navigation check surface as ``status="error"``.
    """
    base_url = base_url or config.staging_base_url
    test_path = config.tests_dir / test.file_name
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(test.code)

    violations = preflight_violations(
        test.code,
        plan,
        base_url=base_url,
        env_origins=config.environment_origins,
        extra_origins=config.extra_origins,
    )
    if violations:
        return TestRunResult(
            status="error",
            did_run=False,
            blocked=True,
            stdout="",
            stderr="",
            error_message=(
                f"Run blocked before Playwright started (baseURL {base_url}): "
                + "; ".join(violations)
            ),
        )

    cmd = [
        "npx",
        "playwright",
        "test",
        str(test_path),
        "--reporter=json",
        "--workers=1",
        f"--output={results_dir}",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(config.output_dir),
            # Inherit this process's environment (PATH/node) + BASE_URL for this run. The
            # generated test carries its own literal dummy creds, so the run needs no
            # per-run secrets or storage state.
            env={**os.environ, "BASE_URL": base_url},
        )
    except (FileNotFoundError, OSError) as exc:  # npx/node missing, cwd gone, ...
        return TestRunResult(
            status="error",
            stdout="",
            stderr=str(exc),
            error_message=f"Could not launch Playwright: {exc}",
        )

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=RUN_TIMEOUT_S
        )
    except TimeoutError:
        # The process may already have exited in the race between the timeout firing
        # and the kill; suppress ProcessLookupError on both kill() and wait() so a
        # timeout always returns status="error" instead of raising.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
            await proc.wait()
        return TestRunResult(
            status="error",
            stdout="",
            stderr="",
            error_message=f"Playwright run timed out after {RUN_TIMEOUT_S}s",
        )

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode == 0:
        return TestRunResult(status="passed", stdout=stdout, stderr=stderr)

    report = _load_report(stdout)
    # A compile/load error still yields a valid JSON report — just one with no specs
    # and the error in the top-level ``errors`` array. So "did it run" is decided by
    # the report's content, not by whether stdout parses. did_run=False routes this
    # class back to the Generator (the Healer's browser can't see a TypeScript error).
    did_run = report is not None and _has_results(report)
    failed_test, error_message, error_line = (
        _parse_failure(report, test.file_name) if report is not None else (None, None, None)
    )
    if error_message is None and report is not None:
        error_message = _report_errors(report)
    if error_message is None:
        # No parseable JSON report at all (crash before the reporter ran): surface the
        # stderr tail.
        error_message = stderr[-500:] or "Playwright run failed (no JSON report produced)"

    return TestRunResult(
        status="failed",
        did_run=did_run,
        stdout=stdout,
        stderr=stderr,
        failed_test=failed_test,
        error_message=error_message,
        error_line=error_line,
        trace_path=_find_trace(config.output_dir / results_dir),
    )


FailureKind = Literal["locator", "assertion", "navigation", "other"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MATCHER_RE = re.compile(r"expect\(|\.(?:tobe|tohave|tocontain|tomatch|toequal)\w*\(")
_WAITING_FOR_LOCATOR_RE = re.compile(r"waiting for (?:locator\(|getby|frame)")


def classify_failure(error_message: str | None) -> FailureKind:
    """Coarse kind of a Playwright failure, from its error text.

    - ``locator``: the target element was never found or never unique — a strict-mode
      violation, any ``expect(locator)`` whose element was not found (``element(s) not
      found``, e.g. a step's pre-action ``toBeVisible()`` guard), or an action timeout while
      waiting for a locator (``locator.click: Timeout 30000ms exceeded``).
    - ``assertion``: an ``expect(...)`` on a FOUND element / the page whose value or state
      differs (``toHaveText``, ``toHaveValue``, ``toBeDisabled``, ``toHaveURL``, counts…).
    - ``navigation``: ``page.goto`` / ``waitForURL`` failures and ``net::ERR_*``.
    - ``other``: anything else (JS errors, a bare test timeout with no call log), and an
      element that was found but is HIDDEN (``Received: hidden``). That is ambiguous — a
      wrong locator matching a hidden duplicate, or a missing prior step such as opening a
      menu — so it gets neither locator escalation nor divergence guidance; the Healer
      replays the flow live and decides.
    """
    msg = _ANSI_RE.sub("", error_message or "").lower()
    if "strict mode violation" in msg or "element(s) not found" in msg:
        return "locator"
    if "net::err_" in msg or "page.goto:" in msg or "waitforurl" in msg:
        return "navigation"
    if "received: hidden" in msg:
        return "other"
    if _MATCHER_RE.search(msg):
        return "assertion"
    if "timeout" in msg and "exceeded" in msg and (
        "locator." in msg or _WAITING_FOR_LOCATOR_RE.search(msg)
    ):
        return "locator"
    return "other"


def _load_report(stdout: str) -> dict | None:
    """The Playwright JSON report on stdout, or ``None`` when stdout is not one."""
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return report if isinstance(report, dict) else None


def _has_results(report: dict) -> bool:
    """True when at least one spec in the report actually produced a test result."""
    return any(
        test_entry.get("results")
        for spec in _iter_specs(report.get("suites", []))
        for test_entry in spec.get("tests", [])
    )


def _report_errors(report: dict) -> str | None:
    """Top-level report ``errors`` (compile/load/collection failures) as one message.

    Each entry is prefixed with its ``file:line`` when Playwright gives a location, and
    the result is capped so it stays a usable prompt snippet.
    """
    parts: list[str] = []
    for error in report.get("errors", []):
        message = (error.get("message") or "").strip()
        if not message:
            continue
        location = error.get("location") or {}
        if location.get("file") and location.get("line"):
            message = f"{location['file']}:{location['line']}: {message}"
        parts.append(message)
    return "\n\n".join(parts)[:REPORT_ERRORS_MAX_CHARS] if parts else None


def _find_trace(results_dir: Path) -> str | None:
    """Path of the newest ``trace.zip`` under the run's ``--output`` folder, if any.

    Playwright (``trace: 'retain-on-failure'``) writes a trace per failed test and
    clears its output folder at the start of every run, so any trace found here
    belongs to the run that just finished.
    """
    if not results_dir.is_dir():
        return None
    traces = sorted(results_dir.rglob("trace.zip"), key=lambda p: p.stat().st_mtime)
    return str(traces[-1]) if traces else None


def _parse_failure(
    report: dict, file_name: str | None = None
) -> tuple[str | None, str | None, int | None]:
    """Extract ``(failed_test_title, error_message, error_line)`` from a Playwright report.

    Returns ``(None, None, None)`` when no spec failed, so the caller can fall back to
    the report's top-level errors / stderr.
    """
    for spec in _iter_specs(report.get("suites", [])):
        for test_entry in spec.get("tests", []):
            for run in test_entry.get("results", []):
                if run.get("status") in ("failed", "timedOut"):
                    error = run.get("error") or {}
                    # A per-test timeout's first error can be a bare "Test timeout … exceeded";
                    # the locator call log sits in a later entry, so join them all.
                    messages = [e.get("message") for e in run.get("errors") or []]
                    messages.append(error.get("message"))
                    message = "\n\n".join(dict.fromkeys(m for m in messages if m)) or None
                    return spec.get("title"), message, _error_line(error, file_name)
    return None, None, None


def _iter_specs(suites: list[dict]) -> Iterator[dict]:
    """Depth-first walk of a Playwright suite tree: each suite's specs, then its children."""
    for suite in suites:
        yield from suite.get("specs", [])
        yield from _iter_specs(suite.get("suites", []))


def _error_line(error: dict, file_name: str | None) -> int | None:
    """The spec line the run died on: error location if present, else stack/message parse.

    The line lets the Healer separate code that ran from code that NEVER executed —
    without it, a downstream timeout reads as a downstream bug.
    """
    location = error.get("location") or {}
    line = location.get("line")
    if isinstance(line, int) and line > 0:
        return line
    if not file_name:
        return None
    # Stack frames (and often the message) reference "<path>/<file>:<line>:<col>".
    pattern = re.compile(re.escape(file_name) + r":(\d+):\d+")
    for text in (error.get("stack") or "", error.get("message") or ""):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None
