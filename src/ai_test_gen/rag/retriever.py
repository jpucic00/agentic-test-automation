"""Retrieval: embed → per-project vector search → rerank → top-K context blocks.

The only consumer-facing entry point is ``retrieve()``. It is **fail-open by
contract** (RETRIEVAL_MEMORY_PLAN.md §7): any failure — store, embeddings,
rerank, malformed payloads — logs a WARNING and returns an EMPTY
``RetrievedContext``, so a run with RAG on and infrastructure down behaves
exactly like an unassisted run. It never raises to the caller.

Provenance rules (plan §1.6):
- ``planner_hints`` may draw on ANY source — selectors are framed as hints to
  verify live, never locators of record.
- ``generator_examples`` only ever contains Playwright-sourced specs
  (``pipeline`` / ``playwright-import``); mined Selenium is knowledge, not style.
- A ``pipeline`` record supersedes a legacy record sharing its ``xray_key``.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..config import Config
from ..models import ManualTestCase
from . import embeddings
from .models import KBRecord, ReconstructedSelector, build_intent_text, project_key_of

logger = logging.getLogger(__name__)

# Retrieval shape (plan §1.6/§1.9): cast a wide recall net, keep a tiny
# precision set — the reranker is the quality gate that protects the prompt
# budget. Overridable per call; env knobs can be wired by the integration task
# if runs show these need tuning.
DEFAULT_TOP_N = 10
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.30
PLANNER_HINT_WORD_BUDGET = 150
MAX_GENERATOR_EXAMPLES = 2
_EXAMPLE_CHAR_CAP = 4000

# Sources whose specs may be shown to the Generator as style examples.
_EXAMPLE_SOURCES = ("pipeline", "playwright-import")


class RetrievedContext(BaseModel):
    """Rendered context blocks for injection, plus a log-friendly summary."""

    planner_hints: str = Field(
        default="", description="Compact similar-cases block for the Planner; '' when none"
    )
    generator_examples: str = Field(
        default="", description="Up to 2 Playwright spec examples for the Generator; '' when none"
    )
    retrieved: list[str] = Field(
        default_factory=list,
        description="One line per surviving record — 'KEY · title (score, source)' — for run logs",
    )

    @property
    def is_empty(self) -> bool:
        return not (self.planner_hints or self.generator_examples)


def retrieve(
    config: Config,
    case: ManualTestCase,
    *,
    store: object | None = None,
    top_n: int = DEFAULT_TOP_N,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> RetrievedContext:
    """Top-K similar solved cases for ``case``, rendered for injection. Fail-open.

    ``store`` accepts a pre-opened ``KBStore`` (tests inject a fake; the seeding
    CLI reuses one); when None, a store is opened on ``config.kb_path`` for the
    duration of the call.
    """
    try:
        return _retrieve(config, case, store, top_n, top_k, min_score)
    except Exception as exc:  # fail-open is the contract — never break a run
        logger.warning(
            "Retrieval memory unavailable for %s (%s: %s) — continuing unassisted.",
            case.key,
            type(exc).__name__,
            exc,
        )
        return RetrievedContext()


def _retrieve(
    config: Config,
    case: ManualTestCase,
    store: object | None,
    top_n: int,
    top_k: int,
    min_score: float,
) -> RetrievedContext:
    project = project_key_of(case.key)
    query = build_intent_text(case.title, case.steps)

    owns_store = store is None
    if store is None:
        from .store import KBStore  # lazy: qdrant only when retrieval actually runs

        store = KBStore(config.kb_path)
    try:
        vector = embeddings.embed(config, [query])[0]
        candidates = store.search(project, vector, top_n)  # type: ignore[attr-defined]
    finally:
        if owns_store:
            store.close()  # type: ignore[attr-defined]

    records = _supersede_legacy_twins([record for record, _ in candidates])
    if not records:
        return RetrievedContext()

    ranked = embeddings.rerank(
        config, query, [record.intent_text for record in records], top_n=len(records)
    )
    selected = [
        (records[index], score) for index, score in ranked if score >= min_score
    ][:top_k]
    if not selected:
        return RetrievedContext()

    return RetrievedContext(
        planner_hints=_render_planner_hints([r for r, _ in selected]),
        generator_examples=_render_generator_examples([r for r, _ in selected]),
        retrieved=[
            f"{record.xray_key or record.record_id[:8]} · {record.title} "
            f"({score:.2f}, {record.source})"
            for record, score in selected
        ],
    )


def _supersede_legacy_twins(records: list[KBRecord]) -> list[KBRecord]:
    """Drop a legacy record when a ``pipeline`` record shares its xray_key (plan §3)."""
    pipeline_keys = {
        record.xray_key for record in records if record.source == "pipeline" and record.xray_key
    }
    return [
        record
        for record in records
        if record.source == "pipeline"
        or not record.xray_key
        or record.xray_key not in pipeline_keys
    ]


def _render_planner_hints(records: list[KBRecord]) -> str:
    """Similar-cases block for the Planner, capped at ~PLANNER_HINT_WORD_BUDGET words.

    Records are appended in rank order until the budget is spent (the best match
    always fits). Selectors carry their ladder kind + provenance and are framed
    as hints to VERIFY live — the never-invent rule is the Planner's, unchanged.
    """
    header = (
        "Similar solved cases (HINTS ONLY — verify every selector live with "
        "browser_generate_locator before recording it; the app may have changed):"
    )
    blocks: list[str] = []
    used_words = len(header.split())
    for record in records:
        block = _hint_block(record)
        block_words = len(block.split())
        if blocks and used_words + block_words > PLANNER_HINT_WORD_BUDGET:
            break
        blocks.append(block)
        used_words += block_words
    return header + "\n" + "\n".join(blocks) if blocks else ""


def _hint_block(record: KBRecord) -> str:
    """One record's hint block — flow + advisory selectors, derived from the plan.

    Selectors carry the resilience-ladder kind, a ✓/⚠ verification mark and their
    provenance; they are hints to VERIFY live, never locators of record. (The full
    injection policy — budget knob, same-ticket rich block, kind filter — lands
    with the Retriever v2 rework.)
    """
    label = record.xray_key or record.source
    lines = [f"- {record.title} ({label}):"]
    actions = [step.action for step in record.plan.steps if step.action.strip()]
    if actions:
        lines.append("  flow: " + " → ".join(actions[:6]))
    seen: set[tuple[str, str]] = set()
    selectors: list[ReconstructedSelector] = []
    for step in record.plan.steps:
        for selector in (step.selector, step.assert_hint):
            if selector is None or (selector.kind, selector.value) in seen:
                continue
            seen.add((selector.kind, selector.value))
            selectors.append(selector)
    for selector in selectors[:5]:
        mark = "✓" if selector.verified else "⚠"
        provenance = f" [{selector.provenance}]" if selector.provenance else ""
        lines.append(f"  {selector.kind}: {selector.value} {mark}{provenance}")
    return "\n".join(lines)


def _render_generator_examples(records: list[KBRecord]) -> str:
    """Up to MAX_GENERATOR_EXAMPLES Playwright specs — never Selenium-sourced."""
    specs = [
        record
        for record in records
        if record.source in _EXAMPLE_SOURCES and record.spec.strip()
    ][:MAX_GENERATOR_EXAMPLES]
    if not specs:
        return ""
    parts = ["Similar existing tests (style reference — follow their conventions):"]
    for record in specs:
        parts.append(
            f"### {record.title} ({record.xray_key or record.source})\n"
            f"```typescript\n{record.spec[:_EXAMPLE_CHAR_CAP]}\n```"
        )
    return "\n".join(parts)
