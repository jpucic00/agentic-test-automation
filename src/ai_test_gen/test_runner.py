"""Execute a generated Playwright test and return a structured result.

Writes the generated ``.spec.ts`` into ``output/tests/`` and runs it with the Node
Playwright harness in ``output/`` (``npx playwright test`` + the JSON reporter), then
parses the report into a :class:`~ai_test_gen.models.TestRunResult`.

Phase 1.D — task ``j18du5c`` (AI_TEST_GENERATION_GUIDE.md §3.11). Two things the
guide's template lacks:

- A **hard timeout** (``asyncio.wait_for`` around ``proc.communicate()``): a hung
  Playwright run would otherwise block the whole pipeline forever. On timeout the
  process is killed and the result is ``status="error"``.
- ``run_test`` **never raises on a test failure** — a failing test is a healable state
  the orchestrator hands to the Healer, not an exception.

The generated test logs itself in with the disposable staging dummy creds from
``project_context.md`` (context-driven auth), so the runner needs no credentials or
storage state — the subprocess just inherits this process's environment for ``PATH`` /
node.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

from .config import Config
from .models import GeneratedTest, TestRunResult

# Hard cap on a single Playwright run. A hung browser/test must not wedge the
# pipeline; on expiry the process is killed and the run is reported as an error.
RUN_TIMEOUT_S = 300

# Cap on the top-level report errors surfaced as error_message (compile/load errors
# carry a code frame; several can stack up).
REPORT_ERRORS_MAX_CHARS = 2000


async def run_test(config: Config, test: GeneratedTest) -> TestRunResult:
    """Write ``test`` to disk, run it via Playwright, and parse the result.

    Returns a :class:`~ai_test_gen.models.TestRunResult`. Does not raise when the test
    itself fails (that is healable); only infrastructure problems (timeout, the runner
    failing to launch) surface as ``status="error"``.
    """
    test_path = config.tests_dir / test.file_name
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(test.code)

    cmd = [
        "npx",
        "playwright",
        "test",
        str(test_path),
        "--reporter=json",
        "--workers=1",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(config.output_dir),
            # No env= override: inherit this process's environment (PATH/node). The
            # generated test carries its own literal dummy creds, so the run needs
            # no per-run secrets or storage state.
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
        trace_path=_find_trace(config.output_dir),
    )


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


def _find_trace(output_dir: Path) -> str | None:
    """Path of the newest ``trace.zip`` under ``output/test-results``, if any.

    Playwright (``trace: 'retain-on-failure'``) writes a trace per failed test and
    clears ``test-results/`` at the start of every run, so any trace found here
    belongs to the run that just finished.
    """
    results_dir = output_dir / "test-results"
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
                    return spec.get("title"), error.get("message"), _error_line(error, file_name)
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
