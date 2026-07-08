"""KB seeding: discover → distill → review files → embed + upsert.

The importable engine behind ``scripts/seed_kb.py`` (RETRIEVAL_MEMORY_PLAN.md §5).
Discovery and context assembly are deterministic (``extract.py``); the Distiller
answers one structured call per test; this module writes a human-readable review
file per record and — unless ``--dry-run`` — embeds ``intent_text`` (only) and
upserts into the project's collection. Stable record ids are computed BEFORE
distilling, so a re-run skips already-stored records without paying LLM calls
(``--force`` re-distills).

Manual cases: the ``@Xray`` annotations name the linked test cases, so unless
``--cases`` overrides the source (a directory of raw-Xray JSON, or explicit
keys), the discovered keys are fetched automatically — from
``LOCAL_TESTCASE_DIR`` when ``TESTCASE_SOURCE=local``, else live from Jira/Xray
when configured. Every key that cannot be loaded is reported per record in the
review output (``--no-fetch`` skips fetching entirely).

Selector ground truth is enforced in code after each distill call: model
selectors are canonicalized to the statically extracted (kind, value) pairs,
model inventions are dropped (and reported), and extracted locators the model
omitted are appended — a record's selectors are exactly the extraction,
enriched by the model's descriptions.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ..config import Config, load_config
from ..models import ManualTestCase
from . import embeddings
from .distiller import DistilledCase, build_distill_message, build_distiller
from .extract import (
    DEFAULT_HELPER_CHAR_CAP,
    DEFAULT_HELPER_DEPTH,
    JavaIndex,
    TestBundle,
    extract_java_tests,
    extract_playwright_specs,
)
from .models import KBRecord, KBSelector, KBSource, SelectorKind, make_record_id

logger = logging.getLogger(__name__)

_EMBED_BATCH = 64
_SOURCE_CODE_CAP = 20_000


@dataclass
class SeedStats:
    project: str
    dry_run: bool
    discovered: int = 0
    skipped_existing: int = 0
    distilled: int = 0
    upserted: int = 0
    cases_loaded: int = 0
    case_misses: dict[str, str] = field(default_factory=dict)
    selectorless: list[str] = field(default_factory=list)
    unresolved_calls: int = 0
    unresolved_locators: int = 0
    dropped_selectors: int = 0
    review_dir: Path | None = None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Self-diagnosing stall watchdog: if anything dangles (a pathological source
    # file, a wedged connection), the process prints every thread's stack to
    # stderr each 120s — a silent hang always names its own culprit.
    import faulthandler

    faulthandler.dump_traceback_later(120, repeat=True)
    config = load_config()
    stats = run_seeding(
        config,
        project=args.project,
        selenium_root=args.selenium,
        playwright_dir=args.playwright,
        cases=args.cases,
        xray_map=args.xray_map,
        dry_run=args.dry_run,
        limit=args.limit,
        force=args.force,
        no_fetch=args.no_fetch,
        helper_depth=args.helper_depth,
        helper_char_cap=args.helper_cap,
    )
    _print_summary(stats)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the per-project test-case knowledge base from an existing corpus."
    )
    parser.add_argument("--project", required=True, help="Jira project key, e.g. QA")
    parser.add_argument(
        "--selenium", type=Path, help="Selenium repo ROOT (tests found via @Xray annotations)"
    )
    parser.add_argument(
        "--playwright", type=Path, help="Directory of hand-written *.spec.ts files"
    )
    parser.add_argument(
        "--cases",
        nargs="*",
        default=[],
        help="A directory of raw-Xray-shaped JSON files, or issue keys to fetch live. "
        "Omitted: the keys named by the @Xray annotations are fetched automatically",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Never fetch manual cases (distill from code only)",
    )
    parser.add_argument(
        "--xray-map",
        type=Path,
        help="JSON fallback mapping {'<ref or Class#method>': 'KEY'} for unannotated tests",
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
        "--helper-depth",
        type=int,
        default=DEFAULT_HELPER_DEPTH,
        help="Bound the call-graph hops followed into helpers "
        "(default: unlimited — the whole in-repo call graph, deduplicated)",
    )
    parser.add_argument(
        "--helper-cap",
        type=int,
        default=DEFAULT_HELPER_CHAR_CAP,
        help="Char budget for helper snippets per record (locator extraction is "
        f"never capped; default {DEFAULT_HELPER_CHAR_CAP})",
    )
    return parser.parse_args(argv)


def run_seeding(
    config: Config,
    *,
    project: str,
    selenium_root: Path | None = None,
    playwright_dir: Path | None = None,
    cases: list[str] | None = None,
    xray_map: Path | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    no_fetch: bool = False,
    helper_depth: int | None = DEFAULT_HELPER_DEPTH,
    helper_char_cap: int = DEFAULT_HELPER_CHAR_CAP,
) -> SeedStats:
    project = project.strip().upper()
    stats = SeedStats(project=project, dry_run=dry_run)

    bundles: list[TestBundle] = []
    if selenium_root:
        logger.info(":: extracting Java tests from %s ...", selenium_root)
        bundles.extend(
            extract_java_tests(
                selenium_root,
                JavaIndex.build(selenium_root),
                helper_depth=helper_depth,
                helper_char_cap=helper_char_cap,
            )
        )
    if playwright_dir:
        logger.info(":: extracting Playwright specs from %s ...", playwright_dir)
        bundles.extend(extract_playwright_specs(playwright_dir))
    _apply_xray_map(bundles, xray_map)
    stats.discovered = len(bundles)
    if limit is not None:
        bundles = bundles[:limit]

    # Deterministic ids exist BEFORE any LLM call → resuming skips paid work.
    planned = [(bundle, _record_id_for(project, bundle)) for bundle in bundles]
    if not force and not dry_run and planned:
        from .store import KBStore

        with KBStore(config.kb_path) as store:
            already = store.existing_ids(project, [record_id for _, record_id in planned])
        stats.skipped_existing = len(already)
        planned = [(b, rid) for b, rid in planned if rid not in already]

    logger.info(
        ":: discovered %d test(s); %d to distill via %s%s",
        stats.discovered,
        len(planned),
        config.distiller_model,
        " (DRY RUN)" if dry_run else "",
    )
    linked_keys = list(dict.fromkeys(b.xray_key for b, _ in planned if b.xray_key))
    case_lookup, case_misses = _load_cases(config, cases or [], linked_keys, no_fetch=no_fetch)
    stats.cases_loaded = len(case_lookup)
    stats.case_misses = case_misses
    review_dir = config.output_dir / "kb_review" / project
    review_dir.mkdir(parents=True, exist_ok=True)
    stats.review_dir = review_dir

    agent = build_distiller(config) if planned else None
    records: list[KBRecord] = []
    for position, (bundle, record_id) in enumerate(planned, start=1):
        case = case_lookup.get(bundle.xray_key or "")
        message = build_distill_message(bundle, case)
        logger.info(":: distilling %s (%d/%d) ...", bundle.ref, position, len(planned))
        distilled = agent.run_sync(message).output  # type: ignore[union-attr]
        selectors, dropped = _enforce_ground_truth(distilled, bundle)
        record = _to_record(project, record_id, bundle, distilled, selectors, case)
        records.append(record)
        stats.distilled += 1
        if not record.selectors:
            stats.selectorless.append(record.title)
        stats.unresolved_calls += bundle.unresolved_count  # pre-display-cap, truthful
        stats.unresolved_locators += len(bundle.unresolved_locators)
        stats.dropped_selectors += len(dropped)
        miss_reason = case_misses.get(bundle.xray_key or "") if case is None else None
        _write_review_file(review_dir, record, bundle, case, miss_reason, dropped)
        logger.info(":: distilled %s (%s)", record.title, record.xray_key or bundle.ref)

    if records and not dry_run:
        vectors: list[list[float]] = []
        for start in range(0, len(records), _EMBED_BATCH):
            chunk = records[start : start + _EMBED_BATCH]
            vectors.extend(embeddings.embed(config, [r.intent_text for r in chunk]))
        from .store import KBStore

        with KBStore(config.kb_path) as store:
            store.upsert(project, records, vectors)
        stats.upserted = len(records)

    _write_summary(stats)
    return stats


def _record_id_for(project: str, bundle: TestBundle) -> str:
    source = _source_of(bundle)
    return make_record_id(project, source, bundle.xray_key or bundle.ref)


def _source_of(bundle: TestBundle) -> KBSource:
    return "selenium-import" if bundle.language == "java" else "playwright-import"


def _inner_literal(value: str) -> str | None:
    """``By.id("login-email")`` / ``getByTestId('save')`` → the quoted literal."""
    match = re.search(r'"([^"]*)"', value) or re.search(r"'([^']*)'", value)
    return match.group(1) if match else None


def _enforce_ground_truth(
    distilled: DistilledCase, bundle: TestBundle
) -> tuple[list[KBSelector], list[str]]:
    """Extraction is ground truth (plan §5.3) — enforced in code, not by prompt.

    Model selectors are matched against the extracted locators (verbatim value
    or bare inner literal) and canonicalized to the extracted (kind, value),
    keeping the model's description/route. Anything with no extracted basis is
    DROPPED and reported. Extracted locators the model omitted are appended, so
    the record's selectors are exactly the extraction, enriched by the model.
    """
    canonical: dict[str, tuple[str, str, str]] = {}
    for loc in bundle.locators:
        entry = (loc.kind, loc.value, loc.declared_in)
        canonical.setdefault(loc.value, entry)
        inner = _inner_literal(loc.value)
        if inner:
            canonical.setdefault(inner, entry)
    kept: list[KBSelector] = []
    dropped: list[str] = []
    seen: set[tuple[str, str]] = set()
    for selector in distilled.selectors:
        value = selector.value.strip()
        match = canonical.get(value) or canonical.get(_inner_literal(value) or "")
        if match is None:
            dropped.append(f"{selector.kind}: {selector.value}")
            continue
        kind, canonical_value, _ = match
        key = (kind, canonical_value)
        if key in seen:
            continue
        seen.add(key)
        kept.append(
            KBSelector(
                kind=cast(SelectorKind, kind),
                value=canonical_value,
                description=selector.description,
                route=selector.route,
            )
        )
    for loc in bundle.locators:
        key = (loc.kind, loc.value)
        if key not in seen:
            seen.add(key)
            note = "; template — placeholders filled at runtime" if loc.template else ""
            kept.append(
                KBSelector(
                    kind=cast(SelectorKind, loc.kind),
                    value=loc.value,
                    description=f"declared in {loc.declared_in}{note}",
                )
            )
    return kept, dropped


def _to_record(
    project: str,
    record_id: str,
    bundle: TestBundle,
    distilled: DistilledCase,
    selectors: list[KBSelector],
    case: ManualTestCase | None,
) -> KBRecord:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    return KBRecord(
        record_id=record_id,
        project_key=project,
        xray_key=bundle.xray_key or "",
        title=distilled.title,
        intent_text=distilled.intent_text,
        steps=distilled.steps,
        manual_steps=list(case.steps) if case else [],
        manual_expected=list(case.expected_results) if case else [],
        selectors=selectors,
        routes=distilled.routes,
        spec=bundle.code if bundle.language == "ts" else "",
        source_code=bundle.source_code[:_SOURCE_CODE_CAP],
        source_lang=bundle.language,
        helper_refs=bundle.helper_refs,
        outcome="legacy",
        source=_source_of(bundle),
        created_at=now,
        updated_at=now,
    )


def _apply_xray_map(bundles: list[TestBundle], xray_map: Path | None) -> None:
    """Fallback linking for unannotated tests (the real repo is fully annotated)."""
    if xray_map is None:
        return
    import json

    mapping = json.loads(xray_map.read_text())
    for bundle in bundles:
        if bundle.xray_key:
            continue
        bundle.xray_key = mapping.get(bundle.ref) or mapping.get(
            f"{bundle.class_name}#{bundle.test_name}"
        )


def _load_cases(
    config: Config, cases: list[str], linked_keys: list[str], *, no_fetch: bool = False
) -> tuple[dict[str, ManualTestCase], dict[str, str]]:
    """Manual cases per key + per-key miss reasons (surfaced in review files).

    ``--cases`` overrides the source: one directory of raw-Xray JSON files, or
    explicit issue keys to fetch live. Without it, the keys named by the
    ``@Xray`` annotations load automatically — locally when
    ``TESTCASE_SOURCE=local``, else live from Jira/Xray when configured. Every
    load is per-key fault-tolerant: one deleted ticket must not sink a corpus run.
    """
    misses: dict[str, str] = {}
    if cases:
        first = Path(cases[0])
        if len(cases) == 1 and first.is_dir():
            local_config = dataclasses.replace(config, local_testcase_dir=first)
            keys = sorted(path.stem for path in first.glob("*.json"))
            loaded = _load_local(local_config, keys, misses)
            for key in linked_keys:
                if key not in loaded:
                    misses.setdefault(key, f"no {key}.json in --cases directory {first}")
            return loaded, misses
        return _fetch_live(config, list(cases), misses), misses
    if no_fetch:
        return {}, misses
    if not linked_keys:
        return {}, misses
    if config.testcase_source == "local" and config.local_testcase_dir is not None:
        return _load_local(config, linked_keys, misses), misses
    if config.jira_base_url and config.jira_email and config.jira_token:
        return _fetch_live(config, linked_keys, misses), misses
    reason = (
        "no case source: --cases not given, TESTCASE_SOURCE!=local and Jira is not configured"
    )
    for key in linked_keys:
        misses[key] = reason
    logger.warning(
        ":: %d linked manual case(s) NOT loaded — %s", len(linked_keys), reason
    )
    return {}, misses


def _load_local(
    config: Config, keys: list[str], misses: dict[str, str]
) -> dict[str, ManualTestCase]:
    from ..local_testcases import load_local_test_case

    logger.info(
        ":: loading %d manual case(s) from %s", len(keys), config.local_testcase_dir
    )
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
            logger.info(":: case %s fetched (%d steps)", key, len(loaded[key].steps))
        except Exception as exc:  # per-key tolerant — reported, never fatal
            misses[key] = f"fetch failed: {exc}"
            logger.warning(":: case %s not fetched: %s", key, exc)
    return loaded


def _write_review_file(
    review_dir: Path,
    record: KBRecord,
    bundle: TestBundle,
    case: ManualTestCase | None,
    case_miss_reason: str | None,
    dropped_selectors: list[str],
) -> None:
    """One human-readable file per record — the distillation-quality feedback loop."""
    name = _slug(f"{record.xray_key or bundle.test_name}-{bundle.test_name}") + ".md"
    selectors = "\n".join(
        f"- `{s.kind}`: `{s.value}`" + (f" — {s.description}" if s.description else "")
        + (f" [{s.route}]" if s.route else "")
        for s in record.selectors
    )
    unresolved = [ref for ref in bundle.helper_refs if ref.startswith("unresolved:")]
    truncated = [ref for ref in bundle.helper_refs if ref.startswith("truncated:")]
    resolved = [
        ref
        for ref in bundle.helper_refs
        if not ref.startswith("unresolved:") and not ref.startswith("truncated:")
    ]
    if case is not None:
        case_lines = [f"{record.xray_key}: {case.title}", "", "Steps (verbatim):"]
        case_lines += [f"{i + 1}. {s}" for i, s in enumerate(case.steps)] or ["(none)"]
        case_lines.append("Expected results (verbatim):")
        case_lines += [f"{i + 1}. {e}" for i, e in enumerate(case.expected_results)] or ["(none)"]
        case_block = "\n".join(case_lines)
    elif record.xray_key:
        case_block = (
            f"**NOT LOADED** ({case_miss_reason or 'no reason recorded'}) — REVIEW: the "
            "distillation ran without the manual steps"
        )
    else:
        case_block = "(test not linked to an Xray key)"
    dropped_block = (
        "\n## Model selectors DROPPED (no basis in extracted ground truth)\n"
        + "\n".join(f"- **{d}**" for d in dropped_selectors)
        if dropped_selectors
        else ""
    )
    unresolved_locator_block = (
        "\n## Locators with unresolvable values (flagged, never guessed)\n"
        + "\n".join(f"- **{u}**" for u in bundle.unresolved_locators)
        if bundle.unresolved_locators
        else ""
    )
    body = f"""# {record.title}

| | |
|---|---|
| Xray key | {record.xray_key or "(unlinked)"} |
| Source | {record.source} ({bundle.ref}) |
| Record id | {record.record_id} |

## Linked manual case (as on Jira/Xray at distillation time — diff base for later ticket edits)
{case_block}

## intent_text (what gets embedded)
{record.intent_text}

## Steps (reverse-engineered from the code — what the automated test actually does)
{chr(10).join(f"{i + 1}. {s}" for i, s in enumerate(record.steps)) or "(none)"}

## Selectors (extracted ground truth, model-enriched)
{selectors or "(none — REVIEW: no locators extracted)"}
{dropped_block}{unresolved_locator_block}

## Routes
{", ".join(record.routes) or "(none)"}

## Helper resolution
{chr(10).join(f"- {r}" for r in resolved) or "(no helpers)"}
{chr(10).join(f"- **{u}**" for u in unresolved)}
{chr(10).join(f"- {t}" for t in truncated)}

## Source (full bundle, {len(record.source_code)} chars)
```{record.source_lang}
{record.source_code}
```
"""
    (review_dir / name).write_text(body)


def _write_summary(stats: SeedStats) -> None:
    if stats.review_dir is None:
        return
    lines = [
        f"# Seeding summary — {stats.project} ({'DRY RUN' if stats.dry_run else 'live'})",
        "",
        f"- discovered: {stats.discovered}",
        f"- skipped (already stored): {stats.skipped_existing}",
        f"- distilled: {stats.distilled}",
        f"- upserted: {stats.upserted}",
        f"- manual cases loaded: {stats.cases_loaded}",
        f"- manual cases NOT loaded: {len(stats.case_misses)}"
        + (
            " — " + "; ".join(f"{k} ({v})" for k, v in stats.case_misses.items())
            if stats.case_misses
            else ""
        ),
        f"- unresolved helper calls: {stats.unresolved_calls}",
        f"- locators with unresolvable values: {stats.unresolved_locators}",
        f"- model selectors dropped (no extracted basis): {stats.dropped_selectors}",
        f"- records without selectors: {len(stats.selectorless)}"
        + (" — " + "; ".join(stats.selectorless) if stats.selectorless else ""),
    ]
    (stats.review_dir / "summary.md").write_text("\n".join(lines) + "\n")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80]


def _print_summary(stats: SeedStats) -> None:
    mode = "DRY RUN — nothing embedded or stored" if stats.dry_run else "live"
    logger.info(
        ":: %s | discovered %d, skipped %d, distilled %d, upserted %d, cases %d/%d (%s)",
        stats.project,
        stats.discovered,
        stats.skipped_existing,
        stats.distilled,
        stats.upserted,
        stats.cases_loaded,
        stats.cases_loaded + len(stats.case_misses),
        mode,
    )
    if stats.review_dir is not None:
        logger.info(":: review files: %s", stats.review_dir)
