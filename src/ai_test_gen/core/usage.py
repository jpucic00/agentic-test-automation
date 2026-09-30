"""Per-agent model usage and wall time for one pipeline run.

Every agent run (Planner, Generator, each heal attempt, and the Vision Aid calls made inside an
agent's ``inspect_screen`` tool) is timed and its token usage recorded in one ``UsageLog`` that the
orchestrator threads down the same way it threads ``VisionStats``. The numbers are pydantic-ai's
own: ``track_usage`` hands the run a fresh ``RunUsage`` (via ``Agent.run(usage=...)``), which
pydantic-ai increments in place after every model response — so a run that aborts (a timeout,
``UsageLimitExceeded``, retry exhaustion, a gateway error) still reports what it spent up to the
failure. ``requests`` counts completed model responses; a request that died without a response
carries no token counts to report.

Reasoning-only replies the Planner/Healer were nudged about
(``agents/runtime/reasoning_only.py``) are counted in the run's ``RunUsage.details`` under
``REASONING_ONLY_RETRIES`` and reported per record as ``reasoning_only_retries`` (the ``nudges``
column).

Records merge by label: the Vision Aid calls of one agent run (``"Planner vision"``,
``"Healer attempt 2 vision"``) add up into one record.

A run given its captured history (``track_usage(..., messages=...)`` — every
``run_agent_logged`` run) also gets a ``context`` profile: how full its context window got and
what filled it (``core/context_window.py``). Vision Aid calls carry none.

``UsageLog.summary`` renders plain dicts and numbers (JSON-serialisable) for the run summary;
``format_usage`` turns that into an aligned table, ``format_context`` into per-agent composition.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage

from .context_window import (
    ContextProfile,
    describe_context,
    format_peak,
    log_context,
    profile_context,
)

logger = logging.getLogger(__name__)

Outcome = Literal["ok", "error"]

REASONING_ONLY_RETRIES = "reasoning_only_retries"


class AgentUsageDict(TypedDict):
    agent: str
    model: str
    runs: int
    requests: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    reasoning_only_retries: int
    wall_s: float
    outcome: Outcome
    context: ContextProfile | None


class UsageTotals(TypedDict):
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    reasoning_only_retries: int
    wall_s: float


class UsageSummary(TypedDict):
    agents: list[AgentUsageDict]
    total: UsageTotals


@dataclass
class AgentUsage:
    """Usage of one agent run — or of several merged under one label (the Vision Aid calls)."""

    agent: str
    model: str
    runs: int = 0
    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_only_retries: int = 0
    wall_s: float = 0.0
    outcome: Outcome = "ok"
    context: ContextProfile | None = None

    def add(
        self, usage: RunUsage, wall_s: float, *, ok: bool, context: ContextProfile | None = None
    ) -> None:
        """Fold one run's usage in; any failed run marks the record ``error``.

        Merged runs keep the context profile with the highest peak.
        """
        self.runs += 1
        self.requests += usage.requests
        self.tool_calls += usage.tool_calls
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.reasoning_tokens += usage.details.get("reasoning_tokens", 0)
        self.reasoning_only_retries += usage.details.get(REASONING_ONLY_RETRIES, 0)
        self.wall_s += wall_s
        if not ok:
            self.outcome = "error"
        if context is not None and (self.context is None or context["peak"] > self.context["peak"]):
            self.context = context

    def to_dict(self) -> AgentUsageDict:
        return AgentUsageDict(
            agent=self.agent,
            model=self.model,
            runs=self.runs,
            requests=self.requests,
            tool_calls=self.tool_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            reasoning_tokens=self.reasoning_tokens,
            reasoning_only_retries=self.reasoning_only_retries,
            wall_s=round(self.wall_s, 2),
            outcome=self.outcome,
            context=self.context,
        )


class UsageLog:
    """One run's usage records, in the order the agents first ran."""

    def __init__(self) -> None:
        self.records: list[AgentUsage] = []

    def record(
        self,
        agent: str,
        model: str,
        usage: RunUsage,
        wall_s: float,
        *,
        ok: bool,
        context: ContextProfile | None = None,
    ) -> None:
        """Add one agent run, merging into an existing record with the same label."""
        existing = next((r for r in self.records if r.agent == agent), None)
        if existing is None:
            existing = AgentUsage(agent=agent, model=model)
            self.records.append(existing)
        existing.add(usage, wall_s, ok=ok, context=context)

    def summary(self, wall_s: float) -> UsageSummary:
        """Per-agent records plus totals; ``wall_s`` is the whole run's wall time.

        Agent wall times are not summed into the total: a Vision Aid call runs INSIDE its
        owning agent's run, and the run also spends time outside any agent (test runs, MR).
        """
        return UsageSummary(
            agents=[r.to_dict() for r in self.records],
            total=UsageTotals(
                requests=sum(r.requests for r in self.records),
                input_tokens=sum(r.input_tokens for r in self.records),
                output_tokens=sum(r.output_tokens for r in self.records),
                cache_read_tokens=sum(r.cache_read_tokens for r in self.records),
                reasoning_tokens=sum(r.reasoning_tokens for r in self.records),
                reasoning_only_retries=sum(r.reasoning_only_retries for r in self.records),
                wall_s=round(wall_s, 2),
            ),
        )


@contextmanager
def track_usage(
    log: UsageLog | None,
    agent: str,
    model: str,
    *,
    messages: list[ModelMessage] | None = None,
    context_windows: Mapping[str, int] | None = None,
) -> Iterator[RunUsage]:
    """Time one agent run and record its usage — on success AND on failure.

    Yields the ``RunUsage`` to pass as ``Agent.run(usage=...)``. On exit it logs one INFO line
    (``Planner usage: 41 requests, in=512,340 out=6,210 tokens, 4m03s``, ``(aborted)`` appended
    when the run raised) and adds the run to ``log`` when one is given. Exceptions propagate.

    ``messages`` is the run's captured history (``capture_run_messages``); when given, the run's
    context profile is logged (``Planner context: peak …``) and recorded with its usage;
    ``context_windows`` (``config.model_context_windows``) sizes its peak against the model's
    window.
    """
    usage = RunUsage()
    started = time.monotonic()
    ok = False
    try:
        yield usage
        ok = True
    finally:
        wall_s = time.monotonic() - started
        logger.info(
            "%s usage: %s%s",
            agent,
            describe(usage.requests, usage.input_tokens, usage.output_tokens, wall_s),
            "" if ok else " (aborted)",
        )
        context = None
        if messages is not None:
            try:
                context = profile_context(messages, model, context_windows)
            except Exception:  # telemetry must never break a run
                logger.warning("%s context profile failed", agent, exc_info=True)
            log_context(agent, context)
        if log is not None:
            log.record(agent, model, usage, wall_s, ok=ok, context=context)


def describe(requests: int, input_tokens: int, output_tokens: int, wall_s: float) -> str:
    """``41 requests, in=512,340 out=6,210 tokens, 4m03s``."""
    noun = "request" if requests == 1 else "requests"
    return (
        f"{requests} {noun}, in={input_tokens:,} out={output_tokens:,} tokens, "
        f"{format_duration(wall_s)}"
    )


def format_duration(seconds: float) -> str:
    """``4m03s`` from one minute up, ``12.3s`` below it."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def format_usage(summary: UsageSummary) -> str:
    """Aligned table: one line per agent record plus a total line."""
    header = ("agent", "model", "requests", "in", "out", "nudges", "peak ctx", "wall", "")
    rows: list[tuple[str, ...]] = [header]
    for r in summary["agents"]:
        rows.append(
            (
                r["agent"],
                r["model"],
                str(r["requests"]),
                f"{r['input_tokens']:,}",
                f"{r['output_tokens']:,}",
                str(r["reasoning_only_retries"]),
                format_peak(r["context"]),
                format_duration(r["wall_s"]),
                "(aborted)" if r["outcome"] == "error" else "",
            )
        )
    total = summary["total"]
    rows.append(
        (
            "total",
            "",
            str(total["requests"]),
            f"{total['input_tokens']:,}",
            f"{total['output_tokens']:,}",
            str(total["reasoning_only_retries"]),
            "",
            format_duration(total["wall_s"]),
            "",
        )
    )
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    numeric = {2, 3, 4, 5, 6, 7}
    lines = [
        "  ".join(
            cell.rjust(widths[i]) if i in numeric else cell.ljust(widths[i])
            for i, cell in enumerate(row)
        ).rstrip()
        for row in rows
    ]
    return "\n".join(lines)


def format_context(summary: UsageSummary) -> str:
    """Per agent run with a context profile: the peak line and its full composition."""
    blocks: list[str] = []
    for r in summary["agents"]:
        profile = r["context"]
        if profile is None:
            continue
        lines = [f"{r['agent']} ({r['model']}): {describe_context(profile, top=0)}"]
        total = sum(profile["composition"].values()) or 1
        width = max((len(key) for key in profile["composition"]), default=0)
        for key, tokens in profile["composition"].items():
            lines.append(f"  {key.ljust(width)}  {tokens:>9,}  {100 * tokens / total:5.1f}%")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
