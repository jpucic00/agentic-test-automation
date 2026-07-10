"""Data model for the retrieval-memory knowledge base (v2).

A ``KBRecord`` is one solved test case — written back by the pipeline after a
green run, or distilled offline from an existing corpus (Selenium suite,
hand-written Playwright specs, bare manual cases). RETRIEVAL_MEMORY_PLAN.md §3 is
the schema contract; this file is the v2 rework (2026-07-09): the record is now
**plan-shaped** (a ``ReconstructedPlan`` replaces the old prose ``steps`` + flat
``selectors``) and the manual snapshot is a list of ``ManualStep`` triplets.

Only ``intent_text`` is ever embedded, and it is **code-built** (§1.17) from the
manual case via ``build_intent_text`` — the SAME builder the runtime query uses,
so both sides of the similarity search compare like with like; the model never
authors it. Everything else — including any original source code — is stored
payload that rides along with a search hit; the store and retriever never
vectorize code.

**Type-boundary rule (§1.18).** ``ReconstructedPlan``/``ReconstructedStep`` are
DISTINCT from the Planner's ``TestPlan``/``PlanStep`` on purpose. ``PlanStep``'s
schema descriptions promise selectors that were "captured + verified live", and
Pydantic AI serializes those descriptions into the model-facing schema — a
legacy record wearing that type would lie in its own schema. A reconstructed
selector is instead ``{kind, value, provenance, verified}``: knowledge with a
citation and a verification flag, advisory until the Planner re-checks it live.

**Write-back mapping (§3).** A green run stores its live-verified ``TestPlan``
mapped INTO ``ReconstructedPlan`` shape with ``verified=True`` and
``provenance="pipeline:<date>"`` — the boundary is crossed only in the safe
direction (verified live → reconstructed). The reverse (Reconstructed → TestPlan)
must always pass live verification by construction; nothing here shortcuts it.
The retrieval preference rule is unchanged: a ``pipeline`` record supersedes a
legacy record sharing its ``xray_key``. (The mapping itself is wired in the
run-loop integration task; this module only defines the shapes it targets.)
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from ..models import ManualStep

# Where a record came from. Provenance drives the retrieval rules: only
# `pipeline` / `playwright-import` records may serve as Generator code examples
# (a mined Selenium test is knowledge, never style), and a `pipeline` record
# supersedes a legacy record that shares its xray_key.
KBSource = Literal["pipeline", "selenium-import", "playwright-import", "manual"]

# `green` = pipeline-verified against the live app; `legacy` = imported and
# assumed-was-passing in its old suite; `unknown` = no execution evidence.
KBOutcome = Literal["green", "legacy", "unknown"]

# Rung on the selector resilience ladder (id > accessible > CSS > XPath).
SelectorKind = Literal["testid", "role", "label", "text", "css", "xpath"]

# What kind of test a record captures. Only `ui` records render as Planner
# selector hints (§3); `api`/`db` are knowledge without a browser surface, and
# `knowledge` is a distilled map lifecycle/conventions note.
KBKind = Literal["ui", "api", "db", "knowledge"]


class ReconstructedSelector(BaseModel):
    """A locator recovered from imported code — a HINT, never a locator of record.

    Kept verbatim in its source form (e.g. Selenium ``By.id("save")``); the
    Planner still captures/verifies its own live before recording it. Provenance
    is a hard requirement (§1.14): a selector with no citation cannot be trusted.
    """

    kind: SelectorKind = Field(
        description="Resilience-ladder rung this locator uses (testid/role/label/text/css/xpath)"
    )
    value: str = Field(
        description="The locator exactly as captured or extracted, e.g. getByTestId('save') "
        "or By.cssSelector('.nav > li') — never invented, never rewritten"
    )
    provenance: str = Field(
        description="Where this locator came from as 'file#symbol' (the cited source location), "
        "or 'pipeline:<date>' for a live-verified write-back — a citation is mandatory"
    )
    verified: bool = Field(
        default=False,
        description="True once string-checked against its cited source (or captured live on a "
        "green run); False = flagged unverified, kept but never trusted blindly",
    )


class ReconstructedStep(BaseModel):
    """One step of a reconstructed plan — parallel to the Planner's PlanStep.

    Values are embedded in the ``action`` text (the PlanStep convention); the
    ``selector`` is the element acted on, ``assert_hint`` an element that would
    prove ``expected``. ``expected`` is filled ONLY where the code proves it (an
    assertion or an explicit wait) — never guessed.
    """

    action: str = Field(description="Imperative description of what the test does at this step")
    selector: ReconstructedSelector | None = Field(
        default=None,
        description="Locator for the element this step acts on, if any; None for "
        "navigation/setup steps that touch no element",
    )
    expected: str = Field(
        default="",
        description="Expected outcome, only where the code proves it (an assert or explicit wait)",
    )
    assert_hint: ReconstructedSelector | None = Field(
        default=None,
        description="Locator of an element that PROVES this step's expected outcome, if the "
        "code asserts on one; None when proven by URL or not asserted",
    )
    route: str = Field(
        default="", description="Page/route this step runs on, if known (map name or path)"
    )
    source_ref: str = Field(
        default="", description="Which code produced this step, as 'file#symbol' when known"
    )


class ReconstructedPlan(BaseModel):
    """The code view of an imported test — the plan a run WOULD have produced.

    Structurally parallel to ``TestPlan`` but built from static/agentic reading
    of the source, so its selectors are advisory hints (see the module docstring).
    """

    title: str = Field(description="Short human-readable title of the reconstructed test")
    start_route: str = Field(
        default="", description="Route/URL the flow starts on, if evident from the code"
    )
    steps: list[ReconstructedStep] = Field(
        default_factory=list,
        description="Ordered reconstructed steps — the user-visible flow the code performs",
    )
    notes: str = Field(
        default="",
        description="Distiller observations: opaque/unresolved calls, uncertainties, caveats",
    )


class ExplorationTrace(BaseModel):
    """Per-record seeding instrumentation (§3/§5.4) — REPLACES the old helper_refs.

    Records how the distiller reached this plan so escalation signals (selectorless
    ui-records, low files-opened, self-reported unresolved) can flag records for
    re-distillation without re-reading every review file.
    """

    files_opened: list[str] = Field(
        default_factory=list, description="Repo files the distiller read to reconstruct the plan"
    )
    tool_calls: int = Field(default=0, description="Tool calls the distiller spent on this test")
    selectors_cited: int = Field(default=0, description="Selectors the distiller emitted")
    selectors_verified: int = Field(
        default=0, description="Of those, ones string-verified at their cited location"
    )
    selectors_unverified: int = Field(
        default=0, description="Cited selectors that survived the bounce loop still unverified"
    )
    unresolved: list[str] = Field(
        default_factory=list,
        description="Calls/locators the distiller could not resolve — opaque steps, kept + flagged",
    )


class KBRecord(BaseModel):
    """One solved test case in the knowledge base (RETRIEVAL_MEMORY_PLAN.md §3, v2)."""

    record_id: str = Field(
        description="Stable UUID from make_record_id(project_key, source, ref) — "
        "re-seeding the same input upserts, never duplicates"
    )
    project_key: str = Field(
        description="Jira project key, e.g. 'QA' — routes the record to collection kb_<key>"
    )
    xray_key: str = Field(
        default="",
        description="Jira/Xray issue key (e.g. 'QA-123') when known; empty if unlinked",
    )
    title: str = Field(description="Short human-readable title of the test case")
    intent_text: str = Field(
        description="The embedded text: title + step actions + expected outcomes, code-built "
        "in the case's ORIGINAL language via build_intent_text — what similarity search compares"
    )
    plan: ReconstructedPlan = Field(
        description="The reconstructed code view (actions + advisory selectors + expected/route) "
        "— REPLACES the old prose steps + flat selectors; hints derive their selectors from it"
    )
    manual_steps: list[ManualStep] = Field(
        default_factory=list,
        description="VERBATIM ManualStep snapshot of the linked case as it read at "
        "distillation/write-back time — the ticket view + diff base for later ticket edits; "
        "empty when no case was available",
    )
    kind: KBKind = Field(
        default="ui",
        description="ui | api | db | knowledge — only 'ui' records render as Planner hints",
    )
    routes: list[str] = Field(
        default_factory=list, description="Pages/flows the case touches (map names or paths)"
    )
    spec: str = Field(
        default="",
        description="Playwright spec code (or repo-relative ref). Empty for selenium/manual "
        "sources. The ONLY field eligible as a Generator few-shot example",
    )
    source_code: str = Field(
        default="",
        description="Original imported source (size-capped). Reference payload only — never "
        "embedded, never a Generator example",
    )
    source_lang: str = Field(
        default="", description="Language of source_code, e.g. 'java' or 'ts'; empty if none"
    )
    explored: ExplorationTrace = Field(
        default_factory=ExplorationTrace,
        description="Seeding instrumentation (files opened, tool calls, selector/verify counts, "
        "unresolved) — REPLACES helper_refs",
    )
    outcome: KBOutcome = Field(
        description="green = pipeline-verified · legacy = imported, assumed-was-passing · unknown"
    )
    source: KBSource = Field(
        description="Provenance: pipeline | selenium-import | playwright-import | manual"
    )
    context_hash: str = Field(
        default="",
        description="For pipeline records: the run's project-context hash (staleness audit)",
    )
    created_at: str = Field(default="", description="ISO-8601 creation timestamp")
    updated_at: str = Field(default="", description="ISO-8601 last-update timestamp")


def make_record_id(project_key: str, source: KBSource, ref: str) -> str:
    """Deterministic record id: UUIDv5 over (project_key, source, ref).

    ``ref`` is the Xray key when the record is linked, else the repo-relative
    source path. Deterministic ids make seeding idempotent (same input → same
    point, upserted not duplicated) — and UUIDs are one of the two point-id
    types Qdrant accepts.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kb|{project_key}|{source}|{ref}"))


def project_key_of(issue_key: str) -> str:
    """``'QA-123' → 'QA'`` — the collection-routing key. A key without a dash is
    returned whole. Uppercased to match ``collection_name`` normalization."""
    return issue_key.split("-", 1)[0].strip().upper()


def build_intent_text(title: str, steps: Sequence[ManualStep]) -> str:
    """The canonical embedded text for a case — code-built (§1.17).

    Used by the green-run write-back AND as the query at retrieval time, so both
    sides of the similarity search compare like with like. Built from the manual
    case verbatim; the per-step ``data`` cell is EXCLUDED (login boilerplate
    deflates discrimination and unique values add noise), and no translation is
    applied — the query arrives in the same language, so like compares with like.

    The OUTPUT shape (title, then ``Steps: …``, then ``Expected: …``) is
    unchanged from v1, so the v2 restructure forces no re-embedding on its own.
    """
    parts = [title.strip()]
    step_text = " ".join(s.action.strip() for s in steps if s.action.strip())
    if step_text:
        parts.append(f"Steps: {step_text}")
    expected_text = " ".join(s.expected.strip() for s in steps if s.expected.strip())
    if expected_text:
        parts.append(f"Expected: {expected_text}")
    return "\n".join(p for p in parts if p)
