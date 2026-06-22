"""Unit tests for ai_test_gen.agents._context — fully local (no network).

Uses the shared ``cfg`` fixture (tests/conftest.py); context/map files are written
into the fixture's tmp_path-backed paths per test.
"""
from __future__ import annotations

import logging

import pytest

from ai_test_gen.agents._context import (
    _load_context_file,
    agent_request_limit,
    agent_retries,
    assemble_system_prompt,
    reasoning_effort,
)

_BASE_PROMPT = "# Base agent prompt"
_CONTEXT_TEXT = "PROJECT-CONTEXT-MARKER conventions go here."
_MAP_TEXT = "APPLICATION-MAP-MARKER routes go here."

_CONTEXT_LOGGER = "ai_test_gen.agents._context"


def _write_context_files(cfg):
    cfg.project_context_path.write_text(_CONTEXT_TEXT)
    cfg.project_map_path.write_text(_MAP_TEXT)


def test_load_context_file_returns_text_when_present(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("hello")
    assert _load_context_file(p) == "hello"


def test_load_context_file_returns_placeholder_when_missing(tmp_path):
    assert _load_context_file(tmp_path / "missing.md") == "(no project context provided)"


def test_html_comments_stripped_from_assembled_prompt(cfg):
    # Comments are author guidance ("fill this in"), not app facts — to a model they
    # read as instructions with system-prompt authority, so they must never be injected.
    cfg.project_context_path.write_text(
        "real rule A\n<!-- GUIDANCE-MARKER: replace every\nplaceholder below -->\nreal rule B"
    )
    cfg.project_map_path.write_text(_MAP_TEXT)
    out = assemble_system_prompt(cfg, _BASE_PROMPT, include_map=True)
    assert "GUIDANCE-MARKER" not in out
    assert "real rule A" in out
    assert "real rule B" in out


def test_template_placeholders_trigger_warning_with_file_and_count(cfg, caplog):
    # Both template generations must be detected: legacy [REPLACE/[EXAMPLE markers and
    # the current <e.g. …> / header style. Markers inside comments count too (raw scan).
    cfg.project_context_path.write_text(
        "[REPLACE WITH YOUR DESCRIPTION]\nEmail pattern: <e.g. qa@example.com>"
    )
    cfg.project_map_path.write_text(_MAP_TEXT)
    with caplog.at_level(logging.WARNING, logger=_CONTEXT_LOGGER):
        assemble_system_prompt(cfg, _BASE_PROMPT, include_map=True)
    warnings = [r for r in caplog.records if "placeholder" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "project_context.md" in msg
    assert "2 template placeholder marker(s)" in msg


def test_filled_files_produce_no_placeholder_warning(cfg, caplog):
    _write_context_files(cfg)  # realistic filled content, no markers
    with caplog.at_level(logging.WARNING, logger=_CONTEXT_LOGGER):
        assemble_system_prompt(cfg, _BASE_PROMPT, include_map=True)
    assert not [r for r in caplog.records if "placeholder" in r.getMessage()]


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


def test_agent_retries_default_env_and_invalid(monkeypatch):
    monkeypatch.delenv("AGENT_MCP_RETRIES", raising=False)
    assert agent_retries() == 5
    monkeypatch.setenv("AGENT_MCP_RETRIES", "8")
    assert agent_retries() == 8
    monkeypatch.setenv("AGENT_MCP_RETRIES", "nope")  # invalid -> default
    assert agent_retries() == 5


def test_agent_request_limit_default_env_and_invalid(monkeypatch):
    monkeypatch.delenv("AGENT_REQUEST_LIMIT", raising=False)
    assert agent_request_limit() == 300
    monkeypatch.setenv("AGENT_REQUEST_LIMIT", "500")
    assert agent_request_limit() == 500
    monkeypatch.setenv("AGENT_REQUEST_LIMIT", "x")  # invalid -> default
    assert agent_request_limit() == 300


def test_reasoning_effort_unset_returns_none(monkeypatch):
    monkeypatch.delenv("PLANNER_REASONING_EFFORT", raising=False)
    assert reasoning_effort("PLANNER_REASONING_EFFORT") is None


def test_reasoning_effort_valid_value_warns_about_gateway_support(monkeypatch, caplog):
    # The knob must never be silent: gateways can drop unknown params, so a set value
    # always reminds that step0d must have proven support.
    monkeypatch.setenv("PLANNER_REASONING_EFFORT", "High")
    with caplog.at_level(logging.WARNING, logger=_CONTEXT_LOGGER):
        assert reasoning_effort("PLANNER_REASONING_EFFORT") == "high"
    warning = [r.getMessage() for r in caplog.records if "REASONING_EFFORT" in r.getMessage()]
    assert warning and "step0d_verify_reasoning_effort" in warning[0]


def test_reasoning_effort_invalid_value_fails_fast(monkeypatch):
    monkeypatch.setenv("HEALER_REASONING_EFFORT", "max")
    with pytest.raises(ValueError, match="HEALER_REASONING_EFFORT"):
        reasoning_effort("HEALER_REASONING_EFFORT")
