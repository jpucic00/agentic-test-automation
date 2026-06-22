"""Unit tests for the Planner's locator→vision steer (agents/_locator_steer.py) — fully local.

Covers the ``process_tool_call`` hook: pass-through of non-target tools, the consecutive-failure
count + threshold steer, reset-on-success, the env-driven and clamped threshold, the "never a
selector" guarantee of the steer message, and the gating that attaches the hook only when
``PLANNER_VISION`` is on. Coroutines run via ``asyncio.run`` (no pytest-asyncio); no network.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from pydantic_ai.exceptions import ModelRetry

from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents._locator_steer import (
    _DEFAULT_STEER_AFTER,
    _STEER_MESSAGE,
    LOCATOR_TOOL,
    LocatorVisionSteer,
    _steer_after,
)


async def _fail(name, tool_args, *, metadata=None):
    raise ModelRetry("ref e7 not found")


async def _ok(name, tool_args, *, metadata=None):
    return "LOCATOR_OK"


# --- pass-through: non-target tools are never counted or steered ---------------


def test_non_target_tool_passes_through():
    steer = LocatorVisionSteer(ceiling=5)
    assert asyncio.run(steer(None, _ok, "browser_click", {})) == "LOCATOR_OK"


def test_non_target_failures_never_steer_and_never_count():
    # Many failures of a DIFFERENT tool must surface unchanged and never touch the counter.
    steer = LocatorVisionSteer(ceiling=5)
    for _ in range(5):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(steer(None, _fail, "browser_click", {}))
        assert ei.value.message == "ref e7 not found"  # original error, never the steer


# --- threshold: Nth consecutive locator failure swaps in the steer -------------


def test_steers_on_third_consecutive_locator_failure():
    steer = LocatorVisionSteer(ceiling=5)
    assert steer.steer_after == _DEFAULT_STEER_AFTER == 3

    # failures 1 and 2 re-raise the original MCP error unchanged
    for _ in range(2):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(steer(None, _fail, LOCATOR_TOOL, {}))
        assert ei.value.message == "ref e7 not found"

    # failure 3 (== threshold) raises the steer instead
    with pytest.raises(ModelRetry) as ei:
        asyncio.run(steer(None, _fail, LOCATOR_TOOL, {}))
    msg = ei.value.message
    assert "browser_take_screenshot" in msg
    assert "inspect_screen" in msg
    assert msg != "ref e7 not found"


def test_steer_keeps_firing_after_threshold():
    # "Repeat until it has an idea" — every failure at/after the threshold re-emits the steer.
    steer = LocatorVisionSteer(ceiling=5)
    for _ in range(2):  # climb to just below threshold
        with pytest.raises(ModelRetry):
            asyncio.run(steer(None, _fail, LOCATOR_TOOL, {}))
    for _ in range(3):  # 3rd, 4th, 5th all steer
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(steer(None, _fail, LOCATOR_TOOL, {}))
        assert "inspect_screen" in ei.value.message


# --- reset: one clean locator clears the streak --------------------------------


def test_success_resets_consecutive_counter():
    steer = LocatorVisionSteer(ceiling=5)
    for _ in range(2):
        with pytest.raises(ModelRetry):
            asyncio.run(steer(None, _fail, LOCATOR_TOOL, {}))
    assert asyncio.run(steer(None, _ok, LOCATOR_TOOL, {})) == "LOCATOR_OK"  # success resets

    # streak cleared → two more failures stay below threshold and must NOT steer
    for _ in range(2):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(steer(None, _fail, LOCATOR_TOOL, {}))
        assert ei.value.message == "ref e7 not found"


# --- the steer message never carries a selector --------------------------------


def test_steer_message_never_contains_a_selector():
    msg = _STEER_MESSAGE.format(n=3)
    for forbidden in ("getByTestId", "getByRole", "getByLabel", "page.", "css=", "xpath="):
        assert forbidden not in msg
    assert "browser_generate_locator" in msg  # the locator still comes from the tool
    assert "NEVER returns a selector" in msg


# --- threshold: env-driven + clamped to [1, ceiling-1] -------------------------


def test_steer_after_reads_env_and_clamps(monkeypatch):
    monkeypatch.delenv("PLANNER_LOCATOR_STEER_AFTER", raising=False)
    assert _steer_after(5) == _DEFAULT_STEER_AFTER  # default 3

    monkeypatch.setenv("PLANNER_LOCATOR_STEER_AFTER", "2")
    assert _steer_after(5) == 2

    monkeypatch.setenv("PLANNER_LOCATOR_STEER_AFTER", "10")  # clamp to ceiling-1
    assert _steer_after(5) == 4

    monkeypatch.setenv("PLANNER_LOCATOR_STEER_AFTER", "0")  # floor at 1
    assert _steer_after(5) == 1
    monkeypatch.setenv("PLANNER_LOCATOR_STEER_AFTER", "-3")
    assert _steer_after(5) == 1

    monkeypatch.setenv("PLANNER_LOCATOR_STEER_AFTER", "abc")  # invalid → default
    assert _steer_after(5) == _DEFAULT_STEER_AFTER

    monkeypatch.setenv("PLANNER_LOCATOR_STEER_AFTER", "3")  # tiny ceiling → upper clamps to 1
    assert _steer_after(1) == 1


# --- gating: hook attached only when vision is enabled -------------------------


def test_planner_attaches_steer_only_when_vision_enabled(cfg, monkeypatch):
    captured: dict[str, object] = {}
    real = planner_mod.build_playwright_mcp

    def spy(config, storage_state=None, *, process_tool_call=None):
        captured["hook"] = process_tool_call
        return real(config, storage_state=storage_state, process_tool_call=process_tool_call)

    monkeypatch.setattr(planner_mod, "build_playwright_mcp", spy)

    planner_mod.build_planner(cfg)  # vision off (vision_max_calls == 0)
    assert captured["hook"] is None

    planner_mod.build_planner(dataclasses.replace(cfg, vision_max_calls=2))  # vision on
    assert isinstance(captured["hook"], LocatorVisionSteer)
