"""Seed the per-project test-case knowledge base from an existing corpus.

Thin CLI wrapper — the engine lives in ``ai_test_gen.rag.seeding`` (importable,
unit-tested). See RETRIEVAL_MEMORY_PLAN.md §5 and SETUP.md §7.2 for the dry-run
review loop.

Phases: deterministic discovery (marker regex + parity accounting) → the cached
suite map (Mapper agent) → one bounded Distiller exploration per test → selector
verification with one bounce round → review files + honesty summary → embed +
upsert (skipped by ``--dry-run``).

Examples:
  uv run python scripts/seed_kb.py --project NOTE \\
      --selenium packages/demo-notes-app/legacy-suite \\
      --cases packages/demo-notes-app/test-cases --dry-run

  uv run python scripts/seed_kb.py --project NOTE \\
      --selenium packages/demo-notes-app/legacy-suite --map-only --dry-run

  uv run python scripts/seed_kb.py --project QA --selenium ~/work/selenium-suite --workers 4
"""
from __future__ import annotations

import sys

from ai_test_gen.rag.seeding import main

if __name__ == "__main__":
    sys.exit(main())
