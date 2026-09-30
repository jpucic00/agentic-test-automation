"""Process several test cases one after another and print one comparison table.

Each key runs through the normal pipeline (``orchestrator.process_test_case``) with its own
run log under ``output/runs/``. A failing key never stops the batch: its exception is logged
with a traceback and recorded as status ``error``. At the end the batch prints one row per key
(status, heal attempts, model requests, tokens, reasoning-only nudges, wall time) plus totals,
and writes every result — including the per-agent ``usage`` records — to
``output/runs/batch-<stamp>.json`` so two batches (e.g. before and after a prompt change) can be
compared.

Keys run strictly one at a time: they share ``output/`` (snapshots, test results), and the way
to scale out is one CI job per key, not threads here.

    uv run python scripts/run_batch.py NOTE-1 NOTE-2 NOTE-3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.usage import format_duration
from ..orchestrator import PROJECT_ROOT, _configure_logging, process_test_case

logger = logging.getLogger(__name__)


def _outcome(result: dict[str, Any]) -> str:
    """``healed`` for a pass that needed the Healer; otherwise the run's own status."""
    status = str(result.get("status", "error"))
    if status == "passed" and result.get("heal_attempts", 0):
        return "healed"
    return status


def _note(result: dict[str, Any], width: int = 70) -> str:
    """Short last column: the MR URL, the heal verdict or the error, whichever applies."""
    text = result.get("mr_url") or result.get("heal_verdict") or result.get("error") or ""
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def format_batch(results: list[dict[str, Any]]) -> str:
    """Aligned table: one row per key, then a total row."""
    header = ("key", "status", "heals", "requests", "in", "out", "nudges", "wall", "note")
    rows: list[tuple[str, ...]] = [header]
    sums = {"heals": 0, "requests": 0, "in": 0, "out": 0, "nudges": 0, "wall": 0.0}
    for result in results:
        total = (result.get("usage") or {}).get("total") or {}
        heals = int(result.get("heal_attempts", 0) or 0)
        requests = int(total.get("requests", 0))
        tokens_in = int(total.get("input_tokens", 0))
        tokens_out = int(total.get("output_tokens", 0))
        nudges = int(total.get("reasoning_only_retries", 0))
        wall = float(result.get("batch_wall_s", total.get("wall_s", 0.0)))
        sums["heals"] += heals
        sums["requests"] += requests
        sums["in"] += tokens_in
        sums["out"] += tokens_out
        sums["nudges"] += nudges
        sums["wall"] += wall
        rows.append(
            (
                str(result.get("issue_key", "?")),
                _outcome(result),
                str(heals),
                str(requests),
                f"{tokens_in:,}",
                f"{tokens_out:,}",
                str(nudges),
                format_duration(wall),
                _note(result),
            )
        )
    passed = sum(1 for r in results if _outcome(r) in ("passed", "healed"))
    rows.append(
        (
            "total",
            f"{passed}/{len(results)} green",
            str(sums["heals"]),
            str(sums["requests"]),
            f"{sums['in']:,}",
            f"{sums['out']:,}",
            str(sums["nudges"]),
            format_duration(sums["wall"]),
            "",
        )
    )
    numeric = {2, 3, 4, 5, 6, 7}
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    return "\n".join(
        "  ".join(
            cell.rjust(widths[i]) if i in numeric else cell.ljust(widths[i])
            for i, cell in enumerate(row)
        ).rstrip()
        for row in rows
    )


def run_one_key(key: str, *, verbose: bool) -> dict[str, Any]:
    """Run one key with its own run log; never raises — a crash becomes status ``error``."""
    log_path = _configure_logging(key, verbose=verbose)
    logger.info("Run log: %s", log_path)
    started = time.monotonic()
    try:
        result = asyncio.run(process_test_case(key))
    except Exception as exc:  # noqa: BLE001 — one bad key must not stop the batch
        logger.error("[%s] Run crashed:\n%s", key, traceback.format_exc())
        result = {"issue_key": key, "status": "error", "heal_attempts": 0, "error": repr(exc)}
    result["batch_wall_s"] = round(time.monotonic() - started, 1)
    result["run_log"] = str(log_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the pipeline for several test cases, one after another."
    )
    parser.add_argument("issue_keys", nargs="+", help="e.g. NOTE-1 NOTE-2 or QA-1 QA-2")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--json-out",
        type=Path,
        help="where to write all results (default: output/runs/batch-<timestamp>.json)",
    )
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    json_out: Path = args.json_out or PROJECT_ROOT / "output" / "runs" / f"batch-{stamp}.json"
    results: list[dict[str, Any]] = []
    for index, key in enumerate(args.issue_keys, start=1):
        print(f"\n=== [{index}/{len(args.issue_keys)}] {key} ===", flush=True)
        results.append(run_one_key(key, verbose=args.verbose))
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(results, indent=2, default=str))  # after every key

    print("\n=== Batch summary ===")
    print(format_batch(results))
    print(f"\nAll results (incl. per-agent usage): {json_out}")


if __name__ == "__main__":
    main()
