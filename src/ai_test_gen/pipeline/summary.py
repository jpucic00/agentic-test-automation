"""The run summary ``process_test_case`` returns: per-environment results, Vision Aid counts,
and per-agent model usage, each added (and logged) by its own ``with_*`` step."""

from __future__ import annotations

import logging
import time

from ..agents.tools.inspect_screen import VisionStats
from ..core.config import Config
from ..core.models import EnvironmentRunResult, TestRunResult
from ..core.usage import UsageLog, describe, format_usage

logger = logging.getLogger(__name__)


def environment_result(
    base_url: str, result: TestRunResult, *, primary: bool
) -> EnvironmentRunResult:
    """Per-environment summary: status plus the error's first line (capped) when not passed."""
    error = None
    if result.status != "passed":
        lines = (result.error_message or result.stderr or "").strip().splitlines()
        error = (lines[0] if lines else "(no error output)")[:300]
    return EnvironmentRunResult(
        base_url=base_url, primary=primary, status=result.status, error=error
    )


def with_environments(summary: dict, env_results: list[EnvironmentRunResult]) -> dict:
    """Add the per-environment results to a run summary (multi-environment runs only)."""
    if env_results:
        summary["environments"] = [r.model_dump() for r in env_results]
    return summary


def with_vision(summary: dict, config: Config, vision: dict[str, VisionStats]) -> dict:
    """Add the per-agent Vision Aid line to a run summary (only when vision is enabled)."""
    if config.vision_max_calls > 0:
        line = "; ".join(f"{agent}: {stats.describe()}" for agent, stats in vision.items())
        summary["vision"] = line
        degraded = any(stats.degraded for stats in vision.values())
        logger.log(
            logging.WARNING if degraded else logging.INFO,
            "[%s] Vision Aid — %s",
            summary["issue_key"],
            line,
        )
    return summary


def with_usage(summary: dict, usage: UsageLog, started: float) -> dict:
    """Add the per-agent usage records + run totals to a run summary; log the totals.

    ``started`` is the run's ``time.monotonic()`` start, so ``total.wall_s`` is the whole run's
    wall time (test runs and the MR included), not a sum of the agent runs.
    """
    report = usage.summary(time.monotonic() - started)
    summary["usage"] = report
    total = report["total"]
    logger.info(
        "[%s] Usage total: %s (%d agent run record(s))\n%s",
        summary["issue_key"],
        describe(total["requests"], total["input_tokens"], total["output_tokens"], total["wall_s"]),
        len(report["agents"]),
        format_usage(report),
    )
    return summary
