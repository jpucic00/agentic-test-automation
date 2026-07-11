"""Retrieval: embed → per-project vector search → rerank → top-K context blocks.

The only consumer-facing entry point is ``retrieve()``. It is **fail-open by
contract** (RETRIEVAL_MEMORY_PLAN.md §7): any failure — store, embeddings,
rerank, malformed payloads — logs a WARNING and returns an EMPTY
``RetrievedContext``, so a run with RAG on and infrastructure down behaves
exactly like an unassisted run. It never raises to the caller.

Injection policy D (§1.19 / §6):
- ``planner_hints`` — compact similar-cases block: title + flow (plan actions) +
  ``outcome:`` (last manual expected) + ~4 selectors with kind/✓⚠/provenance.
  Word-budget from ``config.rag_hint_word_budget`` (default 250). Only ``ui``
  records render here; a same-ticket record is excluded (it gets its own block).
- ``same_ticket_block`` — ~400-word rich block rendered when a retrieved record
  shares the run's xray_key. Framed: "you solved exactly this ticket before —
  verify everything live, the app may have changed."
- ``knowledge_block`` — ≤100-word block for ``knowledge``-kind records (suite
  lifecycle/conventions distilled by the Mapper). ``api``/``db`` records are
  excluded from all injection: they carry no browser surface.
- ``generator_examples`` — ≤2 Playwright specs; only ``pipeline``/
  ``playwright-import`` sources (mined Selenium is knowledge, never style).

A ``pipeline`` record supersedes a legacy record sharing its ``xray_key``.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..config import Config
from ..models import ManualTestCase
from . import embeddings
from .models import KBRecord, ReconstructedSelector, build_intent_text, project_key_of

logger = logging.getLogger(__name__)

# Retrieval shape: wide recall net, tiny precision set — the reranker is the
# quality gate protecting the prompt budget. Overridable per call.
DEFAULT_TOP_N = 10
DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 0.30
DEFAULT_HINT_WORD_BUDGET = 250  # env: RAG_HINT_WORD_BUDGET
MAX_GENERATOR_EXAMPLES = 2
_EXAMPLE_CHAR_CAP = 4000
_KNOWLEDGE_WORD_BUDGET = 100

# Sources whose specs may be shown to the Generator as style examples (§1.6).
_EXAMPLE_SOURCES = ("pipeline", "playwright-import")


class RetrievedContext(BaseModel):
    """Rendered context blocks for injection, plus a log-friendly summary."""

    planner_hints: str = Field(
        default="",
        description="Compact similar-cases block for the Planner (ui records only); '' when none",
    )
    same_ticket_block: str = Field(
        default="",
        description="~400-word prior-solve block when a retrieved record shares the run's xray_key",
    )
    knowledge_block: str = Field(
        default="",
        description="≤100-word core-knowledge block for knowledge-kind records; '' when none",
    )
    generator_examples: str = Field(
        default="",
        description="Up to 2 Playwright spec examples for the Generator; '' when none",
    )
    retrieved: list[str] = Field(
        default_factory=list,
        description="One line per surviving record — 'KEY · title (score, source)' — for run logs",
    )

    @property
    def is_empty(self) -> bool:
        return not (
            self.planner_hints
            or self.same_ticket_block
            or self.knowledge_block
            or self.generator_examples
        )


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

    # Split by kind (§1.19 policy D).
    # ui  → compact hint block (excluding the same-ticket record, which gets the rich block)
    # knowledge → core-knowledge block (suite lifecycle/conventions from the Mapper)
    # api/db → silently excluded (no browser surface, nothing for the Planner to apply)
    all_records = [r for r, _ in selected]
    same_ticket = next(
        (r for r in all_records if r.kind == "ui" and r.xray_key == case.key), None
    )
    ui_records = [
        r for r in all_records if r.kind == "ui" and r is not same_ticket
    ]
    knowledge_records = [r for r in all_records if r.kind == "knowledge"]

    word_budget = getattr(config, "rag_hint_word_budget", DEFAULT_HINT_WORD_BUDGET)
    return RetrievedContext(
        planner_hints=_render_planner_hints(ui_records, word_budget),
        same_ticket_block=_render_same_ticket_block(same_ticket) if same_ticket else "",
        knowledge_block=_render_knowledge_block(knowledge_records),
        generator_examples=_render_generator_examples(all_records),
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


def _render_planner_hints(records: list[KBRecord], word_budget: int) -> str:
    """Compact similar-cases block for the Planner, capped at ``word_budget`` words.

    Records are appended in rank order until the budget is spent (the best match
    always fits). Selectors carry their ladder kind + provenance and are framed
    as hints to VERIFY live — the never-invent rule is the Planner's, unchanged.

    Only ``ui``-kind records arrive here; ``api``/``db``/``knowledge`` are
    excluded before this call.
    """
    if not records:
        return ""
    header = (
        "Similar solved cases (HINTS ONLY — verify every selector live with "
        "browser_generate_locator before recording it; the app may have changed):"
    )
    blocks: list[str] = []
    used_words = len(header.split())
    for record in records:
        block = _hint_block(record)
        block_words = len(block.split())
        if blocks and used_words + block_words > word_budget:
            break
        blocks.append(block)
        used_words += block_words
    return header + "\n" + "\n".join(blocks) if blocks else ""


def _hint_block(record: KBRecord) -> str:
    """One record's compact hint block — flow + outcome + advisory selectors."""
    label = record.xray_key or record.source
    lines = [f"- {record.title} ({label}):"]
    actions = [step.action for step in record.plan.steps if step.action.strip()]
    if actions:
        lines.append("  flow: " + " → ".join(actions[:6]))
    # outcome: last manual expected — the ticket's stated result, not a code assertion
    last_expected = next(
        (s.expected.strip() for s in reversed(record.manual_steps) if s.expected.strip()),
        "",
    )
    if last_expected:
        lines.append(f"  outcome: {last_expected}")
    seen: set[tuple[str, str]] = set()
    selectors: list[ReconstructedSelector] = []
    for step in record.plan.steps:
        for sel in (step.selector, step.assert_hint):
            if sel is None or (sel.kind, sel.value) in seen:
                continue
            seen.add((sel.kind, sel.value))
            selectors.append(sel)
    for selector in selectors[:4]:  # ~4 selectors per hint (§1.19)
        mark = "✓" if selector.verified else "⚠"
        provenance = f" [{selector.provenance}]" if selector.provenance else ""
        lines.append(f"  {selector.kind}: {selector.value} {mark}{provenance}")
    return "\n".join(lines)


def _render_same_ticket_block(record: KBRecord) -> str:
    """~400-word rich block for a prior solve of the same ticket (§6, policy D).

    Renders the full ``ReconstructedPlan`` with selectors. Framed as a prior solve
    to review — the Planner verifies every selector live since the app may have
    changed.
    """
    key_label = record.xray_key or record.title
    lines = [
        f"Prior solve of {key_label} — review the plan and verify every selector live"
        " (the app may have changed):",
        f"  Title: {record.title}",
    ]
    if record.plan.start_route:
        lines.append(f"  Start: {record.plan.start_route}")
    lines.append("  Steps:")
    for step in record.plan.steps:
        step_line = f"    • {step.action}"
        if step.selector:
            mark = "✓" if step.selector.verified else "⚠"
            prov = f" [{step.selector.provenance}]" if step.selector.provenance else ""
            step_line += f" [{step.selector.kind}:{step.selector.value} {mark}{prov}]"
        if step.expected:
            step_line += f" → {step.expected}"
        lines.append(step_line)
        if step.assert_hint:
            mark = "✓" if step.assert_hint.verified else "⚠"
            prov = f" [{step.assert_hint.provenance}]" if step.assert_hint.provenance else ""
            lines.append(
                f"      assert: [{step.assert_hint.kind}:{step.assert_hint.value} {mark}{prov}]"
            )
    if record.plan.notes:
        lines.append(f"  Notes: {record.plan.notes}")
    return "\n".join(lines)


def _render_knowledge_block(records: list[KBRecord]) -> str:
    """≤100-word core-knowledge block for ``knowledge``-kind records (§6, policy D).

    Knowledge records are distilled map sections (lifecycle, conventions) upserted
    by the Mapper. They are advisory — the Planner applies them unless the live app
    contradicts them.
    """
    if not records:
        return ""
    header = "Core knowledge (suite conventions — apply unless the live app contradicts it):"
    parts: list[str] = []
    used_words = len(header.split())
    for record in records:
        # Mapper puts conventions in plan.notes; fall back to step actions.
        text = record.plan.notes.strip() or " | ".join(
            s.action for s in record.plan.steps if s.action.strip()
        )
        if not text:
            continue
        snippet = f"- {record.title}: {text}"
        snippet_words = len(snippet.split())
        if used_words + snippet_words > _KNOWLEDGE_WORD_BUDGET:
            break
        parts.append(snippet)
        used_words += snippet_words
    return header + "\n" + "\n".join(parts) if parts else ""


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
