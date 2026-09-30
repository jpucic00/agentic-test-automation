"""Unit tests for the locator-failure guard (agents/tools/locator_guard.py) — fully local.

Covers the ``process_tool_call`` hook: pass-through of non-target tools, the consecutive-failure
count, the vision-gated steer stage, the ALWAYS-on exhaustion soft-landing (a locator hunt can
never abort the run), reset-on-success, the configured and clamped steer threshold, the "never a
selector" guarantee of the messages, and the wiring that attaches the guard to the Planner
unconditionally. Coroutines run via ``asyncio.run`` (no pytest-asyncio); no network.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest
from pydantic_ai.exceptions import ModelRetry

from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents.tools.locator_guard import (
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
    assert guard.steer_after == 3

    # failures 1 and 2 re-raise the original MCP error unchanged
    for _ in range(2):
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
        assert ei.value.message == "ref e7 not found"

    # failure 3 (== threshold) raises the steer instead
    with pytest.raises(ModelRetry) as ei:
        asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    msg = ei.value.message
    assert "inspect_screen" in msg
    # inspect_screen self-captures — a screenshot-first instruction would waste a turn.
    assert "browser_take_screenshot" not in msg
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


def test_give_up_resets_streak_so_next_element_gets_normal_retries():
    # Element A exhausts the budget → give-up text (a clean return, which also resets
    # pydantic-ai's own retry counter). Element B's first failure must then get the ORIGINAL
    # error back (a normal retry), not "failed N times in a row — STOP".
    guard = LocatorFailureGuard(ceiling=5, vision_on=True)
    for _ in range(4):
        with pytest.raises(ModelRetry):
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {"target": "e7"}))
    out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {"target": "e7"}))
    assert isinstance(out, str) and "STOP calling it" in out

    for _ in range(2):  # element B: fresh streak, below the steer threshold
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {"target": "e9"}))
        assert ei.value.message == "ref e7 not found"
    with pytest.raises(ModelRetry) as ei:  # ...and it can still reach the steer stage again
        asyncio.run(guard(None, _fail, LOCATOR_TOOL, {"target": "e9"}))
    assert "inspect_screen" in ei.value.message


# --- vision budget spent: the guard stops sending the agent to inspect_screen ----


def test_disable_vision_drops_steer_and_vision_hint():
    guard = LocatorFailureGuard(ceiling=3, vision_on=True)
    guard.disable_vision()  # what inspect_screen's on_spent triggers
    for _ in range(2):  # would have steered at 2 (ceiling 3 → steer_after clamps to 2)
        with pytest.raises(ModelRetry) as ei:
            asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
        assert ei.value.message == "ref e7 not found"
    out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    assert "inspect_screen" not in out


def test_exhaust_message_mentions_probe_only_when_probe_on():
    with_probe = LocatorFailureGuard(ceiling=1, vision_on=False, probe_on=True)
    out = asyncio.run(with_probe(None, _fail, LOCATOR_TOOL, {}))
    assert "probe_dom" in out

    without_probe = LocatorFailureGuard(ceiling=1, vision_on=False, probe_on=False)
    out = asyncio.run(without_probe(None, _fail, LOCATOR_TOOL, {}))
    assert "probe_dom" not in out
    # The ladder/verify advice is always there — and verifies an authored selector the way the
    # MCP actually supports (raw selector as generate_locator's target; browser_verify_* can't).
    assert "browser_verify_element_visible" not in out
    assert "RAW" in out and "`target`" in out
    assert "count_matches" in out and "browser_hover" not in out


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
    assert "browser_take_screenshot" not in msg  # inspect_screen self-captures


def test_exhaust_message_never_contains_a_concrete_selector():
    guard = LocatorFailureGuard(ceiling=1, vision_on=True, probe_on=True)
    out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {}))
    for forbidden in ("getByTestId(", "getByRole(", "page.", "css=[", "xpath=//"):
        assert forbidden not in out


# --- threshold: configured + clamped to [1, ceiling-1] -------------------------


def test_steer_after_clamps_to_the_retry_ceiling():
    assert _steer_after(5, 3) == 3  # default 3 (config.locator_steer_after)
    assert _steer_after(5, 2) == 2
    assert _steer_after(5, 10) == 4  # clamp to ceiling-1
    assert _steer_after(1, 3) == 1  # tiny ceiling → upper clamps to 1


def test_guard_takes_the_configured_steer_threshold():
    assert LocatorFailureGuard(ceiling=5).steer_after == 3  # the shipped default
    assert LocatorFailureGuard(ceiling=5, steer_after=2).steer_after == 2


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

    # The configured budgets reach the guard: its ceiling and steer threshold come from Config.
    planner_mod.build_planner(dataclasses.replace(cfg, agent_mcp_retries=4, locator_steer_after=2))
    hook = captured["hook"]
    assert isinstance(hook, LocatorFailureGuard)
    assert (hook.exhaust_after, hook.steer_after) == (4, 2)
