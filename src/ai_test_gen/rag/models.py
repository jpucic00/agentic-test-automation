"""Data model for the retrieval-memory knowledge base.

A ``KBRecord`` is one solved test case — written back by the pipeline after a
green run, or distilled offline from an existing corpus (Selenium suite,
hand-written Playwright specs, bare manual cases). The offline Distiller agent
reuses this model (minus the vector) as its structured-output schema, so every
field carries a ``description``.

Only ``intent_text`` is ever embedded. Everything else — including any original
source code — is stored payload that rides along with a search hit; the store
and retriever never vectorize code. RETRIEVAL_MEMORY_PLAN.md §3 is the schema
contract.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

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


class KBSelector(BaseModel):
    """One captured/extracted locator attached to a solved case.

    Selectors from the KB are HINTS for the Planner to verify live — never
    locators of record. They are excluded from Generator examples entirely.
    """

    kind: SelectorKind = Field(
        description="Resilience-ladder rung this locator uses (testid/role/label/text/css/xpath)"
    )
    value: str = Field(
        description="The locator exactly as captured or extracted, e.g. getByTestId('save') "
        "or By.cssSelector('.nav > li') — never invented"
    )
    description: str = Field(
        default="", description="What element this locator points at, in a few words"
    )
    route: str = Field(
        default="", description="Page/route where the element lives, if known (map name or path)"
    )


class KBRecord(BaseModel):
    """One solved test case in the knowledge base (RETRIEVAL_MEMORY_PLAN.md §3)."""

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
        description="The embedded text: title + condensed step intents + expected outcomes. "
        "English prose — this is what similarity search compares"
    )
    steps: list[str] = Field(
        default_factory=list,
        description="Ordered one-action step descriptions, plan-style granularity",
    )
    selectors: list[KBSelector] = Field(
        default_factory=list,
        description="Locators this case used, with their ladder kind — hints to verify live",
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
        description="Original imported source (test + resolved helper snippets, size-capped). "
        "Reference payload only — never embedded, never a Generator example",
    )
    source_lang: str = Field(
        default="", description="Language of source_code, e.g. 'java' or 'ts'; empty if none"
    )
    helper_refs: list[str] = Field(
        default_factory=list,
        description="Helper classes/methods the test uses (paths where resolved); entries "
        "prefixed 'unresolved:' when the seeding could not locate them",
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


def build_intent_text(title: str, steps: Sequence[str], expected: Sequence[str]) -> str:
    """The canonical embedded text for a case.

    Used by the green-run write-back AND as the query at retrieval time, so both
    sides of the similarity search compare like with like. The Distiller is
    prompted to produce the same shape (in the case's original language) for
    imported records.
    """
    parts = [title.strip()]
    step_text = " ".join(s.strip() for s in steps if s.strip())
    if step_text:
        parts.append(f"Steps: {step_text}")
    expected_text = " ".join(e.strip() for e in expected if e.strip())
    if expected_text:
        parts.append(f"Expected: {expected_text}")
    return "\n".join(p for p in parts if p)
