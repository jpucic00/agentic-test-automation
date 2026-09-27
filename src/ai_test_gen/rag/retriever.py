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
- ``same_ticket_block`` — ~400-word rich block rendered when a ``ui`` record
  shares the run's xray_key. Fetched by key, independent of ranking — a prior
  solve must not depend on surviving top-N, the rerank cutoff or top-K. Framed:
  "you solved exactly this ticket before — verify everything live, the app may
  have changed."
- ``knowledge_block`` — ≤100-word block for ``knowledge``-kind records (suite
  lifecycle/conventions distilled by the Mapper), ranked with its own quota so
  hints and knowledge never crowd each other out. ``api``/``db`` records are
  excluded at the vector query: they carry no browser surface and must not
  occupy ranking slots.
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
DEFAULT_KNOWLEDGE_K = 2  # independent quota; the ≤100-word block rarely fits more
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
        description="One line per INJECTED record — 'KEY · title (score, source)' — for run logs",
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
    knowledge_k: int = DEFAULT_KNOWLEDGE_K,
) -> RetrievedContext:
    """Top-K similar solved cases for ``case``, rendered for injection. Fail-open.

    ``top_k`` caps ``ui`` hints and ``knowledge_k`` caps knowledge records —
    separate quotas over one rerank call. ``store`` accepts a pre-opened
    ``KBStore`` (tests inject a fake; the seeding CLI reuses one); when None, a
    store is opened on ``config.kb_path`` for the duration of the call.
    """
    try:
        return _retrieve(config, case, store, top_n, top_k, min_score, knowledge_k)
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
    knowledge_k: int,
) -> RetrievedContext:
    project = project_key_of(case.key)
    query = build_intent_text(case.title, case.steps)

    owns_store = store is None
    if store is None:
        from .store import KBStore  # lazy: qdrant only when retrieval actually runs

        store = KBStore(config.kb_path)
    try:
        vector = embeddings.embed(config, [query])[0]
        # Kind filter at the query (§6): api/db never take a top-N slot.
        ui_hits = store.search(project, vector, top_n, kinds=("ui",))  # type: ignore[attr-defined]
        knowledge_hits = store.search(  # type: ignore[attr-defined]
            project, vector, top_n, kinds=("knowledge",)
        )
        ticket_records = store.by_xray_key(project, case.key)  # type: ignore[attr-defined]
    finally:
        if owns_store:
            store.close()  # type: ignore[attr-defined]

    # Same-ticket prior solve (§1.19): fetched by key, not ranked; the pipeline
    # record wins over its legacy twin. Its key is kept out of the hint pool.
    same_ticket = next(iter(_supersede_legacy_twins(ticket_records)), None)
    ui_pool = _supersede_legacy_twins(
        [record for record, _ in ui_hits if record.xray_key != case.key]
    )
    candidates = ui_pool + [record for record, _ in knowledge_hits]

    # One rerank call, independent quotas: ui hints → top_k, knowledge → knowledge_k.
    ui_selected: list[tuple[KBRecord, float]] = []
    knowledge_selected: list[tuple[KBRecord, float]] = []
    if candidates:
        ranked = embeddings.rerank(
            config, query, [record.intent_text for record in candidates], top_n=len(candidates)
        )
        for index, score in ranked:
            if score < min_score:
                continue
            record = candidates[index]
            bucket, quota = (
                (ui_selected, top_k) if index < len(ui_pool) else (knowledge_selected, knowledge_k)
            )
            if len(bucket) < quota:
                bucket.append((record, score))
    if same_ticket is None and not ui_selected and not knowledge_selected:
        return RetrievedContext()

    word_budget = getattr(config, "rag_hint_word_budget", DEFAULT_HINT_WORD_BUDGET)
    hints, hint_records = _render_planner_hints([r for r, _ in ui_selected], word_budget)
    knowledge, knowledge_records = _render_knowledge_block([r for r, _ in knowledge_selected])
    examples, example_records = _render_generator_examples(
        ([same_ticket] if same_ticket else []) + [r for r, _ in ui_selected]
    )

    # Log only what was actually injected, in block order, each record once.
    scores = {record.record_id: score for record, score in ui_selected + knowledge_selected}
    injected: dict[str, KBRecord] = {}
    for record in (
        ([same_ticket] if same_ticket else []) + hint_records + knowledge_records + example_records
    ):
        injected.setdefault(record.record_id, record)
    return RetrievedContext(
        planner_hints=hints,
        same_ticket_block=_render_same_ticket_block(same_ticket) if same_ticket else "",
        knowledge_block=knowledge,
        generator_examples=examples,
        retrieved=[
            f"{record.xray_key or record.record_id[:8]} · {record.title} ("
            + (f"{scores[record.record_id]:.2f}" if record.record_id in scores else "same ticket")
            + f", {record.source})"
            for record in injected.values()
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


def _render_planner_hints(records: list[KBRecord], word_budget: int) -> tuple[str, list[KBRecord]]:
    """Compact similar-cases block for the Planner, capped at ``word_budget`` words.
    Returns the block and the records that made it in.

    Records are appended in rank order until the budget is spent (the best match
    always fits). Selectors carry their ladder kind + provenance and are framed
    as hints to VERIFY live — the never-invent rule is the Planner's, unchanged.

    Only ``ui``-kind records arrive here; ``api``/``db``/``knowledge`` are
    excluded before this call.
    """
    if not records:
        return "", []
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
    return header + "\n" + "\n".join(blocks), records[: len(blocks)]


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


def _render_knowledge_block(records: list[KBRecord]) -> tuple[str, list[KBRecord]]:
    """≤100-word core-knowledge block for ``knowledge``-kind records (§6, policy D).

    Knowledge records are distilled map sections (lifecycle, conventions) upserted
    by the Mapper. They are advisory — the Planner applies them unless the live app
    contradicts them. The best record always renders, truncated to the budget:
    mapper bodies routinely exceed 100 words, and without that rule the block
    rendered empty for exactly the records it exists to inject. Returns the block
    and the records that made it in.
    """
    if not records:
        return "", []
    header = "Core knowledge (suite conventions — apply unless the live app contradicts it):"
    parts: list[str] = []
    rendered: list[KBRecord] = []
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
        remaining = _KNOWLEDGE_WORD_BUDGET - used_words
        if snippet_words > remaining:
            if parts:
                break
            # Best-match-always-fits (same rule as the planner hints): the top
            # knowledge record renders truncated rather than not at all.
            snippet = " ".join(snippet.split()[: max(remaining - 1, 1)]) + " …"
            snippet_words = len(snippet.split())
        parts.append(snippet)
        rendered.append(record)
        used_words += snippet_words
    return (header + "\n" + "\n".join(parts) if parts else ""), rendered


def _render_generator_examples(records: list[KBRecord]) -> tuple[str, list[KBRecord]]:
    """Up to MAX_GENERATOR_EXAMPLES Playwright specs — never Selenium-sourced.
    Returns the block and the records used."""
    specs = [
        record
        for record in records
        if record.source in _EXAMPLE_SOURCES and record.spec.strip()
    ][:MAX_GENERATOR_EXAMPLES]
    if not specs:
        return "", []
    parts = ["Similar existing tests (style reference — follow their conventions):"]
    for record in specs:
        parts.append(
            f"### {record.title} ({record.xray_key or record.source})\n"
            f"```typescript\n{record.spec[:_EXAMPLE_CHAR_CAP]}\n```"
        )
    return "\n".join(parts), specs
