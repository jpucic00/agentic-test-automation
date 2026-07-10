"""Offline KB seeding — phase 0: the suite map (RETRIEVAL_MEMORY_PLAN.md §5.2).

Builds one browsable suite map per project from an existing test corpus and (unless
``--dry-run``) upserts the map's lifecycle + conventions sections as ``kind=knowledge``
records the Planner can retrieve. This is OFFLINE seeding: it reads the corpus and calls
the gateway (Mapper model + embeddings); it never touches the app under test.

    uv run python scripts/seed_kb.py --project NOTE \
        --selenium packages/demo-notes-app/legacy-suite --map-only --dry-run

    uv run python scripts/seed_kb.py --project QA --selenium ~/work/selenium-suite --refresh-map

Flags:
  --project KEY        Jira/collection key (required) — records route to kb_<KEY>.
  --selenium PATH      Root of the Selenium/Java suite to map.
  --playwright PATH    Directory of Playwright specs to map.
  --map-only           Build the map (+ knowledge records); skip the pending distill phase note.
  --refresh-map        Ignore the section cache and regenerate every section.
  --dry-run            Write the map, but do NOT embed/upsert anything.
  --marker-regex RE    Override TEST_MARKER_REGEX for test discovery.

The per-test distillation phase (ReconstructedPlan records, verification bounce loop, workers)
lands with the Agentic-Distiller task and will extend this same script.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from ai_test_gen.config import load_config
from ai_test_gen.rag import embeddings
from ai_test_gen.rag.discover import discover_tests, render_discovery_summary
from ai_test_gen.rag.mapper import build_suite_map
from ai_test_gen.rag.store import KBStore

logger = logging.getLogger("seed_kb")


def _resolve(path_arg: str) -> Path:
    path = Path(path_arg).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline KB seeding — suite map (phase 0).")
    parser.add_argument("--project", required=True, help="Jira/collection key, e.g. QA or NOTE")
    parser.add_argument("--selenium", help="Root of the Selenium/Java suite to map")
    parser.add_argument("--playwright", help="Directory of Playwright specs to map")
    parser.add_argument("--map-only", action="store_true", help="Just the map + knowledge records")
    parser.add_argument("--refresh-map", action="store_true", help="Ignore the section cache")
    parser.add_argument("--dry-run", action="store_true", help="Write the map; do not embed/upsert")
    parser.add_argument("--marker-regex", help="Override TEST_MARKER_REGEX for discovery")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging")
    return parser.parse_args(argv)


def _upsert_knowledge(config, project: str, records) -> None:
    """Embed each knowledge record's intent_text and upsert into kb_<project>."""
    if not records:
        logger.info("No core-knowledge records to upsert (lifecycle/conventions were empty).")
        return
    texts = [r.intent_text for r in records]
    vectors = embeddings.embed(config, texts)
    with KBStore(config.kb_path) as store:
        store.upsert(project, records, vectors)
    logger.info("Upserted %d core-knowledge record(s) into kb_%s.", len(records), project.upper())


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.selenium and not args.playwright:
        logger.error("Provide at least one corpus root: --selenium and/or --playwright.")
        return 2

    config = load_config()
    project = args.project.strip().upper()
    selenium_root = _resolve(args.selenium) if args.selenium else None
    playwright_dir = _resolve(args.playwright) if args.playwright else None
    marker_regex = args.marker_regex or config.test_marker_regex

    discovery = discover_tests(
        project,
        selenium_root=selenium_root,
        playwright_dir=playwright_dir,
        marker_regex=marker_regex,
    )
    logger.info("Discovery:\n%s", render_discovery_summary(discovery))

    result = asyncio.run(
        build_suite_map(
            config,
            project,
            selenium_root=selenium_root,
            playwright_dir=playwright_dir,
            discovery=discovery,
            refresh=args.refresh_map,
        )
    )
    logger.info(
        "Suite map written to %s (%s; refreshed: %s; %d file(s) read, %d tool call(s)).",
        result.path,
        "from cache" if result.from_cache else "generated",
        ", ".join(result.stale_sections) or "none",
        len(result.files_opened),
        result.tool_calls,
    )
    if result.unresolved_citations:
        logger.warning(
            "%d citation(s) did not resolve to a corpus file (flagged in the map): %s",
            len(result.unresolved_citations),
            ", ".join(result.unresolved_citations),
        )

    if args.dry_run:
        logger.info("--dry-run: map written, nothing embedded/upserted. Review %s.", result.path)
    else:
        try:
            _upsert_knowledge(config, project, result.knowledge_records)
        except Exception as exc:  # noqa: BLE001 — seeding tool: report and fail loudly
            logger.error("Embedding/upsert of knowledge records failed: %s", exc)
            return 1

    if not args.map_only:
        logger.info(
            "Per-test distillation is not wired yet (Agentic-Distiller task). "
            "Re-run with --map-only to suppress this note."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
