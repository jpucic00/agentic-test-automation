"""Hold the Planner's selectors to text the live page actually showed during the run.

The Planner reads the page and records locators, but it retypes their text — and gpt-oss
straightens the page's typographic quotes on the way (``“New note”`` → ``"New note"``). A plan
with the wrong characters poisons everything downstream: the Generator must copy it verbatim
(``spec_literals.py``) and the Healer repeats it. This output validator checks every text inside
a plan selector against the run's evidence of what the page showed:

- the text of every tool result in the run (``browser_snapshot`` / ``browser_find`` trees,
  ``browser_generate_locator`` output) — minus anything that merely echoes that call's own
  arguments, since ``count_matches`` and friends repeat the query they were given (a 2026-09-30
  plan recorded ``getByText('unique new-user email per the')``, a phrase from the Planner's own
  prompt, and the echo let it pass),
- the values the Planner typed into the page (``browser_type`` / ``browser_fill_form`` /
  ``browser_select_option`` arguments — a note title it created is real page text afterwards),
- the snapshot files Playwright MCP wrote to ``output/snapshots/`` during this run (the folder is
  emptied when a run starts), which survive history trimming.

A literal found verbatim passes. One that matches only after folding typographic quotes,
non-breaking spaces, ellipses and spacing is rewritten to the page's exact characters — no model
retry. One found nowhere goes back to the Planner as a retry naming the step; after
``MAX_BOUNCES`` such retries the plan is accepted with a WARNING instead, so the check can never
be what exhausts the run's output-retry budget. It proves the text existed on the page during the
run — not that the locator is unique (that is ``count_matches``' job). Regex literals and raw
``locator('css=…' / 'xpath=…')`` selectors are not checked: page text can't evidence them, and
their proof is ``count_matches``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart

from ...core.models import TestPlan
from .history import content_text
from .spec_literals import TYPOGRAPHIC, LocatorLiteral, fold_text, locator_literals, quote_literal

logger = logging.getLogger(__name__)

TYPING_TOOLS = frozenset({"browser_type", "browser_fill_form", "browser_select_option"})
MAX_BOUNCES = 2
MAX_REPORTED = 6


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def run_evidence(messages: Iterable[ModelMessage], snapshots_dir: Path | None) -> list[str]:
    """Every piece of text the page showed (or was given) during the run."""
    messages = list(messages)
    call_args = {
        part.tool_call_id: sorted(set(_strings(part.args_as_dict())), key=len, reverse=True)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }
    texts: list[str] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolReturnPart):
                text = content_text(part.content) or ""
                for arg in call_args.get(part.tool_call_id, []):
                    if arg:
                        text = text.replace(arg, "\n")
                texts.append(text)
            elif isinstance(part, ToolCallPart) and part.tool_name in TYPING_TOOLS:
                texts.extend(_strings(part.args_as_dict()))
    if snapshots_dir is not None and snapshots_dir.is_dir():
        texts.extend(p.read_text(errors="replace") for p in sorted(snapshots_dir.glob("*.yml")))
    return [t for t in texts if t]


class Evidence:
    """Searchable run evidence: verbatim, and folded with a map back to the original text."""

    def __init__(self, texts: Iterable[str]) -> None:
        raw = "\n".join(texts)
        # Snapshot YAML and JS locators escape quotes inside names; search the unescaped form too.
        self._unescaped = raw.replace('\\"', '"').replace("\\'", "'")
        self._raw = raw
        self._folded: tuple[str, list[int]] | None = None

    def contains(self, value: str) -> bool:
        return value in self._raw or value in self._unescaped

    def find_folded(self, value: str) -> str | None:
        """The page's exact text for ``value`` when they differ only by folding, else None."""
        target = fold_text(value)
        if not target:
            return None
        folded, origin = self._fold_index()
        at = folded.find(target)
        if at < 0:
            return None
        return self._unescaped[origin[at] : origin[at + len(target) - 1] + 1]

    def _fold_index(self) -> tuple[str, list[int]]:
        if self._folded is None:
            chars: list[str] = []
            origin: list[int] = []
            for i, ch in enumerate(self._unescaped):
                plain = ch.translate(TYPOGRAPHIC)
                if plain.isspace():
                    if chars and chars[-1] != " ":
                        chars.append(" ")
                        origin.append(i)
                    continue
                for c in plain:
                    chars.append(c)
                    origin.append(i)
            self._folded = ("".join(chars), origin)
        return self._folded


Unverified = tuple[int, str, LocatorLiteral]  # (1-based step number, field name, literal)


def check_plan_literals(
    plan: TestPlan, evidence: Evidence
) -> tuple[TestPlan, int, list[Unverified]]:
    """Repair drifted selector text; return ``(plan, repaired_count, unverified)``."""
    repaired = 0
    unverified: list[Unverified] = []
    steps = []
    for number, step in enumerate(plan.steps, start=1):
        updates: dict[str, str] = {}
        for field in ("target_selector", "assert_selector"):
            selector = getattr(step, field)
            if not selector:
                continue
            fixed = selector
            for lit in sorted(locator_literals(selector), key=lambda x: x.start, reverse=True):
                if not lit.quote or lit.method == "locator" or evidence.contains(lit.value):
                    continue
                page_text = evidence.find_folded(lit.value)
                if page_text is None:
                    unverified.append((number, field, lit))
                    continue
                fixed = fixed[: lit.start] + quote_literal(page_text, lit.quote) + fixed[lit.end :]
                repaired += 1
            if fixed != selector:
                updates[field] = fixed
        steps.append(step.model_copy(update=updates) if updates else step)
    unverified.sort(key=lambda u: (u[0], u[1], u[2].start))
    return plan.model_copy(update={"steps": steps}), repaired, unverified


def bounce_message(unverified: list[Unverified]) -> str:
    lines = [
        f"- step {n} `{field}`: `{lit.call}` — {lit.value!r} never appeared on the page"
        for n, field, lit in unverified[:MAX_REPORTED]
    ]
    if len(unverified) > MAX_REPORTED:
        lines.append(f"- … and {len(unverified) - MAX_REPORTED} more")
    return (
        "These selectors contain text that no page showed during this run — no snapshot, "
        "locator or tool result contains it:\n"
        + "\n".join(lines)
        + "\nCapture each one live with browser_generate_locator and copy its text exactly, "
        "or leave the field empty if nothing on the page proves it. Keep the rest of the plan "
        "as it is and return the full TestPlan again."
    )


def plan_evidence_validator(
    snapshots_dir: Path | None,
) -> Callable[[RunContext[None], TestPlan], TestPlan]:
    """An output validator binding the Planner's selectors to the run's live page text."""
    bounces = 0

    def validate(ctx: RunContext[None], plan: TestPlan) -> TestPlan:
        nonlocal bounces
        evidence = Evidence(run_evidence(ctx.messages, snapshots_dir))
        fixed, repaired, unverified = check_plan_literals(plan, evidence)
        if repaired:
            logger.info(
                "Planner: restored %d selector text(s) to the page's exact characters "
                "(typographic quotes / spacing)",
                repaired,
            )
        if unverified:
            names = ", ".join(repr(lit.value) for _, _, lit in unverified[:MAX_REPORTED])
            if bounces < MAX_BOUNCES:
                bounces += 1
                logger.info(
                    "Planner recorded %d selector text(s) no page showed (%s) — asking it to "
                    "capture them live (%d/%d)",
                    len(unverified),
                    names,
                    bounces,
                    MAX_BOUNCES,
                )
                raise ModelRetry(bounce_message(unverified))
            logger.warning(
                "Planner still records %d selector text(s) no page showed (%s) after %d "
                "retries — accepting the plan; the run will show whether they resolve",
                len(unverified),
                names,
                MAX_BOUNCES,
            )
        return fixed

    return validate
