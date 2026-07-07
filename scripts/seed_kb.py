"""Seed the per-project test-case knowledge base from an existing corpus.

Thin CLI wrapper — the engine lives in ``ai_test_gen.rag.seeding`` (importable,
unit-tested). See RETRIEVAL_MEMORY_PLAN.md §5 and SETUP.md for the dry-run
review loop.

Examples:
  uv run python scripts/seed_kb.py --project NOTE \\
      --selenium packages/demo-notes-app/legacy-suite \\
      --playwright packages/demo-notes-app/legacy-suite/playwright \\
      --cases packages/demo-notes-app/test-cases --dry-run --limit 5

  uv run python scripts/seed_kb.py --project QA --selenium /corpus/selenium
"""
from __future__ import annotations

import sys

from ai_test_gen.rag.seeding import main

if __name__ == "__main__":
    sys.exit(main())
