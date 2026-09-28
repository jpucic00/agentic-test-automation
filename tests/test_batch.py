"""Batch runner: per-key crash isolation and the comparison table."""
from __future__ import annotations

from pathlib import Path

from ai_test_gen import batch


def _usage(requests: int, tokens_in: int, tokens_out: int, wall: float, nudges: int = 0) -> dict:
    return {
        "agents": [],
        "total": {
            "requests": requests,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "reasoning_only_retries": nudges,
            "wall_s": wall,
        },
    }


def test_format_batch_labels_outcomes_and_sums_totals():
    results = [
        {"issue_key": "NOTE-1", "status": "passed", "heal_attempts": 0,
         "usage": _usage(40, 500_000, 6_000, 240.0), "batch_wall_s": 241.0},
        {"issue_key": "NOTE-2", "status": "passed", "heal_attempts": 1,
         "usage": _usage(60, 700_000, 9_000, 300.0, nudges=4), "batch_wall_s": 301.0},
        {"issue_key": "NOTE-3", "status": "error", "heal_attempts": 0,
         "error": "Planning/generation failed: boom", "batch_wall_s": 5.0},
    ]
    table = batch.format_batch(results)
    lines = table.splitlines()
    assert lines[0].split()[:7] == ["key", "status", "heals", "requests", "in", "out", "nudges"]
    assert "passed" in lines[1] and "healed" in lines[2]
    assert "error" in lines[3] and "Planning/generation failed: boom" in lines[3]
    assert lines[-1].startswith("total") and "2/3 green" in lines[-1]
    assert "1,200,000" in lines[-1] and "100" in lines[-1]  # summed tokens and requests
    assert lines[-1].split()[7] == "4"  # summed reasoning-only nudges


def test_run_one_key_turns_a_crash_into_an_error_row(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(batch, "_configure_logging", lambda key, verbose: tmp_path / f"{key}.log")

    async def _boom(key: str) -> dict:
        raise RuntimeError("xray down")

    monkeypatch.setattr(batch, "process_test_case", _boom)
    result = batch.run_one_key("QA-9", verbose=False)
    assert result["status"] == "error"
    assert "xray down" in result["error"]
    assert result["run_log"].endswith("QA-9.log")
    assert "batch_wall_s" in result


def test_run_one_key_keeps_the_pipeline_result(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(batch, "_configure_logging", lambda key, verbose: tmp_path / f"{key}.log")

    async def _ok(key: str) -> dict:
        return {"issue_key": key, "status": "passed", "heal_attempts": 0}

    monkeypatch.setattr(batch, "process_test_case", _ok)
    result = batch.run_one_key("NOTE-1", verbose=False)
    assert result["status"] == "passed" and result["issue_key"] == "NOTE-1"
