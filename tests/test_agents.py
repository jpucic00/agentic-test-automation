"""Unit tests for the three agents — fully local (no network, no npx subprocess).

Each agent is built with the hermetic ``cfg`` fixture and exercised through a
``TestModel`` via the ``agent.override(model=..., toolsets=[])`` seam, so no real LLM
gateway is contacted and the Playwright MCP subprocess is never started. Coroutines
are driven with ``asyncio.run`` so this needs no ``pytest-asyncio`` (no new dependency).

Models are referenced via the ``models`` module because ``TestPlan`` / ``TestRunResult``
start with "Test" and would otherwise be collected by pytest as test classes.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from ai_test_gen import models
from ai_test_gen.agents import generator as generator_mod
from ai_test_gen.agents import healer as healer_mod
from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents.generator import build_generator
from ai_test_gen.agents.healer import build_healer
from ai_test_gen.agents.planner import build_planner


def _run_offline(agent):
    """Run an agent with a TestModel and no toolsets — no network, no subprocess."""
    with agent.override(model=TestModel(), toolsets=[]):
        return asyncio.run(agent.run("sample")).output


def test_generator_builds_and_returns_generated_test(cfg):
    out = _run_offline(build_generator(cfg))
    assert isinstance(out, models.GeneratedTest)


def test_planner_builds_and_returns_test_plan(cfg):
    out = _run_offline(build_planner(cfg))
    assert isinstance(out, models.TestPlan)


def test_healer_builds_and_returns_healed_test(cfg):
    out = _run_offline(build_healer(cfg))
    assert isinstance(out, models.HealedTest)


def test_generator_has_no_playwright_mcp():
    # The Generator deliberately does not use Playwright MCP (smaller scope = better code).
    assert not hasattr(generator_mod, "build_playwright_mcp")


def test_planner_attaches_playwright_mcp(cfg, monkeypatch):
    calls: list[object] = []
    real = planner_mod.build_playwright_mcp

    def spy(config, storage_state=None):
        calls.append(storage_state)
        return real(config, storage_state=storage_state)

    monkeypatch.setattr(planner_mod, "build_playwright_mcp", spy)
    build_planner(cfg)
    assert len(calls) == 1


def test_healer_attaches_playwright_mcp(cfg, monkeypatch):
    calls: list[object] = []
    real = healer_mod.build_playwright_mcp

    def spy(config, storage_state=None):
        calls.append(storage_state)
        return real(config, storage_state=storage_state)

    monkeypatch.setattr(healer_mod, "build_playwright_mcp", spy)
    build_healer(cfg)
    assert len(calls) == 1


def test_prompts_carry_generate_locator_contract():
    # Locks the selector-contract migration offline: every browser-driving prompt instructs the
    # verified-locator workflow, and the Planner prompt no longer carries the retired #id-first
    # GOOD/BAD guidance the migration replaced (otherwise a regression would pass CI unnoticed).
    prompts = planner_mod.PROMPTS_DIR
    for name in ("planner.md", "generator.md", "healer.md"):
        assert "browser_generate_locator" in (prompts / name).read_text(), name
    planner_md = (prompts / "planner.md").read_text()
    assert "getByTestId" in planner_md
    assert "#login-submit" not in planner_md  # retired GOOD-example marker


def test_heal_message_includes_intent_plan_and_notes():
    # Path A: the heal message must surface the original intent, the plan's verified selectors,
    # and the Planner's notes — not just the failing code + error.
    case = models.ManualTestCase(
        key="QA-7",
        title="Create org",
        steps=["click Add org", "fill name"],
        expected_results=["dialog opens", "org created"],
    )
    plan = models.TestPlan(
        test_case_key="QA-7",
        title="Create org",
        target_url="https://staging.example.internal",
        steps=[
            models.PlanStep(
                action="click Add org",
                target_selector="getByTestId('add-org')",
                expected="dialog opens",
            )
        ],
        notes="The Add-org dialog animates in; the submit button is briefly detached.",
    )
    test = models.GeneratedTest(file_name="QA-7.spec.ts", code="// spec", description="x")
    failure = models.TestRunResult(
        status="failed", stdout="", stderr="boom", error_message="locator timeout"
    )
    msg = healer_mod._build_heal_message(test, failure, plan, case)
    assert "QA-7" in msg
    assert "click Add org" in msg  # intent step
    assert "org created" in msg  # expected result, paired with its step
    assert "getByTestId('add-org')" in msg  # verified selector carried from the plan
    assert "animates in" in msg  # Planner note surfaced
    assert "locator timeout" in msg  # failure still present


def test_planner_builds_with_valid_reasoning_effort(cfg, monkeypatch):
    monkeypatch.setenv("PLANNER_REASONING_EFFORT", "high")
    out = _run_offline(build_planner(cfg))  # knob must not break the agent
    assert isinstance(out, models.TestPlan)


def test_planner_invalid_reasoning_effort_fails_at_build(cfg, monkeypatch):
    # Fail fast on a typo — a silently-vanishing effort value would masquerade as
    # a tuned pipeline (the whole reason the knob ships with a validation story).
    monkeypatch.setenv("PLANNER_REASONING_EFFORT", "ultra")
    with pytest.raises(ValueError, match="PLANNER_REASONING_EFFORT"):
        build_planner(cfg)


def test_generation_message_plain_has_no_retry_section():
    plan = models.TestPlan(
        test_case_key="QA-9", title="t", target_url="https://staging.example.internal", steps=[]
    )
    msg = generator_mod._build_generation_message(plan)
    assert "QA-9" in msg
    assert "Previous attempt failed to run" not in msg


def test_generation_message_retry_includes_previous_code_and_error():
    # Compile-retry path: the Generator gets its own broken output + the error text,
    # and is told to keep the plan's steps/selectors unchanged.
    plan = models.TestPlan(
        test_case_key="QA-9", title="t", target_url="https://staging.example.internal", steps=[]
    )
    msg = generator_mod._build_generation_message(
        plan, previous_code="const broken =", error_text="SyntaxError: unexpected end"
    )
    assert "Previous attempt failed to run" in msg
    assert "const broken =" in msg
    assert "SyntaxError: unexpected end" in msg


def _heal_message_fixtures():
    case = models.ManualTestCase(key="QA-7", title="Create org", steps=["click Add org"])
    plan = models.TestPlan(
        test_case_key="QA-7",
        title="Create org",
        target_url="https://staging.example.internal",
        steps=[models.PlanStep(action="click Add org")],
    )
    test = models.GeneratedTest(file_name="QA-7.spec.ts", code="// spec", description="x")
    failure = models.TestRunResult(
        status="failed", stdout="", stderr="boom", error_message="locator timeout"
    )
    return test, failure, plan, case


def test_heal_message_first_attempt_has_no_history_section():
    msg = healer_mod._build_heal_message(*_heal_message_fixtures())
    assert "Previous heal attempts" not in msg


def test_heal_message_second_attempt_lists_prior_changes():
    # The Healer rewrites the whole file: without the history, attempt 2 can silently
    # undo attempt 1's fix. The message must carry the prior summaries + a numbered list
    # and the don't-undo instruction.
    msg = healer_mod._build_heal_message(
        *_heal_message_fixtures(),
        heal_history=["added exact:true to the Add button locator"],
    )
    assert "Previous heal attempts" in msg
    assert "1. added exact:true to the Add button locator" in msg
    assert "Do NOT undo a previous attempt's change" in msg


def test_healer_prompt_allows_intent_reconciliation():
    # The Healer may now restructure to reconcile with intent (add a skipped step / drop a
    # hallucinated one); the old blanket "DO NOT restructure" must be gone, while live-verified
    # selectors are still required.
    healer_md = (healer_mod.PROMPTS_DIR / "healer.md").read_text()
    assert "DO NOT restructure the test." not in healer_md
    assert "browser_generate_locator" in healer_md
