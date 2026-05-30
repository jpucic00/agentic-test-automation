"""Unit tests for ai_test_gen.agents._context — fully local (no network).

Uses the shared ``cfg`` fixture (tests/conftest.py); context/map files are written
into the fixture's tmp_path-backed paths per test.
"""
from __future__ import annotations

from ai_test_gen.agents._context import _safe_read, assemble_system_prompt

_BASE_PROMPT = "# Base agent prompt"
_CONTEXT_TEXT = "PROJECT-CONTEXT-MARKER conventions go here."
_MAP_TEXT = "APPLICATION-MAP-MARKER routes go here."


def _write_context_files(cfg):
    cfg.project_context_path.write_text(_CONTEXT_TEXT)
    cfg.project_map_path.write_text(_MAP_TEXT)


def test_safe_read_returns_text_when_present(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("hello")
    assert _safe_read(p) == "hello"


def test_safe_read_returns_placeholder_when_missing(tmp_path):
    assert _safe_read(tmp_path / "missing.md") == "(no project context provided)"


def test_assemble_includes_context_and_map_when_include_map_true(cfg):
    _write_context_files(cfg)
    out = assemble_system_prompt(cfg, _BASE_PROMPT, include_map=True)
    assert _BASE_PROMPT in out
    assert _CONTEXT_TEXT in out
    assert _MAP_TEXT in out
    assert "# Application Map" in out


def test_assemble_omits_map_when_include_map_false(cfg):
    _write_context_files(cfg)
    out = assemble_system_prompt(cfg, _BASE_PROMPT, include_map=False)
    assert _CONTEXT_TEXT in out
    assert _MAP_TEXT not in out
    assert "# Application Map" not in out


def test_assemble_uses_placeholder_for_missing_context(cfg):
    # Context/map files are intentionally NOT written.
    out = assemble_system_prompt(cfg, _BASE_PROMPT, include_map=True)
    assert "(no project context provided)" in out
