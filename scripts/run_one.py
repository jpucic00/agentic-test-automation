"""Thin CLI wrapper: process one test case end-to-end.

Equivalent to ``python -m ai_test_gen.orchestrator <key> [--verbose]``; provided as a
``scripts/`` entry point for parity with the other runnable scripts. A run needs a model
gateway and the app under test reachable. The test case comes from Jira/Xray by default, or
— with ``TESTCASE_SOURCE=local`` — from a local raw-Xray-shaped JSON file (e.g. the bundled
demo's ``NOTE-2``); see the "Demo profile" block in ``.env.example``.

    uv run python scripts/run_one.py QA-1234 --verbose     # xray source (default)
    uv run python scripts/run_one.py NOTE-2 --verbose      # local source (bundled demo)
"""
from __future__ import annotations

from ai_test_gen.orchestrator import main

if __name__ == "__main__":
    main()
