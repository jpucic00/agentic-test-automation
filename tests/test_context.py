"""Unit tests for ai_test_gen.agents.runtime.context — fully local (no network).

Uses the shared ``cfg`` fixture (tests/conftest.py); context/map files are written
into the fixture's tmp_path-backed paths per test.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from ai_test_gen.agents.generator import build_generator
from ai_test_gen.agents.healer import build_healer
from ai_test_gen.agents.planner import build_planner
from ai_test_gen.agents.runtime.context import (
    _load_context_file,
    assemble_system_prompt,
    build_model_settings,
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


# --- build_model_settings -----------------------------------------------------


def test_build_model_settings_always_sequential(cfg):
    # Browser tools mutate one shared page: parallel_tool_calls is ALWAYS off, no gating —
    # concurrent execution of a turn's batched actions is what mis-ordered UI steps.
    settings = build_model_settings(cfg, None)
    assert settings is not None
    assert settings.get("parallel_tool_calls") is False
    assert "openai_reasoning_effort" not in settings  # no effort configured


def test_build_model_settings_combines_effort_and_sequential(cfg):
    # Both pieces coexist: reasoning effort AND parallel_tool_calls=False.
    settings = build_model_settings(cfg, "high")
    assert settings.get("openai_reasoning_effort") == "high"
    assert settings.get("parallel_tool_calls") is False


def test_build_model_settings_includes_output_budget_when_set(cfg):
    # A gateway's small default max_tokens can truncate a thinking model's turn into a
    # thinking-only response that retries to exhaustion; the knob overrides it per request.
    settings = build_model_settings(dataclasses.replace(cfg, agent_max_output_tokens=8000), None)
    assert settings.get("max_tokens") == 8000
    assert "max_tokens" not in build_model_settings(cfg, None)


@pytest.mark.parametrize("build", [build_planner, build_healer, build_generator])
def test_every_agent_builds_with_the_output_retry_budget(cfg, build):
    # The knob only helps if each agent actually receives it: without an explicit output
    # budget pydantic-ai falls back to the tool budget, which is what killed long runs.
    # _max_output_retries is pydantic-ai's store for AgentRetries(output=...).
    assert build(dataclasses.replace(cfg, agent_output_retries=7))._max_output_retries == 7
