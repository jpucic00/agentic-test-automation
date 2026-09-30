"""Unit tests for ai_test_gen.agents.runtime.context — fully local (no network).

Uses the shared ``cfg`` fixture (tests/conftest.py); context/map files are written
into the fixture's tmp_path-backed paths per test.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ai_test_gen.agents.generator import build_generator
from ai_test_gen.agents.healer import build_healer
from ai_test_gen.agents.planner import build_planner
from ai_test_gen.agents.runtime.context import (
    _load_context_file,
    agent_max_output_tokens,
    agent_output_retries,
    agent_request_limit,
    agent_retries,
    assemble_system_prompt,
    build_model_settings,
    reasoning_effort,
)

_BASE_PROMPT = "# Base agent prompt"
_CONTEXT_TEXT = "PROJECT-CONTEXT-MARKER conventions go here."
_MAP_TEXT = "APPLICATION-MAP-MARKER routes go here."

_CONTEXT_LOGGER = "ai_test_gen.agents.runtime.context"

_REPO_ROOT = Path(__file__).resolve().parent.parent


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


def test_nested_comment_example_is_removed_whole(tmp_path):
    # Template headers quote a literal `<!-- … -->` inside an outer comment; a
    # non-greedy match would stop at the inner `-->` and leak the rest of the header.
    p = tmp_path / "f.md"
    p.write_text(
        "before\n<!--\nDelete every `<!-- … -->` comment.\nLEAKED-GUIDANCE\n-->\nafter"
    )
    out = _load_context_file(p)
    assert "LEAKED-GUIDANCE" not in out
    assert "<!--" not in out and "-->" not in out
    assert "before" in out and "after" in out


def test_ordinary_comments_removed_and_surrounding_content_kept(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("keep A <!-- drop 1 --> keep B\n<!-- drop\n2 -->\nkeep C")
    assert _load_context_file(p) == "keep A  keep B\n\nkeep C"


@pytest.mark.parametrize(
    "rel_path",
    [
        "project_context.example.md",
        "project_map.example.md",
        "packages/demo-notes-app/project_context.md",
        "packages/demo-notes-app/project_map.md",
    ],
)
def test_shipped_context_files_inject_no_comment_markers(rel_path):
    out = _load_context_file(_REPO_ROOT / rel_path)
    assert "<!--" not in out
    assert "-->" not in out


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


# --- build_model_settings -----------------------------------------------------


def test_build_model_settings_always_sequential(monkeypatch):
    # Browser tools mutate one shared page: parallel_tool_calls is ALWAYS off, no gating —
    # concurrent execution of a turn's batched actions is what mis-ordered UI steps.
    monkeypatch.delenv("PLANNER_REASONING_EFFORT", raising=False)
    settings = build_model_settings("PLANNER_REASONING_EFFORT")
    assert settings is not None
    assert settings.get("parallel_tool_calls") is False
    assert "openai_reasoning_effort" not in settings  # no effort env set


def test_build_model_settings_combines_effort_and_sequential(monkeypatch):
    # Both pieces coexist: reasoning effort AND parallel_tool_calls=False.
    monkeypatch.setenv("PLANNER_REASONING_EFFORT", "high")
    settings = build_model_settings("PLANNER_REASONING_EFFORT")
    assert settings.get("openai_reasoning_effort") == "high"
    assert settings.get("parallel_tool_calls") is False


def test_build_model_settings_includes_output_budget_when_set(monkeypatch):
    # A gateway's small default max_tokens can truncate a thinking model's turn into a
    # thinking-only response that retries to exhaustion; the knob overrides it per request.
    monkeypatch.delenv("PLANNER_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("AGENT_MAX_OUTPUT_TOKENS", "8000")
    settings = build_model_settings("PLANNER_REASONING_EFFORT")
    assert settings.get("max_tokens") == 8000
    monkeypatch.delenv("AGENT_MAX_OUTPUT_TOKENS")
    settings = build_model_settings("PLANNER_REASONING_EFFORT")
    assert "max_tokens" not in settings


def test_agent_max_output_tokens_parsing(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_OUTPUT_TOKENS", raising=False)
    assert agent_max_output_tokens() is None
    monkeypatch.setenv("AGENT_MAX_OUTPUT_TOKENS", "8000")
    assert agent_max_output_tokens() == 8000
    monkeypatch.setenv("AGENT_MAX_OUTPUT_TOKENS", "bogus")
    assert agent_max_output_tokens() is None
    monkeypatch.setenv("AGENT_MAX_OUTPUT_TOKENS", "0")
    assert agent_max_output_tokens() is None


def test_agent_output_retries_default_env_and_invalid(monkeypatch):
    # A separate budget from AGENT_MCP_RETRIES: output retries accumulate ACROSS a run, so
    # intermittent flaky-serving husk turns need more headroom than per-tool errors do.
    monkeypatch.delenv("AGENT_OUTPUT_RETRIES", raising=False)
    assert agent_output_retries() == 15
    monkeypatch.setenv("AGENT_OUTPUT_RETRIES", "25")
    assert agent_output_retries() == 25
    monkeypatch.setenv("AGENT_OUTPUT_RETRIES", "bogus")
    assert agent_output_retries() == 15


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_agent_output_retries_floors_at_one(monkeypatch, raw):
    # Zero output retries would kill a run on the first empty/malformed turn.
    monkeypatch.setenv("AGENT_OUTPUT_RETRIES", raw)
    assert agent_output_retries() == 1


@pytest.mark.parametrize("build", [build_planner, build_healer, build_generator])
def test_every_agent_builds_with_the_output_retry_budget(cfg, monkeypatch, build):
    # The knob only helps if each agent actually receives it: without an explicit output
    # budget pydantic-ai falls back to the tool budget, which is what killed long runs.
    # _max_output_retries is pydantic-ai's store for AgentRetries(output=...).
    monkeypatch.setenv("AGENT_OUTPUT_RETRIES", "7")
    assert build(cfg)._max_output_retries == 7
