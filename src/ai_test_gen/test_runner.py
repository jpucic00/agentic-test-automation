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
from pathlib import Path

from .config import Config
from .models import GeneratedTest, TestRunResult

# Hard cap on a single Playwright run. A hung browser/test must not wedge the
# pipeline; on expiry the process is killed and the run is reported as an error.
RUN_TIMEOUT_S = 300


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

    did_run = _report_parses(stdout)
    failed_test, error_message = _parse_failure(stdout)
    if error_message is None:
        # No parseable JSON report (compile error, no tests, crash): the spec never
        # actually ran. Surface the stderr tail; did_run=False routes this class back
        # to the Generator (the Healer's browser can't see a TypeScript error).
        error_message = stderr[:500] or "Playwright run failed (no JSON report produced)"

    return TestRunResult(
        status="failed",
        did_run=did_run,
        stdout=stdout,
        stderr=stderr,
        failed_test=failed_test,
        error_message=error_message,
        trace_path=_find_trace(config.output_dir),
    )


def _report_parses(stdout: str) -> bool:
    """True when stdout is a parseable Playwright JSON report (i.e. tests actually ran)."""
    try:
        json.loads(stdout)
    except json.JSONDecodeError:
        return False
    return True


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


def _parse_failure(stdout: str) -> tuple[str | None, str | None]:
    """Extract ``(failed_test_title, error_message)`` from Playwright JSON output.

    Returns ``(None, None)`` when the output is not parseable JSON, so the caller can
    fall back to stderr.
    """
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError:
        return None, None

    for suite in report.get("suites", []):
        found = _scan_suite(suite)
        if found is not None:
            return found
    return None, None


def _scan_suite(suite: dict) -> tuple[str | None, str | None] | None:
    """Depth-first search for the first failed/timedOut spec in a Playwright suite tree."""
    for spec in suite.get("specs", []):
        for test_entry in spec.get("tests", []):
            for run in test_entry.get("results", []):
                if run.get("status") in ("failed", "timedOut"):
                    message = (run.get("error") or {}).get("message")
                    return spec.get("title"), message
    for child in suite.get("suites", []):
        found = _scan_suite(child)
        if found is not None:
            return found
    return None
