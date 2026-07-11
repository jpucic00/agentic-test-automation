"""KB seeding orchestration: discover → map → distill → verify → review → embed/upsert.

The importable engine behind ``scripts/seed_kb.py`` (RETRIEVAL_MEMORY_PLAN.md §5).
Discovery and the suite map are phase 0 (deterministic skeleton + one Mapper
refinement, cached); this module then runs **one bounded Distiller exploration per
discovered test** (§5.3), string-verifies every selector claim with one bounce
round (§5.4, ``verify.py``), assembles KBRecord v2 in plain code (§5.5 — the model
never authors ``intent_text``), writes one human-readable review file per record
plus an honesty summary, and — unless ``--dry-run`` — embeds ``intent_text`` (only)
and upserts into the project's collection.

Fault containment (§7): a distill failure (tool errors, budget exhausted, terminal
model error) skips THAT record, is counted in the summary, and never sinks the
corpus run. Infrastructure failures (map model, embeddings, store) stay loud — this
is an offline tool, not the run loop.

Money-shaped decisions: stable record ids exist BEFORE any model call, so a re-run
skips already-stored records without paying for an LLM (``--force`` re-distills);
completed records are embedded+upserted in periodic flushes so a crash mid-corpus
keeps what was already distilled; ``--workers N`` runs distillations concurrently
(each with its OWN ``RepoTools`` so per-record instrumentation never
cross-contaminates); every manual-case load is per-key fault-tolerant.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import Config
from ..models import ManualStep, ManualTestCase
from . import embeddings
from .discover import (
    DiscoveredTest,
    DiscoveryResult,
    discover_tests,
    render_discovery_summary,
)
from .distiller import (
    DistillResult,
    DistillTurns,
    distill_test,
    render_manual_triplets,
)
from .mapper import RunDraft, SuiteMapResult, build_suite_map
from .models import KBRecord, build_intent_text
from .tools import RepoTools

logger = logging.getLogger(__name__)

# Embed+upsert completed records every N distills — a crash mid-corpus must not
# burn the model spend of everything before it (resume then skips the stored ones).
_FLUSH_EVERY = 20

# Injectable seams for offline tests.
EmbedFn = Callable[[Config, Sequence[str]], list[list[float]]]
TurnsFactory = Callable[[Config, RepoTools, DiscoveredTest], DistillTurns]


@dataclass
class SeedStats:
    """Everything the summary reports — the §5.5 honesty counters."""

    project: str
    dry_run: bool
    discovery: DiscoveryResult | None = None
    map_result: SuiteMapResult | None = None
    review_dir: Path | None = None
    # distillation accounting
    planned: int = 0
    skipped_existing: int = 0
    distilled: int = 0
    failed: dict[str, str] = field(default_factory=dict)  # ref → reason (skipped, counted)
    retried: int = 0  # failed records given the one end-of-run second attempt
    recovered: int = 0  # of those, ones that distilled on the retry
    upserted: int = 0
    knowledge_upserted: int = 0
    # manual cases
    cases_loaded: int = 0
    case_misses: dict[str, str] = field(default_factory=dict)  # key → reason
    # selector honesty
    claims_cited: int = 0
    claims_verified: int = 0
    claims_unverified: int = 0
    citations_auto_fixed: int = 0
    records_bounced: int = 0
    claims_bounced: int = 0
    selectorless_ui: list[str] = field(default_factory=list)  # record refs
    escalations: dict[str, list[str]] = field(default_factory=dict)  # ref → signals
    per_suite: dict[str, int] = field(default_factory=dict)  # suite → distilled count

    @property
    def unverified_rate(self) -> float:
        return self.claims_unverified / self.claims_cited if self.claims_cited else 0.0


async def run_seeding(
    config: Config,
    *,
    project: str,
    selenium_root: Path | None = None,
    playwright_dir: Path | None = None,
    cases: list[str] | None = None,
    no_fetch: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    workers: int = 1,
    refresh_map: bool = False,
    map_only: bool = False,
    marker_regex: str | None = None,
    map_dir: Path | None = None,
    run_draft: RunDraft | None = None,
    turns_factory: TurnsFactory | None = None,
    embed: EmbedFn = embeddings.embed,
) -> SeedStats:
    """Seed ``kb_<project>`` from a corpus. Returns the stats the summary was built from.

    ``run_draft`` / ``turns_factory`` / ``embed`` are test seams (recorded Mapper
    transcript, canned Distiller turns, fixed vectors); production leaves them unset.
    """
    project = project.strip().upper()
    stats = SeedStats(project=project, dry_run=dry_run)
    roots = [r for r in (selenium_root, playwright_dir) if r is not None]
    if not roots:
        raise ValueError("run_seeding needs at least one corpus root (selenium/playwright)")
    marker = marker_regex or config.test_marker_regex

    # --- phase 0a: deterministic discovery (parity is always reported) -------------
    discovery = discover_tests(
        project, selenium_root=selenium_root, playwright_dir=playwright_dir, marker_regex=marker
    )
    stats.discovery = discovery
    logger.info(":: discovery\n%s", render_discovery_summary(discovery))

    # --- phase 0b: the suite map (cached per section) -------------------------------
    logger.info(":: suite map — building/refreshing for %s ...", project)
    map_result = await build_suite_map(
        config,
        project,
        selenium_root=selenium_root,
        playwright_dir=playwright_dir,
        discovery=discovery,
        map_dir=map_dir,
        refresh=refresh_map,
        run_draft=run_draft,
    )
    stats.map_result = map_result
    logger.info(
        ":: suite map at %s (%s; refreshed: %s; %d file(s) read, %d tool call(s))",
        map_result.path,
        "from cache" if map_result.from_cache else "generated",
        ", ".join(map_result.stale_sections) or "none",
        len(map_result.files_opened),
        map_result.tool_calls,
    )
    if map_result.unresolved_citations:
        logger.warning(
            ":: %d map citation(s) did not resolve to a corpus file (flagged in the map): %s",
            len(map_result.unresolved_citations),
            ", ".join(map_result.unresolved_citations),
        )

    store = None
    try:
        if not dry_run:
            from .store import KBStore  # lazy: qdrant only when something will be written

            store = KBStore(config.kb_path)
            if map_result.knowledge_records:
                vectors = embed(config, [r.intent_text for r in map_result.knowledge_records])
                store.upsert(project, map_result.knowledge_records, vectors)
                stats.knowledge_upserted = len(map_result.knowledge_records)
                logger.info(
                    ":: upserted %d core-knowledge record(s) into kb_%s",
                    stats.knowledge_upserted,
                    project,
                )
        if map_only:
            return stats

        # --- phase 1: per-test distillation ----------------------------------------
        planned = list(discovery.tests)
        if not force and store is not None and planned:
            already = store.existing_ids(project, [t.record_id for t in planned])
            stats.skipped_existing = len(already)
            planned = [t for t in planned if t.record_id not in already]
        if limit is not None:
            planned = planned[:limit]
        stats.planned = len(planned)
        logger.info(
            ":: distilling %d test(s) via %s (mode=%s, workers=%d%s%s)",
            len(planned),
            config.distiller_model,
            config.distiller_mode,
            workers,
            f", skipped {stats.skipped_existing} already stored" if stats.skipped_existing else "",
            " — DRY RUN" if dry_run else "",
        )
        if not planned:
            _write_summary(config, stats)
            return stats

        linked_keys = list(dict.fromkeys(t.xray_key for t in planned if t.xray_key))
        case_lookup = _load_cases(config, cases or [], linked_keys, stats, no_fetch=no_fetch)

        review_dir = config.output_dir / "kb_review" / project
        review_dir.mkdir(parents=True, exist_ok=True)
        stats.review_dir = review_dir

        text_cache: dict[str, str] = {}
        semaphore = asyncio.Semaphore(max(1, workers))
        done = 0
        pending: list[KBRecord] = []

        async def one(
            test: DiscoveredTest,
        ) -> tuple[DiscoveredTest, DistillResult | None, str | None]:
            async with semaphore:
                # Per-distill RepoTools: files_opened/tool_calls stay per-record honest.
                tools = RepoTools(roots)
                case = case_lookup.get(test.xray_key or "")
                try:
                    result = await distill_test(
                        config,
                        tools,
                        test,
                        case,
                        address=_address_for(test, tools, selenium_root, playwright_dir),
                        map_index=map_result.index,
                        suite_block=_suite_block(map_result, test),
                        turns=turns_factory(config, tools, test) if turns_factory else None,
                        text_cache=text_cache,
                    )
                    return test, result, None
                except Exception as exc:  # per-test containment (§7) — counted, never fatal
                    logger.error(":: distill FAILED for %s — skipped: %r", test.ref, exc)
                    return test, None, f"{type(exc).__name__}: {exc}"

        async def flush() -> None:
            if not pending or store is None:
                pending.clear()
                return
            batch = list(pending)
            pending.clear()
            vectors = await asyncio.to_thread(
                embed, config, [r.intent_text for r in batch]
            )
            await asyncio.to_thread(store.upsert, project, batch, vectors)
            stats.upserted += len(batch)
            logger.info(":: flushed %d record(s) to kb_%s (%d total)", len(batch),
                        project, stats.upserted)

        async def consume(future) -> None:
            nonlocal done
            test, result, failure = await future
            done += 1
            if result is None:
                stats.failed[test.ref] = failure or "unknown failure"
                return
            stats.failed.pop(test.ref, None)  # a retry recovery clears the first failure
            case = case_lookup.get(test.xray_key or "")
            record = _to_record(project, test, result, case)
            _fold_into_stats(stats, test, record, result)
            miss = stats.case_misses.get(test.xray_key) if test.xray_key and not case else None
            _write_review_file(review_dir, record, test, result, case, miss)
            logger.info(
                ":: [%d/%d] distilled %s (%s) — %d step(s), %d/%d selector(s) verified",
                min(done, len(planned)),
                len(planned),
                test.ref,
                test.xray_key or "unlinked",
                len(record.plan.steps),
                result.trace.selectors_verified,
                result.trace.selectors_cited,
            )
            if not dry_run:
                pending.append(record)
                if len(pending) >= _FLUSH_EVERY:
                    await flush()

        for future in asyncio.as_completed([one(test) for test in planned]):
            await consume(future)

        # One bounded second attempt for records that failed the first pass — live
        # gateways fail transiently (a provider hiccup, a bad route in a balancer
        # pool), and losing a record to one bad minute wastes the whole re-run.
        # Exactly one retry round, then failures stand and are counted.
        retry = [test for test in planned if test.ref in stats.failed]
        if retry:
            stats.retried = len(retry)
            logger.info(":: retrying %d failed distill(s) once ...", len(retry))
            first_pass_failures = set(stats.failed)
            for future in asyncio.as_completed([one(test) for test in retry]):
                await consume(future)
            stats.recovered = sum(1 for ref in first_pass_failures if ref not in stats.failed)
        await flush()
    finally:
        if store is not None:
            store.close()

    _write_summary(config, stats)
    return stats


# --- record assembly (code, never the model — §5.5) ---------------------------------
def _to_record(
    project: str,
    test: DiscoveredTest,
    result: DistillResult,
    case: ManualTestCase | None,
) -> KBRecord:
    output = result.output
    if case is not None:
        title = case.title
        intent = build_intent_text(case.title, case.steps)
    else:
        # Plan-derived fallback (§1.17) through the SAME builder the runtime query uses.
        title = output.plan.title or test.symbol
        intent = build_intent_text(
            title,
            [ManualStep(action=s.action, expected=s.expected) for s in output.plan.steps],
        )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    routes = list(
        dict.fromkeys(
            [*(r.strip() for r in output.routes if r.strip()),
             *(s.route.strip() for s in output.plan.steps if s.route.strip())]
        )
    )
    return KBRecord(
        record_id=test.record_id,
        project_key=project,
        xray_key=test.xray_key,
        title=title,
        intent_text=intent,
        plan=output.plan,
        manual_steps=list(case.steps) if case else [],
        kind=output.kind,
        routes=routes,
        spec=test.code if test.language == "ts" else "",
        source_code=test.code,
        source_lang=test.language,
        explored=result.trace,
        outcome="legacy",
        source=test.source,
        created_at=now,
        updated_at=now,
    )


def _fold_into_stats(
    stats: SeedStats, test: DiscoveredTest, record: KBRecord, result: DistillResult
) -> None:
    from .verify import escalation_signals

    stats.distilled += 1
    suite = test.path.split("/", 1)[0] if "/" in test.path else "(root)"
    stats.per_suite[suite] = stats.per_suite.get(suite, 0) + 1
    stats.claims_cited += result.trace.selectors_cited
    stats.claims_verified += result.trace.selectors_verified
    stats.claims_unverified += result.trace.selectors_unverified
    stats.citations_auto_fixed += len(result.verify.auto_fixed)
    if result.bounced_claims:
        stats.records_bounced += 1
        stats.claims_bounced += result.bounced_claims
    signals = escalation_signals(
        record.plan,
        record.kind,
        record.manual_steps,
        len(result.trace.files_opened),
        result.trace.unresolved,
    )
    if signals:
        stats.escalations[test.ref] = signals
    if "selectorless-ui" in signals:
        stats.selectorless_ui.append(test.ref)


def _address_for(
    test: DiscoveredTest,
    tools: RepoTools,
    selenium_root: Path | None,
    playwright_dir: Path | None,
) -> str:
    """The test file's tool-addressable path (labeled when there are multiple roots)."""
    root = selenium_root if test.language == "java" else playwright_dir
    if root is None:  # cannot happen for a discovered test; keep the fallback honest
        return test.path
    return tools.address_of((root / test.path).resolve())


def _suite_block(map_result: SuiteMapResult, test: DiscoveredTest) -> str:
    """The map's suite line(s) for the suite this test lives in ('' when unmapped)."""
    if map_result.draft is None:
        return ""
    hits: list[str] = []
    for note in map_result.draft.suites:
        suite_path = note.path.strip().strip("/")
        if not suite_path:
            continue
        if test.path.startswith(suite_path) or f"/{suite_path}/" in f"/{test.path}":
            hits.append(f"- `{note.path}` — {note.role}")
    return "\n".join(hits)


# --- manual cases (per-key fault-tolerant — §5.3) ------------------------------------
def _load_cases(
    config: Config,
    cases: list[str],
    linked_keys: list[str],
    stats: SeedStats,
    *,
    no_fetch: bool,
) -> dict[str, ManualTestCase]:
    """Manual cases per key; every miss lands in ``stats.case_misses`` with a reason.

    ``--cases`` overrides the source: one directory of raw-Xray JSON files, or explicit
    issue keys to fetch live. Without it, the keys named by the corpus markers load
    automatically — locally when ``TESTCASE_SOURCE=local``, else from Jira/Xray when
    configured. ``--no-fetch`` skips fetching entirely (plan-derived intent_text).
    """
    misses = stats.case_misses
    if cases:
        first = Path(cases[0]).expanduser()
        if len(cases) == 1 and first.is_dir():
            local_config = dataclasses.replace(
                config, testcase_source="local", local_testcase_dir=first.resolve()
            )
            keys = sorted(path.stem for path in first.glob("*.json"))
            loaded = _load_local(local_config, keys, misses)
            for key in linked_keys:
                if key not in loaded:
                    misses.setdefault(key, f"no {key}.json in --cases directory {first}")
            stats.cases_loaded = len(loaded)
            return loaded
        loaded = _fetch_live(config, list(cases), misses)
        stats.cases_loaded = len(loaded)
        return loaded
    if no_fetch or not linked_keys:
        return {}
    if config.testcase_source == "local" and config.local_testcase_dir is not None:
        loaded = _load_local(config, linked_keys, misses)
    elif config.jira_base_url and config.jira_email and config.jira_token:
        loaded = _fetch_live(config, linked_keys, misses)
    else:
        reason = (
            "no case source: --cases not given, TESTCASE_SOURCE!=local and Jira is not configured"
        )
        for key in linked_keys:
            misses[key] = reason
        logger.warning(":: %d linked manual case(s) NOT loaded — %s", len(linked_keys), reason)
        return {}
    stats.cases_loaded = len(loaded)
    return loaded


def _load_local(
    config: Config, keys: list[str], misses: dict[str, str]
) -> dict[str, ManualTestCase]:
    from ..local_testcases import load_local_test_case

    logger.info(":: loading %d manual case(s) from %s", len(keys), config.local_testcase_dir)
    loaded: dict[str, ManualTestCase] = {}
    for key in keys:
        try:
            loaded[key] = load_local_test_case(config, key)
        except Exception as exc:  # per-key tolerant — reported, never fatal
            misses[key] = str(exc)
            logger.warning(":: local case %s not loaded: %s", key, exc)
    return loaded


def _fetch_live(
    config: Config, keys: list[str], misses: dict[str, str]
) -> dict[str, ManualTestCase]:
    from ..xray_client import XrayClient  # live fetch — needs the Jira network

    try:
        client = XrayClient(config)
    except Exception as exc:
        for key in keys:
            misses[key] = f"Xray client unavailable: {exc}"
        logger.warning(":: Xray client unavailable — no manual cases loaded: %s", exc)
        return {}
    logger.info(":: fetching %d manual case(s) from Jira/Xray ...", len(keys))
    loaded: dict[str, ManualTestCase] = {}
    for key in keys:
        try:
            loaded[key] = client.fetch(key)
        except Exception as exc:  # per-key tolerant — reported, never fatal
            misses[key] = f"fetch failed: {exc}"
            logger.warning(":: case %s not fetched: %s", key, exc)
    return loaded


# --- review files + summary (§5.5) ----------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "record"


def _render_selector_line(label: str, selector, *, indent: str = "   ") -> str:
    mark = "✓" if selector.verified else "⚠"
    suffix = "" if selector.verified else " (UNVERIFIED)"
    return (
        f"{indent}- {label} {mark} `{selector.kind}`: `{selector.value}` — "
        f"`{selector.provenance or '(no citation)'}`{suffix}"
    )


def _render_plan(record: KBRecord) -> str:
    plan = record.plan
    lines: list[str] = []
    if plan.start_route:
        lines.append(f"Start route: `{plan.start_route}`")
    for index, step in enumerate(plan.steps, 1):
        route = f"  _[@ {step.route}]_" if step.route else ""
        lines.append(f"{index}. {step.action}{route}")
        if step.selector is not None:
            lines.append(_render_selector_line("selector", step.selector))
        if step.expected:
            lines.append(f"   - expected: {step.expected}")
        if step.assert_hint is not None:
            lines.append(_render_selector_line("assert", step.assert_hint))
        if step.source_ref:
            lines.append(f"   - from: `{step.source_ref}`")
    if not plan.steps:
        lines.append("(no steps — REVIEW: empty reconstruction)")
    if plan.notes:
        lines.append(f"\nNotes: {plan.notes}")
    return "\n".join(lines)


def _write_review_file(
    review_dir: Path,
    record: KBRecord,
    test: DiscoveredTest,
    result: DistillResult,
    case: ManualTestCase | None,
    case_miss_reason: str | None,
) -> None:
    """One human-readable file per record — the distillation-quality feedback loop."""
    from .verify import escalation_signals

    if case is not None:
        case_block = f"{record.xray_key}: {case.title}\n\n{render_manual_triplets(case.steps)}"
    elif record.xray_key:
        case_block = (
            f"**NOT LOADED** ({case_miss_reason or 'no reason recorded'}) — REVIEW: the "
            "distillation ran without the manual steps"
        )
    else:
        case_block = "(test not linked to a manual case)"

    verify = result.verify
    bounce_line = (
        f"{result.bounced_claims} claim(s) sent to one revalidation round; "
        f"{len(verify.unverified)} remain unverified"
        if result.bounced_claims
        else "not needed (all claims verified on the first pass)"
    )
    auto_fixed = "\n".join(f"- {fix}" for fix in verify.auto_fixed) or "- (none)"
    survivors = "\n".join(f"- {c.describe()}" for c in verify.unverified) or "- (none)"
    signals = escalation_signals(
        record.plan,
        record.kind,
        record.manual_steps,
        len(result.trace.files_opened),
        result.trace.unresolved,
    )
    escalation_block = "\n".join(f"- {s}" for s in signals) or "- (none)"
    unresolved = "\n".join(f"- {u}" for u in result.trace.unresolved) or "- (none)"
    files = ", ".join(result.trace.files_opened) or "(none)"

    body = f"""# {record.title}

| | |
|---|---|
| Xray key | {record.xray_key or "(unlinked)"} |
| Source | {record.source} (`{test.ref}`) |
| Record id | {record.record_id} |
| Kind | {record.kind} |
| Routes | {", ".join(record.routes) or "(none)"} |

## Linked manual case (verbatim snapshot — the diff base for later ticket edits)
{case_block}

## intent_text (what gets embedded — code-built)
{record.intent_text}

## Reconstructed plan (✓ verified at citation · ⚠ unverified — advisory either way)
{_render_plan(record)}

## Verification
- claims cited: {verify.cited} · verified: {verify.verified} · unverified: {len(verify.unverified)}
- citations auto-fixed: {len(verify.auto_fixed)}
- bounce: {bounce_line}

Auto-fixed citations:
{auto_fixed}

Unverified survivors (flagged, kept):
{survivors}

## Escalation signals
{escalation_block}

## Exploration
- files opened ({len(result.trace.files_opened)}): {files}
- tool calls: {result.trace.tool_calls}
- unresolved (self-reported):
{unresolved}

## Source ({record.source_lang}, {len(record.source_code)} chars)
```{record.source_lang}
{record.source_code}
```
"""
    name = _slug(f"{record.xray_key or test.symbol}-{test.symbol}") + ".md"
    (review_dir / name).write_text(body)


def _write_summary(config: Config, stats: SeedStats) -> None:
    """The §5.5 honesty counters — written even when nothing was distilled."""
    review_dir = stats.review_dir or (config.output_dir / "kb_review" / stats.project)
    review_dir.mkdir(parents=True, exist_ok=True)
    stats.review_dir = review_dir

    per_suite = (
        "\n".join(f"  - {suite}: {count}" for suite, count in sorted(stats.per_suite.items()))
        or "  - (none)"
    )
    failed = (
        "\n".join(f"  - {ref}: {reason}" for ref, reason in sorted(stats.failed.items()))
        or "  - (none)"
    )
    misses = (
        "\n".join(f"  - {key}: {reason}" for key, reason in sorted(stats.case_misses.items()))
        or "  - (none)"
    )
    escalations = (
        "\n".join(
            f"  - {ref}: {', '.join(signals)}"
            for ref, signals in sorted(stats.escalations.items())
        )
        or "  - (none)"
    )
    selectorless = ", ".join(stats.selectorless_ui) or "(none)"
    discovery_block = (
        render_discovery_summary(stats.discovery) if stats.discovery else "- (no discovery run)"
    )
    map_line = (
        f"- suite map: {stats.map_result.path} "
        f"({'cache hit' if stats.map_result.from_cache else 'refreshed'})"
        if stats.map_result
        else "- suite map: (not built)"
    )

    lines = f"""# Seeding summary — {stats.project} ({"DRY RUN" if stats.dry_run else "live"})

## Discovery (parity — a gap is never silent)
{discovery_block}
{map_line}

## Distillation
- planned: {stats.planned} (skipped already-stored: {stats.skipped_existing})
- distilled: {stats.distilled}
- transient retries: {stats.retried} record(s) given one second attempt; {stats.recovered} \
recovered
- failed (skipped after the retry, run continued): {len(stats.failed)}
{failed}
- per suite:
{per_suite}

## Manual cases
- loaded: {stats.cases_loaded}
- misses: {len(stats.case_misses)}
{misses}

## Selector honesty
- claims cited: {stats.claims_cited} · verified: {stats.claims_verified} · \
unverified: {stats.claims_unverified} (rate: {stats.unverified_rate:.0%})
- citations auto-fixed: {stats.citations_auto_fixed}
- bounce outcomes: {stats.records_bounced} record(s) bounced ({stats.claims_bounced} claim(s) \
sent); {stats.claims_unverified} flagged unverified after
- selectorless ui-records: {len(stats.selectorless_ui)} — {selectorless}

## Escalations (re-distillation nudge list)
{escalations}

## Store
- records upserted: {stats.upserted} · core-knowledge records: {stats.knowledge_upserted}\
{" · DRY RUN — nothing embedded/upserted" if stats.dry_run else ""}
"""
    (review_dir / "summary.md").write_text(lines)
    logger.info(":: summary → %s", review_dir / "summary.md")


# --- CLI --------------------------------------------------------------------------
def _parse_args(argv: list[str] | None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Offline KB seeding: discover → suite map → distill → verify → embed/upsert."
    )
    parser.add_argument("--project", required=True, help="Jira/collection key, e.g. QA or NOTE")
    parser.add_argument("--selenium", help="Root of the Selenium/Java suite")
    parser.add_argument("--playwright", help="Directory of hand-written *.spec.ts files")
    parser.add_argument(
        "--cases",
        nargs="*",
        default=[],
        help="A directory of raw-Xray-shaped JSON files, or issue keys to fetch live. "
        "Omitted: the keys named by the corpus markers are fetched automatically",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="Never fetch manual cases (code-only distill)"
    )
    parser.add_argument(
        "--map-only", action="store_true", help="Stop after the suite map + knowledge records"
    )
    parser.add_argument(
        "--refresh-map", action="store_true", help="Ignore the map's per-section cache"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Distill + write review files only — no embeddings, no KB writes",
    )
    parser.add_argument("--limit", type=int, help="Distill at most N discovered tests")
    parser.add_argument(
        "--force", action="store_true", help="Re-distill records that are already stored"
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="Concurrent distillations (default 1)"
    )
    parser.add_argument("--marker-regex", help="Override TEST_MARKER_REGEX for discovery")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    return parser.parse_args(argv)


def _resolve(path_arg: str) -> Path:
    path = Path(path_arg).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.selenium and not args.playwright:
        logger.error("Provide at least one corpus root: --selenium and/or --playwright.")
        return 2
    # Self-diagnosing stall watchdog: if anything dangles (a wedged connection, a
    # pathological file), every thread's stack prints to stderr each 120s — a silent
    # hang always names its own culprit.
    import faulthandler

    faulthandler.dump_traceback_later(120, repeat=True)

    from ..config import load_config

    config = load_config()
    try:
        stats = asyncio.run(
            run_seeding(
                config,
                project=args.project,
                selenium_root=_resolve(args.selenium) if args.selenium else None,
                playwright_dir=_resolve(args.playwright) if args.playwright else None,
                cases=args.cases,
                no_fetch=args.no_fetch,
                dry_run=args.dry_run,
                limit=args.limit,
                force=args.force,
                workers=args.workers,
                refresh_map=args.refresh_map,
                map_only=args.map_only,
                marker_regex=args.marker_regex,
            )
        )
    except Exception as exc:  # infra failures (map model, embed, store) — loud, non-zero
        logger.error(":: seeding aborted: %r", exc)
        return 1
    if stats.dry_run:
        logger.info(
            ":: DRY RUN complete — review %s before seeding for real.", stats.review_dir
        )
    return 0
