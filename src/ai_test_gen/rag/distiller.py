"""The agentic Distiller: one bounded repo exploration per discovered test (§5.3).

Offline seeding only — never part of the per-run loop. For each test that
``discover.py`` found, the Distiller reconstructs the plan the pipeline would have
produced: user-visible steps, verbatim locators WITH provenance citations, expected
outcomes only where the code proves them. The model does NOT author ``intent_text``
(§1.17 — that is code-built in ``seeding.py``), and its selector claims are
string-verified by ``verify.py`` after every call.

Two interchangeable ways to run the model live behind one seam (``DistillTurns``):

- **AgenticTurns** (default): a pydantic-ai agent with the shared read-only repo
  tools, exploration bounded by ``DISTILLER_REQUEST_LIMIT``. The verification
  bounce continues the SAME conversation (message history), so the agent re-checks
  with everything it already read in context.
- **TwoCallTurns** (``DISTILLER_MODE=two-call``): the degraded mode for a gateway
  that fails the tool-loop serving check (§1.12) — tool calls returned as text
  would kill every exploration. No tools cross the wire: call 1 asks the model
  WHICH files it needs (structured), the code reads them (through the same
  sandboxed tools, so instrumentation stays honest), call 2 distills from the
  supplied contents. The bounce is a third structured call over the same material.

Tests inject a fake ``DistillTurns``; ``distill_test`` orchestrates
first-call → verify → (one bounce) → verify and returns the final state.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from pydantic_ai import Agent, AgentRetries, capture_run_messages
from pydantic_ai.messages import ModelMessage
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from ..agents._context import agent_output_retries, agent_retries
from ..agents._run_failure import summarize_run_failure
from ..config import Config
from ..llm import build_openai_model
from ..models import ManualStep, ManualTestCase
from .discover import DiscoveredTest
from .models import (
    ExplorationTrace,
    ReconstructedPlan,
    ReconstructedSelector,
    ReconstructedStep,
)
from .tools import RepoTools
from .verify import VerifyPass, build_revalidation_message, verify_plan

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# One distill turn is bounded work — a hung or queued gateway must surface as an
# error within minutes, not dangle on the client library's 10-minute default.
_DISTILL_TIMEOUT_S = 240.0

# Degraded-mode caps: how many files call 1 may request and how much of their
# combined text call 2 may carry (the agentic mode is bounded by the request
# limit instead; reads are individually capped by the tools themselves).
_TWO_CALL_MAX_FILES = 12
_TWO_CALL_CHAR_CAP = 48_000


class DistillOutput(BaseModel):
    """The Distiller's structured output — the model-authored slice of a KB record.

    Everything else (ids, intent_text, provenance fields, source payload,
    timestamps) is attached in plain code by ``seeding.py``.
    """

    plan: ReconstructedPlan = Field(
        description="The reconstructed plan: ordered user-visible steps with verbatim, "
        "provenance-cited selectors; expected outcomes only where the code proves them"
    )
    kind: Literal["ui", "api", "db"] = Field(
        default="ui",
        description="What the test drives: ui = a browser, api = HTTP only, db = data checks",
    )
    routes: list[str] = Field(
        default_factory=list,
        description="Pages/paths the flow touches, as evidenced in the code (e.g. /login)",
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Calls/locators you could NOT resolve (opaque helpers, runtime-built "
        "values) — each kept as one opaque step in the plan and named here",
    )


class FileRequestList(BaseModel):
    """Call-1 output of the degraded two-call mode: which files to read."""

    paths: list[str] = Field(
        description=f"Corpus files (from the inventory) needed to reconstruct this test — "
        f"page objects, helpers, locator/resource files; at most {_TWO_CALL_MAX_FILES}"
    )


# --- the MODEL-facing draft schema -------------------------------------------------
# The stored contract (ReconstructedPlan, §1.18) uses `selector: X | None`, which
# serializes to an `anyOf [object, null]` union. Grammar-constrained serving stacks
# that handle scalar-or-null fine (PlanStep's `str | None` works on the same model)
# choke on object-or-null — live evidence: every Distiller turn on Gemma 4 came back
# as an empty husk (finish_reason=stop, ~30 tokens, no parts) while the union-free
# Mapper schema worked in the same session. So the model emits this DRAFT — no
# unions anywhere, "at most one" expressed as a list, `verified` absent entirely
# (verification is pipeline-owned) — and plain code maps it into the real types.


class DraftSelector(BaseModel):
    """A locator claim as the model states it — kind + verbatim value + citation."""

    kind: Literal["testid", "role", "label", "text", "css", "xpath"] = Field(
        description="Resilience-ladder rung this locator uses"
    )
    value: str = Field(
        description="The locator exactly as the source spells it — never invented or rewritten"
    )
    provenance: str = Field(
        description="Where it came from, as 'file#symbol' — a citation is mandatory"
    )


class DraftStep(BaseModel):
    """One reconstructed step; element locators ride in ≤1-item lists (no unions)."""

    action: str = Field(description="Imperative description of what the test does at this step")
    selectors: list[DraftSelector] = Field(
        default_factory=list,
        description="The locator of THE element this step acts on — at most one entry; "
        "an empty list for navigation/setup steps that touch no element",
    )
    expected: str = Field(
        default="",
        description="Expected outcome, only where the code proves it (an assert or explicit wait)",
    )
    assert_hints: list[DraftSelector] = Field(
        default_factory=list,
        description="Locator of an element that PROVES this step's expected outcome — at most "
        "one entry; empty when proven by URL or not asserted",
    )
    route: str = Field(default="", description="Page/route this step runs on, if known")
    source_ref: str = Field(
        default="", description="Which code produced this step, as 'file#symbol' when known"
    )


class DraftPlan(BaseModel):
    """The reconstructed plan as the model drafts it."""

    title: str = Field(description="Short human-readable title of the reconstructed test")
    start_route: str = Field(
        default="", description="Route/URL the flow starts on, if evident from the code"
    )
    steps: list[DraftStep] = Field(
        default_factory=list,
        description="Ordered reconstructed steps — the user-visible flow the code performs",
    )
    notes: str = Field(
        default="",
        description="Observations: opaque/unresolved calls, uncertainties, caveats",
    )


class DistillDraft(BaseModel):
    """What the model actually emits; mapped to :class:`DistillOutput` in code."""

    plan: DraftPlan
    kind: Literal["ui", "api", "db"] = Field(
        default="ui",
        description="What the test drives: ui = a browser, api = HTTP only, db = data checks",
    )
    routes: list[str] = Field(
        default_factory=list,
        description="Pages/paths the flow touches, as evidenced in the code (e.g. /login)",
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Calls/locators you could NOT resolve — each kept as one opaque step "
        "in the plan and named here",
    )


def _reconstructed(selector: DraftSelector) -> ReconstructedSelector:
    return ReconstructedSelector(
        kind=selector.kind, value=selector.value, provenance=selector.provenance
    )


def draft_to_output(draft: DistillDraft) -> DistillOutput:
    """Map the model's union-free draft onto the stored plan shape.

    A step listing more than one selector keeps the FIRST (a step acts on one
    element); the surplus is surfaced in the plan notes rather than silently
    dropped — the review file is the feedback loop that catches a drafting habit.
    """
    steps: list[ReconstructedStep] = []
    surplus: list[str] = []
    for index, step in enumerate(draft.plan.steps, 1):
        if len(step.selectors) > 1 or len(step.assert_hints) > 1:
            surplus.append(f"step {index} listed {len(step.selectors)} selectors / "
                           f"{len(step.assert_hints)} assert hints; kept the first of each")
        steps.append(
            ReconstructedStep(
                action=step.action,
                selector=_reconstructed(step.selectors[0]) if step.selectors else None,
                expected=step.expected,
                assert_hint=_reconstructed(step.assert_hints[0]) if step.assert_hints else None,
                route=step.route,
                source_ref=step.source_ref,
            )
        )
    notes = draft.plan.notes
    if surplus:
        notes = (notes + "\n" if notes else "") + "; ".join(surplus)
    return DistillOutput(
        plan=ReconstructedPlan(
            title=draft.plan.title,
            start_route=draft.plan.start_route,
            steps=steps,
            notes=notes,
        ),
        kind=draft.kind,
        routes=draft.routes,
        unresolved=draft.unresolved,
    )


class DistillTurns(Protocol):
    """One distillation conversation: a first turn and at most one revalidation turn."""

    async def first(self, message: str) -> DistillOutput: ...

    async def revalidate(self, message: str) -> DistillOutput: ...


@dataclass
class DistillResult:
    """Final state of one distilled test: output + verification + instrumentation."""

    output: DistillOutput
    verify: VerifyPass  # the FINAL pass (post-bounce when one ran)
    bounced_claims: int  # claims sent to the revalidation round; 0 = no bounce
    trace: ExplorationTrace


# --- message assembly ------------------------------------------------------------
def render_manual_triplets(steps: list[ManualStep]) -> str:
    """ManualStep rows as numbered action lines with Data/Expected sub-lines.

    Shared by the distill message and the review files: pipe-safe (no tables) and
    diffable, per §5.5. Empty cells are omitted, never rendered as blanks.
    """
    lines: list[str] = []
    for index, step in enumerate(steps, 1):
        lines.append(f"{index}. {step.action.strip() or '(no action text)'}")
        if step.data.strip():
            lines.append(f"   - Data: {step.data.strip()}")
        if step.expected.strip():
            lines.append(f"   - Expected: {step.expected.strip()}")
    return "\n".join(lines) if lines else "(no steps)"


def build_distill_message(
    test: DiscoveredTest,
    address: str,
    case: ManualTestCase | None,
    map_index: str,
    suite_block: str,
) -> str:
    """The one user message for a test: source + manual triplets + map digest."""
    flavor = "Selenium/Java" if test.language == "java" else "Playwright/TypeScript"
    scope_note = (
        f"The snippet is the TEST METHOD only — read `{address}` for its class, "
        "@Before* lifecycle and imports before reconstructing."
        if test.language == "java"
        else "The snippet is the complete spec file."
    )
    parts = [
        f"Reconstruct this {flavor} test into a plan-shaped record.",
        f"## Test `{test.symbol}` — file `{address}`"
        + (f" — linked case {test.xray_key}" if test.xray_key else " (unlinked)")
        + f"\n{scope_note}\n```{test.language}\n{test.code}\n```",
    ]
    if case is not None:
        parts.append(
            f"## Linked manual case {case.key}: {case.title}\n"
            "Rough intent skeleton — trust the CODE for detail:\n"
            f"{render_manual_triplets(case.steps)}"
        )
    else:
        parts.append(
            "## Linked manual case\n(none available — reconstruct from the code alone)"
        )
    if map_index.strip():
        parts.append(f"## Suite map — at a glance\n{map_index.strip()}")
    if suite_block.strip():
        parts.append(f"## This test's suite\n{suite_block.strip()}")
    return "\n\n".join(parts)


# --- the two live turn implementations --------------------------------------------
def _system_prompt() -> str:
    return (PROMPTS_DIR / "distiller.md").read_text()


def seeding_model_settings(config: Config) -> ModelSettings | None:
    """Per-request extras for the seeding agents (Mapper + Distiller).

    ``DISTILLER_EXTRA_BODY`` merges an arbitrary JSON object into every request —
    the serving escape hatch (pin a provider on a load-balanced gateway, pass vLLM
    ``chat_template_kwargs``). None when unset, so default requests are untouched.
    """
    if config.distiller_extra_body is None:
        return None
    return ModelSettings(extra_body=config.distiller_extra_body)


class AgenticTurns:
    """Production turns: one repo-exploring agent, bounce continues its conversation.

    The agent emits the union-free :class:`DistillDraft` (see above) and each turn
    maps it to :class:`DistillOutput` in code.
    """

    def __init__(self, config: Config, tools: RepoTools) -> None:
        model = build_openai_model(config, config.distiller_model, timeout_s=_DISTILL_TIMEOUT_S)
        self._agent: Agent[None, DistillDraft] = Agent(
            model=model,
            output_type=DistillDraft,
            system_prompt=_system_prompt(),
            model_settings=seeding_model_settings(config),
            retries=AgentRetries(tools=agent_retries(), output=agent_output_retries()),
        )
        tools.register(self._agent)
        self._limit = config.distiller_request_limit
        self._history: list[ModelMessage] | None = None

    async def first(self, message: str) -> DistillOutput:
        return draft_to_output(await self._run(message, history=None))

    async def revalidate(self, message: str) -> DistillOutput:
        return draft_to_output(await self._run(message, history=self._history))

    async def _run(self, message: str, history: list[ModelMessage] | None) -> DistillDraft:
        # Each turn gets the full DISTILLER_REQUEST_LIMIT budget (a bounced distill may
        # spend up to 2×) — a revalidation that cannot re-read anything would be forced
        # to guess, which is the one thing the bounce exists to prevent.
        with capture_run_messages() as messages:
            try:
                async with self._agent:
                    result = await self._agent.run(
                        message,
                        message_history=history,
                        usage_limits=UsageLimits(request_limit=self._limit),
                    )
            except BaseException as exc:
                # Same evidence contract as run_agent_logged: nothing leaves a model
                # run without WHAT the model emitted being in the log. Re-raised —
                # the seeding loop's per-test isolation does the skipping/counting.
                logger.error(
                    "Distiller run aborted: %r\n%s",
                    exc,
                    summarize_run_failure(exc, messages),
                )
                raise
        self._history = result.all_messages()
        return result.output


class TwoCallTurns:
    """Degraded mode (§1.12): request files → code reads them → distill. No tools.

    ``run_request``/``run_distill`` are injectable for tests; unset, each is one
    structured pydantic-ai call (no toolset) against ``DISTILLER_MODEL``.
    """

    def __init__(
        self,
        config: Config,
        tools: RepoTools,
        *,
        run_request: Callable[[str], Awaitable[FileRequestList]] | None = None,
        run_distill: Callable[[str], Awaitable[DistillOutput]] | None = None,
    ) -> None:
        self._tools = tools
        self._run_request = run_request or _structured_call(config, FileRequestList)
        self._run_distill = run_distill or _draft_distill_call(config)
        self._base_message = ""
        self._file_block = ""

    async def first(self, message: str) -> DistillOutput:
        self._base_message = message
        inventory = "\n".join(self._tools.inventory())
        request = await self._run_request(
            f"{message}\n\n## Repository files\n{inventory}\n\n"
            f"List the files (at most {_TWO_CALL_MAX_FILES}, exactly as shown above) you "
            "need to reconstruct this test — the page objects, helpers and locator/resource "
            "files its execution path reaches."
        )
        self._file_block = self._read_files(request.paths)
        return await self._run_distill(
            f"{message}\n\n{self._file_block}\n\nReconstruct the test from the sources above."
        )

    async def revalidate(self, message: str) -> DistillOutput:
        return await self._run_distill(
            f"{self._base_message}\n\n{self._file_block}\n\n{message}"
        )

    def _read_files(self, paths: list[str]) -> str:
        blocks: list[str] = []
        total = 0
        for path in paths[:_TWO_CALL_MAX_FILES]:
            text = self._tools.read_file(path)  # sandboxed + honest instrumentation
            if total + len(text) > _TWO_CALL_CHAR_CAP:
                blocks.append(f"…[{path}: omitted — file budget reached]")
                break
            total += len(text)
            blocks.append(text)
        return "## Requested sources\n" + ("\n\n".join(blocks) if blocks else "(none readable)")


def _draft_distill_call(config: Config) -> Callable[[str], Awaitable[DistillOutput]]:
    """The two-call mode's live distill call: emit the union-free draft, map in code."""
    call = _structured_call(config, DistillDraft)

    async def run(message: str) -> DistillOutput:
        return draft_to_output(await call(message))

    return run


def _structured_call[OutputT: BaseModel](
    config: Config, output_type: type[OutputT]
) -> Callable[[str], Awaitable[OutputT]]:
    """One no-tools structured completion against the distiller model."""
    model = build_openai_model(config, config.distiller_model, timeout_s=_DISTILL_TIMEOUT_S)
    agent: Agent[None, OutputT] = Agent(
        model=model,
        output_type=output_type,
        system_prompt=_system_prompt(),
        model_settings=seeding_model_settings(config),
        retries=AgentRetries(tools=agent_retries(), output=agent_output_retries()),
    )

    async def run(message: str) -> OutputT:
        with capture_run_messages() as messages:
            try:
                result = await agent.run(message)
            except BaseException as exc:
                logger.error(
                    "Distiller (two-call) run aborted: %r\n%s",
                    exc,
                    summarize_run_failure(exc, messages),
                )
                raise
        return result.output

    return run


def build_turns(config: Config, tools: RepoTools) -> DistillTurns:
    """The live turns for ``config.distiller_mode`` (the seeding loop's default)."""
    if config.distiller_mode == "two-call":
        return TwoCallTurns(config, tools)
    return AgenticTurns(config, tools)


# --- orchestration -----------------------------------------------------------------
async def distill_test(
    config: Config,
    tools: RepoTools,
    test: DiscoveredTest,
    case: ManualTestCase | None,
    *,
    address: str,
    map_index: str = "",
    suite_block: str = "",
    turns: DistillTurns | None = None,
    text_cache: dict[str, str] | None = None,
) -> DistillResult:
    """Distill ONE test: first call → verify → at most one bounce → verify.

    The returned plan has every selector's ``verified`` flag set and citations
    auto-fixed where the value was found elsewhere; claims still unverified after
    the bounce ship flagged (``verified=False``) — never dropped (§1.14).
    """
    live = turns if turns is not None else build_turns(config, tools)
    message = build_distill_message(test, address, case, map_index, suite_block)
    output = await live.first(message)
    outcome = verify_plan(output.plan, tools, text_cache=text_cache)
    bounced = 0
    if outcome.unverified:
        bounced = len(outcome.unverified)
        logger.info(
            ":: %s — %d claim(s) failed verification; one revalidation round",
            test.ref,
            bounced,
        )
        output = await live.revalidate(build_revalidation_message(outcome.unverified))
        outcome = verify_plan(output.plan, tools, text_cache=text_cache)
        if outcome.unverified:
            logger.warning(
                ":: %s — %d claim(s) remain unverified after the bounce (flagged, kept)",
                test.ref,
                len(outcome.unverified),
            )
    trace = ExplorationTrace(
        files_opened=sorted(tools.files_opened),
        tool_calls=tools.tool_calls,
        selectors_cited=outcome.cited,
        selectors_verified=outcome.verified,
        selectors_unverified=len(outcome.unverified),
        unresolved=[u.strip() for u in output.unresolved if u.strip()],
    )
    return DistillResult(output=output, verify=outcome, bounced_claims=bounced, trace=trace)
