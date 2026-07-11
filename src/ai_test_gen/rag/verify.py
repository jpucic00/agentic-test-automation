"""Bounce verification for distilled plans (RETRIEVAL_MEMORY_PLAN.md §1.14/§5.4).

KB selectors are advisory (the Planner re-verifies live), so the failure mode to
prevent is not "occasionally wrong" — it is "plausible and uncheckable". Every
selector/assert-hint claim therefore cites provenance (``file#symbol``), and this
module string-checks each claim in plain code:

- fragments found in the cited file            → ``verified=True``
- fragments found in exactly one OTHER file    → citation AUTO-FIXED, ``verified=True``
- found nowhere                                → the claim joins the bounce list; the
  caller sends ONE bounded revalidation round back to the agent ("these do not exist
  at the cited locations — recheck or remove"); whatever survives ships
  ``verified=False`` — flagged, never silently dropped, never silently kept.

**What is checked.** A locator value is matched by its *literal fragments*: the
quoted strings inside it (``By.id("login-email")`` → ``login-email``). A template
value assembled at runtime (``"//li[.//h3[text()='" + title + "']]"``) is matched by
its static skeleton parts — each quoted fragment individually. A value with no
quotes is matched whole. The check is a plain substring scan of the cited file, so
it can never hallucinate; symbol names inside a citation are informational only.

Escalation signals (§1.13) also live here: they are derived from the agent's OUTPUT
(never from parsing the corpus) and mark records for re-distillation in the seeding
summary — a nudge list, not a gate.
"""
from __future__ import annotations

import re
from collections.abc import MutableMapping
from dataclasses import dataclass, field

from ..models import ManualStep
from .models import ReconstructedPlan, ReconstructedSelector
from .tools import RepoTools

# Fragments shorter than this match everywhere and prove nothing ("a", "/", "1").
_MIN_FRAGMENT_LEN = 2

_DOUBLE_QUOTED = re.compile(r'"([^"]*)"')
_SINGLE_QUOTED = re.compile(r"'([^']*)'")


def literal_fragments(value: str) -> list[str]:
    """The checkable literal parts of a locator value.

    Double-quoted fragments win (they are the outer literals in Java/TS source and
    may legally contain single quotes, e.g. XPath ``[text()='Save']``); single-quoted
    fragments are the fallback; a value with no usable quoted literal is checked
    whole. Order-preserving, de-duplicated, too-short fragments dropped.
    """
    text = value.strip()
    for pattern in (_DOUBLE_QUOTED, _SINGLE_QUOTED):
        fragments = [f.strip() for f in pattern.findall(text)]
        fragments = list(dict.fromkeys(f for f in fragments if len(f) >= _MIN_FRAGMENT_LEN))
        if fragments:
            return fragments
    return [text] if text else []


@dataclass
class Claim:
    """One provenance-carrying selector occurrence inside a reconstructed plan."""

    step_index: int  # 0-based position in plan.steps
    slot: str  # "selector" | "assert_hint"
    selector: ReconstructedSelector

    def describe(self) -> str:
        return (
            f"step {self.step_index + 1} {self.slot}: {self.selector.kind} "
            f"`{self.selector.value}` cited at `{self.selector.provenance or '(no citation)'}`"
        )


@dataclass
class VerifyPass:
    """Outcome of one string-check pass over a plan (the plan is mutated in place)."""

    cited: int = 0
    verified: int = 0
    auto_fixed: list[str] = field(default_factory=list)  # "value: old-citation → new-file"
    unverified: list[Claim] = field(default_factory=list)  # candidates for the bounce round


def collect_claims(plan: ReconstructedPlan) -> list[Claim]:
    """Every selector/assert-hint in the plan, in step order."""
    claims: list[Claim] = []
    for index, step in enumerate(plan.steps):
        if step.selector is not None:
            claims.append(Claim(index, "selector", step.selector))
        if step.assert_hint is not None:
            claims.append(Claim(index, "assert_hint", step.assert_hint))
    return claims


def verify_plan(
    plan: ReconstructedPlan,
    tools: RepoTools,
    *,
    text_cache: MutableMapping[str, str] | None = None,
) -> VerifyPass:
    """String-check every claim; set ``verified`` flags and auto-fix citations in place.

    ``text_cache`` (address → file text) is shared across records by the seeding run so
    the corpus is read once, not once per record. Reads go through ``resolve_citation``
    (sandboxed) but never through the agent tools — code checks must not pollute the
    agent's exploration instrumentation.
    """
    cache: MutableMapping[str, str] = text_cache if text_cache is not None else {}
    outcome = VerifyPass()
    for claim in collect_claims(plan):
        outcome.cited += 1
        selector = claim.selector
        fragments = literal_fragments(selector.value)
        if not fragments:
            selector.verified = False
            outcome.unverified.append(claim)
            continue
        if _fragments_in_file(fragments, selector.provenance, tools, cache):
            selector.verified = True
            outcome.verified += 1
            continue
        rehomed = _find_home(fragments, tools, cache)
        if rehomed is not None:
            outcome.auto_fixed.append(
                f"{selector.value}: `{selector.provenance or '(no citation)'}` → `{rehomed}`"
            )
            selector.provenance = rehomed
            selector.verified = True
            outcome.verified += 1
            continue
        selector.verified = False
        outcome.unverified.append(claim)
    return outcome


def build_revalidation_message(claims: list[Claim]) -> str:
    """The ONE bounded bounce round sent back to the agent (§5.4)."""
    lines = "\n".join(f"- {claim.describe()}" for claim in claims)
    return f"""Verification failed for {len(claims)} claim(s) in your plan — the quoted \
locator value(s) do not exist at the cited location(s), nor anywhere else in the corpus:

{lines}

Re-check each one against the actual source. For every failed claim either fix the value \
(copy it VERBATIM from the file), fix the citation to the file that really contains it, or \
remove the claim entirely if you cannot find it. Do not invent values. Then return the \
complete corrected output (all steps, not just the fixed ones)."""


def escalation_signals(
    plan: ReconstructedPlan,
    kind: str,
    manual_steps: list[ManualStep],
    files_opened: int,
    unresolved: list[str],
) -> list[str]:
    """Agent-output-derived signals that mark a record for re-distillation (§1.13).

    Advisory: they aggregate in the seeding summary as a re-distillation nudge list
    (re-run those ids with ``--force``, a steer, or a stronger model) — never a gate.
    """
    signals: list[str] = []
    has_selector = any(s.selector is not None or s.assert_hint is not None for s in plan.steps)
    if kind == "ui" and not has_selector:
        signals.append("selectorless-ui")
    if manual_steps and len(plan.steps) < len(manual_steps):
        signals.append(
            f"shallow-plan ({len(plan.steps)} step(s) vs {len(manual_steps)} manual)"
        )
    if files_opened == 0:
        signals.append("no-files-opened")
    if unresolved:
        signals.append(f"self-reported-unresolved ({len(unresolved)})")
    return signals


# --- internals -----------------------------------------------------------------
def _file_text(
    address: str, tools: RepoTools, cache: MutableMapping[str, str]
) -> str | None:
    if address in cache:
        return cache[address]
    path = tools.resolve_citation(address)
    if path is None:
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    cache[address] = text
    return text


def _fragments_in_file(
    fragments: list[str],
    citation: str,
    tools: RepoTools,
    cache: MutableMapping[str, str],
) -> bool:
    text = _file_text(citation, tools, cache)
    return text is not None and all(fragment in text for fragment in fragments)


def _find_home(
    fragments: list[str], tools: RepoTools, cache: MutableMapping[str, str]
) -> str | None:
    """The corpus file that actually contains ALL fragments, or None.

    Candidates are deterministic (inventory order); when several files qualify the
    first sorted address wins — an auto-fix must be reproducible, and any candidate
    is a strictly better citation than one that verifiably contains nothing.
    """
    for address in tools.inventory():
        text = _file_text(address, tools, cache)
        if text is not None and all(fragment in text for fragment in fragments):
            return address
    return None
