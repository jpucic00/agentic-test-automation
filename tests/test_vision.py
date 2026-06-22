"""Unit tests for the optional Devstral vision sensor — fully local (no network).

Covers ``ask_vision`` (mocked via ``FunctionModel``), the planner's ``_latest_png`` helper, the
``inspect_screen`` tool's staleness guard / per-run budget / no-screenshot handling, and the
gating that keeps a disabled run (``PLANNER_VISION`` unset) identical to before. Coroutines are
driven with ``asyncio.run`` so this needs no ``pytest-asyncio``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time

import pytest
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from ai_test_gen import models
from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents import vision as vision_mod
from ai_test_gen.agents.planner import _latest_png, _register_inspect_screen, build_planner


def _vision_cfg(cfg, max_calls=2):
    """``cfg`` with the vision sensor enabled and its snapshots_dir created."""
    vcfg = dataclasses.replace(cfg, vision_max_calls=max_calls)
    vcfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    return vcfg


# --- ask_vision ---------------------------------------------------------------


def _vision_function_model(seen):
    """A FunctionModel that records whether the user prompt carried an image."""

    def fn(messages, info):
        for message in messages:
            for part in getattr(message, "parts", []):
                if isinstance(part, UserPromptPart):
                    content = part.content
                    items = content if isinstance(content, list) else [content]
                    seen["image"] = any(isinstance(c, BinaryContent) for c in items)
        return ModelResponse(parts=[TextPart("A modal dialog is covering the page.")])

    return FunctionModel(fn)


def test_ask_vision_sends_image_and_returns_text(cfg, monkeypatch):
    seen: dict[str, bool] = {}
    monkeypatch.setattr(
        vision_mod, "build_openai_model", lambda config, model: _vision_function_model(seen)
    )
    out = asyncio.run(vision_mod.ask_vision(cfg, "What is on screen?", b"\x89PNG\r\n\x1a\n"))
    assert out == "A modal dialog is covering the page."
    assert seen.get("image") is True  # the screenshot bytes reached the model


def test_ask_vision_truncates_long_answer(cfg, monkeypatch):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart("x" * 5000)])

    monkeypatch.setattr(vision_mod, "build_openai_model", lambda config, model: FunctionModel(fn))
    out = asyncio.run(vision_mod.ask_vision(cfg, "q", b"png-bytes"))
    assert len(out) <= 600


# --- _latest_png --------------------------------------------------------------


def test_latest_png_none_when_empty(cfg):
    cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
    assert _latest_png(cfg.snapshots_dir) is None


def test_latest_png_picks_newest_by_mtime(cfg):
    d = cfg.snapshots_dir
    d.mkdir(parents=True, exist_ok=True)
    old = d / "old.png"
    old.write_bytes(b"o")
    new = d / "new.png"
    new.write_bytes(b"n")
    os.utime(old, (1000, 1000))  # far in the past
    os.utime(new, (2_000_000_000, 2_000_000_000))  # far in the future
    assert _latest_png(d) == new


# --- inspect_screen tool: staleness / budget / no-screenshot ------------------


def _tool_with_fake_vision(cfg, monkeypatch, *, max_calls=2, answer="VISION_OK"):
    async def fake_ask(config, question, png):
        return answer

    monkeypatch.setattr(planner_mod, "ask_vision", fake_ask)
    agent = Agent(model=TestModel(), output_type=models.TestPlan)
    return _vision_cfg(cfg, max_calls), _register_inspect_screen(
        agent, _vision_cfg(cfg, max_calls)
    )


def test_inspect_screen_nudges_when_no_screenshot(cfg, monkeypatch, caplog):
    _, tool = _tool_with_fake_vision(cfg, monkeypatch)
    with caplog.at_level(logging.INFO, logger="ai_test_gen.agents.planner"):
        out = asyncio.run(tool("anything visible?"))
    assert "browser_take_screenshot" in out  # tell the model to capture first
    # The bounce logs at INFO (was DEBUG) so a call that never reaches vision is still visible.
    assert any("no screenshot" in r.getMessage().lower() for r in caplog.records)


def test_inspect_screen_fresh_png_calls_vision(cfg, monkeypatch):
    vcfg = _vision_cfg(cfg)
    (vcfg.snapshots_dir / "shot.png").write_bytes(b"img")  # fresh: just written

    async def fake_ask(config, question, png):
        return "VISION_OK"

    monkeypatch.setattr(planner_mod, "ask_vision", fake_ask)
    agent = Agent(model=TestModel(), output_type=models.TestPlan)
    tool = _register_inspect_screen(agent, vcfg)
    assert asyncio.run(tool("is a modal open?")) == "VISION_OK"


def test_inspect_screen_stale_png_warns_and_skips_vision(cfg, monkeypatch, caplog):
    vcfg = _vision_cfg(cfg)
    shot = vcfg.snapshots_dir / "shot.png"
    shot.write_bytes(b"img")
    os.utime(shot, (1000, 1000))  # very old -> stale

    async def fake_ask(config, question, png):
        raise AssertionError("ask_vision must not run on a stale screenshot")

    monkeypatch.setattr(planner_mod, "ask_vision", fake_ask)
    agent = Agent(model=TestModel(), output_type=models.TestPlan)
    tool = _register_inspect_screen(agent, vcfg)
    with caplog.at_level(logging.INFO, logger="ai_test_gen.agents.planner"):
        out = asyncio.run(tool("is a modal open?"))
    assert "stale" in out.lower()
    # The stale bounce logs at INFO (was DEBUG) so it can't hide from a default-level run.
    assert any("old" in r.getMessage().lower() for r in caplog.records)


def test_inspect_screen_enforces_per_run_budget(cfg, monkeypatch):
    vcfg = _vision_cfg(cfg, max_calls=1)
    (vcfg.snapshots_dir / "shot.png").write_bytes(b"img")

    async def fake_ask(config, question, png):
        return "VISION_OK"

    monkeypatch.setattr(planner_mod, "ask_vision", fake_ask)
    agent = Agent(model=TestModel(), output_type=models.TestPlan)
    tool = _register_inspect_screen(agent, vcfg)
    assert asyncio.run(tool("q1")) == "VISION_OK"  # within budget
    out = asyncio.run(tool("q2"))  # exceeds budget of 1
    assert "budget" in out.lower()


def test_inspect_screen_logs_each_trigger(cfg, monkeypatch, caplog):
    vcfg = _vision_cfg(cfg)
    (vcfg.snapshots_dir / "shot.png").write_bytes(b"img")

    async def fake_ask(config, question, png):
        return "a modal dialog is visible"

    monkeypatch.setattr(planner_mod, "ask_vision", fake_ask)
    agent = Agent(model=TestModel(), output_type=models.TestPlan)
    tool = _register_inspect_screen(agent, vcfg)
    with caplog.at_level(logging.INFO, logger="ai_test_gen.agents.planner"):
        asyncio.run(tool("is a modal open?"))
    messages = [r.getMessage() for r in caplog.records]
    assert any("vision check" in m and "is a modal open?" in m for m in messages)


def test_register_inspect_screen_logs_enabled(cfg, caplog):
    # Registration logs at INFO so a run can confirm the sensor is on (vs. the silent "is it even
    # enabled?" ambiguity that made "no vision in the logs" undiagnosable).
    vcfg = _vision_cfg(cfg)
    agent = Agent(model=TestModel(), output_type=models.TestPlan)
    with caplog.at_level(logging.INFO, logger="ai_test_gen.agents.planner"):
        _register_inspect_screen(agent, vcfg)
    assert any("vision sensor enabled" in r.getMessage().lower() for r in caplog.records)


def test_stale_after_s_reads_env(monkeypatch):
    f = planner_mod._stale_after_s
    monkeypatch.delenv("PLANNER_VISION_STALE_S", raising=False)
    assert f() == planner_mod._DEFAULT_STALE_AFTER_S  # default
    monkeypatch.setenv("PLANNER_VISION_STALE_S", "120")
    assert f() == 120.0
    monkeypatch.setenv("PLANNER_VISION_STALE_S", "not-a-number")  # invalid -> default
    assert f() == planner_mod._DEFAULT_STALE_AFTER_S
    monkeypatch.setenv("PLANNER_VISION_STALE_S", "0")  # non-positive -> default
    assert f() == planner_mod._DEFAULT_STALE_AFTER_S


def test_inspect_screen_staleness_window_configurable(cfg, monkeypatch):
    # A screenshot ~20s old is FRESH under the 45s default (vision runs) but STALE under a
    # tightened PLANNER_VISION_STALE_S=5 (bounces) — proves the knob and the generous default.
    vcfg = _vision_cfg(cfg)
    shot = vcfg.snapshots_dir / "shot.png"
    shot.write_bytes(b"img")
    twenty_s_ago = time.time() - 20
    os.utime(shot, (twenty_s_ago, twenty_s_ago))

    async def fake_ask(config, question, png):
        return "VISION_OK"

    monkeypatch.setattr(planner_mod, "ask_vision", fake_ask)
    agent = Agent(model=TestModel(), output_type=models.TestPlan)
    tool = _register_inspect_screen(agent, vcfg)

    monkeypatch.delenv("PLANNER_VISION_STALE_S", raising=False)  # default 45 -> fresh
    assert asyncio.run(tool("is a modal open?")) == "VISION_OK"

    monkeypatch.setenv("PLANNER_VISION_STALE_S", "5")  # 20s > 5s -> stale
    assert "stale" in asyncio.run(tool("is a modal open?")).lower()


# --- gating: off by default -> Planner unchanged ------------------------------


def test_planner_registers_inspect_screen_only_when_enabled(cfg, monkeypatch):
    seen: list[int] = []
    real = planner_mod._register_inspect_screen

    def spy(agent, config):
        seen.append(config.vision_max_calls)
        return real(agent, config)

    monkeypatch.setattr(planner_mod, "_register_inspect_screen", spy)
    build_planner(cfg)  # vision off (vision_max_calls == 0)
    assert seen == []
    build_planner(dataclasses.replace(cfg, vision_max_calls=2))  # on
    assert seen == [2]


def test_planner_prompt_has_vision_block_only_when_enabled(cfg, monkeypatch):
    captured: dict[str, str] = {}
    real = planner_mod.assemble_system_prompt

    def spy(config, base_prompt, *, include_map=True):
        captured["base"] = base_prompt
        return real(config, base_prompt, include_map=include_map)

    monkeypatch.setattr(planner_mod, "assemble_system_prompt", spy)
    build_planner(cfg)  # off
    assert "Seeing the page" not in captured["base"]
    build_planner(dataclasses.replace(cfg, vision_max_calls=2))  # on
    assert "Seeing the page" in captured["base"]


@pytest.mark.usefixtures("cfg")
def test_vision_prompt_fragment_forbids_selectors():
    # The gated fragment must keep the "vision never yields a selector" guardrail AND forbid the
    # Planner from ASKING vision for ids/selectors (vision is diagnostic only).
    fragment = (planner_mod.PROMPTS_DIR / "planner_vision.md").read_text()
    assert "browser_generate_locator" in fragment
    assert "NEVER" in fragment or "never" in fragment
    assert "data-testid" in fragment  # names the thing the Planner must not ask vision for
    assert "never ask it" in fragment.lower() or "must never ask" in fragment.lower()


def test_vision_system_prompt_redirects_selector_requests():
    # Backstop: asked for an id/selector, the vision model must redirect to browser_generate_locator
    # rather than invent one.
    system_prompt = vision_mod._SYSTEM_PROMPT
    assert "browser_generate_locator" in system_prompt
    assert "data-testid" in system_prompt


def test_inspect_screen_docstring_forbids_selector_questions(cfg, monkeypatch):
    # The tool's own docstring (what the Planner reads to decide how to call it) must forbid asking
    # for a selector and redirect to browser_generate_locator.
    _, tool = _tool_with_fake_vision(cfg, monkeypatch)
    doc = (tool.__doc__ or "").lower()
    assert "never ask it" in doc
    assert "browser_generate_locator" in doc
