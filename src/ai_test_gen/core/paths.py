"""Repository-root anchor shared by every module that resolves a path (output/, .env, configs)."""

from pathlib import Path

# src/ai_test_gen/core/paths.py → parents: core, ai_test_gen, src, <repo root>
PROJECT_ROOT = Path(__file__).resolve().parents[3]
