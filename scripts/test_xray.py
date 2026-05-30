"""Smoke test for the Xray client — fetch one test case and print it as JSON.

RUNTIME check (needs Jira/Xray access): run on the company laptop, not the
private PC. Confirms steps and expected_results populate from the live tenant.

    uv run python scripts/test_xray.py --issue-key QA-1234
    uv run python scripts/test_xray.py --issue-key QA-1234 --raw   # diagnose empty steps
"""
from __future__ import annotations

import argparse
import json

from ai_test_gen.config import load_config
from ai_test_gen.xray_client import XrayClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch one Jira/Xray test case and print it as JSON."
    )
    parser.add_argument("--issue-key", required=True, help="Jira issue key, e.g. QA-1234")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Dump a diagnostic of where manual steps live (field shapes + Xray Raven "
        "endpoints) instead of the normalized test case. Use when steps come back empty.",
    )
    args = parser.parse_args()

    config = load_config()
    client = XrayClient(config)
    if args.raw:
        print(json.dumps(client.diagnose_steps(args.issue_key), indent=2, default=str))
        return
    test_case = client.fetch(args.issue_key)
    print(test_case.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
