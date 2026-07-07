"""KB seeding: discover → distill → review files → embed + upsert.

The importable engine behind ``scripts/seed_kb.py`` (RETRIEVAL_MEMORY_PLAN.md §5).
Discovery and context assembly are deterministic (``extract.py``); the Distiller
answers one structured call per test; this module writes a human-readable review
file per record and — unless ``--dry-run`` — embeds ``intent_text`` (only) and
upserts into the project's collection. Stable record ids are computed BEFORE
distilling, so a re-run skips already-stored records without paying LLM calls
(``--force`` re-distills).
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import Config, load_config
from ..models import ManualTestCase
from . import embeddings
from .distiller import DistilledCase, build_distill_message, build_distiller
from .extract import JavaIndex, TestBundle, extract_java_tests, extract_playwright_specs
from .models import KBRecord, KBSource, make_record_id

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
    selectorless: list[str] = field(default_factory=list)
    unresolved_calls: int = 0
    review_dir: Path | None = None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
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
        help="A directory of raw-Xray-shaped JSON files, or issue keys to fetch live",
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
) -> SeedStats:
    project = project.strip().upper()
    stats = SeedStats(project=project, dry_run=dry_run)

    bundles: list[TestBundle] = []
    if selenium_root:
        bundles.extend(extract_java_tests(selenium_root, JavaIndex.build(selenium_root)))
    if playwright_dir:
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

    case_lookup = _load_cases(config, cases or [])
    review_dir = config.output_dir / "kb_review" / project
    review_dir.mkdir(parents=True, exist_ok=True)
    stats.review_dir = review_dir

    agent = build_distiller(config) if planned else None
    records: list[KBRecord] = []
    for bundle, record_id in planned:
        case = case_lookup.get(bundle.xray_key or "")
        message = build_distill_message(bundle, case)
        distilled = agent.run_sync(message).output  # type: ignore[union-attr]
        record = _to_record(project, record_id, bundle, distilled)
        records.append(record)
        stats.distilled += 1
        if not record.selectors:
            stats.selectorless.append(record.title)
        stats.unresolved_calls += sum(
            1 for ref in bundle.helper_refs if ref.startswith("unresolved:")
        )
        _write_review_file(review_dir, record, bundle)
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


def _to_record(
    project: str, record_id: str, bundle: TestBundle, distilled: DistilledCase
) -> KBRecord:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    return KBRecord(
        record_id=record_id,
        project_key=project,
        xray_key=bundle.xray_key or "",
        title=distilled.title,
        intent_text=distilled.intent_text,
        steps=distilled.steps,
        selectors=distilled.selectors,
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


def _load_cases(config: Config, cases: list[str]) -> dict[str, ManualTestCase]:
    """--cases: one directory of raw-Xray JSON files, or issue keys fetched live."""
    if not cases:
        return {}
    from ..local_testcases import load_local_test_case

    first = Path(cases[0])
    if len(cases) == 1 and first.is_dir():
        local_config = dataclasses.replace(config, local_testcase_dir=first)
        keys = sorted(path.stem for path in first.glob("*.json"))
        return {key: load_local_test_case(local_config, key) for key in keys}

    from ..xray_client import XrayClient  # live fetch — company network only

    client = XrayClient(config)
    return {key: client.fetch(key) for key in cases}


def _write_review_file(review_dir: Path, record: KBRecord, bundle: TestBundle) -> None:
    """One human-readable file per record — the distillation-quality feedback loop."""
    name = _slug(f"{record.xray_key or bundle.test_name}-{bundle.test_name}") + ".md"
    selectors = "\n".join(
        f"- `{s.kind}`: `{s.value}`" + (f" — {s.description}" if s.description else "")
        + (f" [{s.route}]" if s.route else "")
        for s in record.selectors
    )
    unresolved = [ref for ref in bundle.helper_refs if ref.startswith("unresolved:")]
    resolved = [ref for ref in bundle.helper_refs if not ref.startswith("unresolved:")]
    body = f"""# {record.title}

| | |
|---|---|
| Xray key | {record.xray_key or "(unlinked)"} |
| Source | {record.source} ({bundle.ref}) |
| Record id | {record.record_id} |

## intent_text (what gets embedded)
{record.intent_text}

## Steps
{chr(10).join(f"{i + 1}. {s}" for i, s in enumerate(record.steps)) or "(none)"}

## Selectors (ground truth from static extraction)
{selectors or "(none — REVIEW: no locators extracted)"}

## Routes
{", ".join(record.routes) or "(none)"}

## Helper resolution
{chr(10).join(f"- {r}" for r in resolved) or "(no helpers)"}
{chr(10).join(f"- **{u}**" for u in unresolved)}

## Source excerpt
```{record.source_lang}
{record.source_code[:1500]}
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
        f"- unresolved helper calls: {stats.unresolved_calls}",
        f"- records without selectors: {len(stats.selectorless)}"
        + (" — " + "; ".join(stats.selectorless) if stats.selectorless else ""),
    ]
    (stats.review_dir / "summary.md").write_text("\n".join(lines) + "\n")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80]


def _print_summary(stats: SeedStats) -> None:
    mode = "DRY RUN — nothing embedded or stored" if stats.dry_run else "live"
    logger.info(
        ":: %s | discovered %d, skipped %d, distilled %d, upserted %d (%s)",
        stats.project,
        stats.discovered,
        stats.skipped_existing,
        stats.distilled,
        stats.upserted,
        mode,
    )
    if stats.review_dir is not None:
        logger.info(":: review files: %s", stats.review_dir)
