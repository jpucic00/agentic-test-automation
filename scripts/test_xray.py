"""Smoke test for the Xray client — fetch one test case and print it as JSON.

RUNTIME check (needs Jira/Xray access): run on the company laptop, not the
private PC. Confirms steps and expected_results populate from the live tenant.

    uv run python scripts/test_xray.py --issue-key QA-1234
"""
from __future__ import annotations

import argparse

from ai_test_gen.config import load_config
from ai_test_gen.xray_client import XrayClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch one Jira/Xray test case and print it as JSON."
    )
    parser.add_argument("--issue-key", required=True, help="Jira issue key, e.g. QA-1234")
    args = parser.parse_args()

    config = load_config()
    test_case = XrayClient(config).fetch(args.issue_key)
    print(test_case.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
