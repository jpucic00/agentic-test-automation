"""Unit tests for the locator-failure guard (agents/_locator_steer.py) — fully local.

Covers the ``process_tool_call`` hook: pass-through of non-target tools, the consecutive-failure
count, the vision-gated steer stage, the ALWAYS-on exhaustion soft-landing (a locator hunt can
never abort the run), reset-on-success, the env-driven and clamped steer threshold, the "never a
selector" guarantee of the messages, and the wiring that attaches the guard to the Planner
unconditionally. Coroutines run via ``asyncio.run`` (no pytest-asyncio); no network.
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
    LocatorFailureGuard,
    _steer_after,
)


async def _fail(name, tool_args, *, metadata=None):
    raise ModelRetry("ref e7 not found")


async def _ok(name, tool_args, *, metadata=None):
    return "LOCATOR_OK"


# --- pass-through: non-target tools are never counted or intervened on ---------


def test_non_target_tool_passes_through():
    guard = LocatorFailureGuard(ceiling=5, vision_on=True)
    assert asyncio.run(guard(None, _ok, "browser_click", {})) == "LOCATOR_OK"


def test_non_target_failures_never_intervene_and_never_count():
    # Many failures of a DIFFERENT tool must surface unchanged and never touch the counter.
    guard = LocatorFailureGuard(ceiling=5, vision_on=True)
    for _ in range(6):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, "browser_click", {}))
        assert ei.value.message == "ref e7 not found"  # original error, never replaced


# --- steer stage (vision on): Nth consecutive locator failure swaps in the steer


def test_steers_on_third_consecutive_locator_failure():
    guard = LocatorFailureGuard(ceiling=5, vision_on=True)
    assert guard.steer_after == _DEFAULT_STEER_AFTER == 3

    # failures 1 and 2 re-raise the original MCP error unchanged
    for _ in range(2):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
        assert ei.value.message == "ref e7 not found"

    # failure 3 (== threshold) raises the steer instead
    with pytest.raises(ModelRetry) as ei:
        asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    msg = ei.value.message
    assert "browser_take_screenshot" in msg
    assert "inspect_screen" in msg
    assert msg != "ref e7 not found"


def test_steer_fires_between_threshold_and_ceiling_then_soft_lands():
    # Failures 3 and 4 steer (raise); failure 5 (== ceiling) RETURNS the give-up guidance —
    # the run must never see the fatal Nth retry.
    guard = LocatorFailureGuard(ceiling=5, vision_on=True)
    for _ in range(2):  # climb to just below the steer threshold
        with pytest.raises(ModelRetry):
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    for _ in range(2):  # 3rd and 4th steer
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
        assert "inspect_screen" in ei.value.message
    out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))  # 5th: soft-land, no raise
    assert isinstance(out, str)
    assert "STOP calling it" in out


def test_no_steer_when_vision_off_but_exhaustion_still_soft_lands():
    # Vision off: below the ceiling the ORIGINAL error passes through (no steer message);
    # at the ceiling the guard still returns give-up guidance instead of raising.
    guard = LocatorFailureGuard(ceiling=3, vision_on=False)
    for _ in range(2):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
        assert ei.value.message == "ref e7 not found"
    out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    assert isinstance(out, str)
    assert "MOVE ON" in out
    assert "inspect_screen" not in out  # vision off -> no vision advice


def test_exhaustion_keeps_soft_landing_on_further_failures():
    # Once exhausted, every further failure returns guidance too — the streak only resets on a
    # real success, so the fatal raise can never sneak back in.
    guard = LocatorFailureGuard(ceiling=2, vision_on=False)
    with pytest.raises(ModelRetry):
        asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    for _ in range(3):
        out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
        assert isinstance(out, str) and "STOP calling it" in out


def test_exhaust_message_mentions_probe_only_when_probe_on():
    with_probe = LocatorFailureGuard(ceiling=1, vision_on=False, probe_on=True)
    out = asyncio.run(with_probe(None, _fail, LOCATOR_TOOL, {}))
    assert "probe_dom" in out

    without_probe = LocatorFailureGuard(ceiling=1, vision_on=False, probe_on=False)
    out = asyncio.run(without_probe(None, _fail, LOCATOR_TOOL, {}))
    assert "probe_dom" not in out
    assert "browser_verify_element_visible" in out  # the ladder/verify advice is always there


# --- reset: one clean locator clears the streak --------------------------------


def test_success_resets_consecutive_counter():
    guard = LocatorFailureGuard(ceiling=5, vision_on=True)
    for _ in range(2):
        with pytest.raises(ModelRetry):
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    assert asyncio.run(guard(None, _ok, LOCATOR_TOOL, {})) == "LOCATOR_OK"  # success resets

    # streak cleared → two more failures stay below threshold and must NOT steer
    for _ in range(2):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
        assert ei.value.message == "ref e7 not found"


# --- the guard's messages never carry a selector --------------------------------


def test_steer_message_never_contains_a_selector():
    msg = _STEER_MESSAGE.format(n=3)
    for forbidden in ("getByTestId", "getByRole", "getByLabel", "page.", "css=", "xpath="):
        assert forbidden not in msg
    assert "browser_generate_locator" in msg  # the locator still comes from the tool
    assert "NEVER returns a selector" in msg


def test_exhaust_message_never_contains_a_concrete_selector():
    guard = LocatorFailureGuard(ceiling=1, vision_on=True, probe_on=True)
    out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    for forbidden in ("getByTestId(", "getByRole(", "page.", "css=[", "xpath=//"):
        assert forbidden not in out


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


# --- wiring: the guard is attached ALWAYS, vision on or off --------------------


def test_planner_attaches_guard_always(cfg, monkeypatch):
    captured: dict[str, object] = {}
    real = planner_mod.build_playwright_mcp

    def spy(config, storage_state=None, *, process_tool_call=None):
        captured["hook"] = process_tool_call
        return real(config, storage_state=storage_state, process_tool_call=process_tool_call)

    monkeypatch.setattr(planner_mod, "build_playwright_mcp", spy)

    planner_mod.build_planner(cfg)  # vision off (vision_max_calls == 0)
    assert isinstance(captured["hook"], LocatorFailureGuard)

    planner_mod.build_planner(dataclasses.replace(cfg, vision_max_calls=2))  # vision on
    assert isinstance(captured["hook"], LocatorFailureGuard)
