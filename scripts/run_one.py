"""Thin CLI wrapper: process one Jira/Xray test case end-to-end.

Equivalent to ``python -m ai_test_gen.orchestrator <issue_key> [--verbose]``; provided
as a ``scripts/`` entry point for parity with the other runnable scripts. Company-laptop
runtime only (needs the gateway, Xray, staging, and GitLab).

    uv run python scripts/run_one.py QA-1234 --verbose
"""
from __future__ import annotations

from ai_test_gen.orchestrator import main

if __name__ == "__main__":
    main()
