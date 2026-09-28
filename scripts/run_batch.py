"""Thin CLI wrapper: process several test cases one after another.

Equivalent to ``python -m ai_test_gen.batch <key> [<key> ...]``. Each key gets its own run log;
a failing key is recorded as ``error`` and the batch continues. Ends with one comparison table
(status, heals, requests, tokens, wall time per key) and a JSON file with every result.

    uv run python scripts/run_batch.py NOTE-1 NOTE-2 NOTE-3 NOTE-4 NOTE-5 NOTE-6
"""
from __future__ import annotations

from ai_test_gen.batch import main

if __name__ == "__main__":
    main()
