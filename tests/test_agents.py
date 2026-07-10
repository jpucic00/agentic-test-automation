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
import dataclasses

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

    def spy(config, storage_state=None, *, process_tool_call=None):
        calls.append(storage_state)
        return real(config, storage_state=storage_state, process_tool_call=process_tool_call)

    monkeypatch.setattr(planner_mod, "build_playwright_mcp", spy)
    build_planner(cfg)
    assert len(calls) == 1


def test_healer_attaches_playwright_mcp(cfg, monkeypatch):
    calls: list[object] = []
    real = healer_mod.build_playwright_mcp

    def spy(config, storage_state=None, *, process_tool_call=None):
        calls.append(storage_state)
        return real(config, storage_state=storage_state, process_tool_call=process_tool_call)

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


def test_prompts_carry_resilience_ladder():
    # The locator strategy is the element-driven resilience ladder (id > accessible > CSS > XPath),
    # not id-first: planner.md and healer.md must teach descending to a verified XPath for
    # inaccessible elements, and the Generator must accept css/xpath plan selectors.
    prompts = planner_mod.PROMPTS_DIR
    planner_md = (prompts / "planner.md").read_text()
    healer_md = (prompts / "healer.md").read_text()
    generator_md = (prompts / "generator.md").read_text()
    for md, name in ((planner_md, "planner.md"), (healer_md, "healer.md")):
        assert "resilience ladder" in md.lower(), name
        assert "xpath" in md.lower(), name
    # The Generator carries css/xpath plan selectors verbatim (it has no MCP to re-capture).
    assert "xpath" in generator_md.lower()
    assert "locator('css=" in generator_md


def test_planner_prompt_has_navigation_discipline():
    # Pins the real-app navigation fixes: navigate like a user (not guessed URLs), never record a
    # URL the live app rejected, and don't emit a plan for a page that wasn't visited.
    planner_md = (planner_mod.PROMPTS_DIR / "planner.md").read_text()
    assert "navigate like a USER" in planner_md
    assert "Never record a URL the live app rejected" in planner_md
    assert "Don't plan a page you didn't visit" in planner_md


def test_planner_prompt_has_declared_followup_flows():
    # Activation-flow contract: a declared follow-up flow (canonically: email verification before
    # a new account's first login) yields REAL plan steps right after the creation step, and the
    # navigation rule admits map-declared auxiliary tool UIs (mail-catcher) — without this the
    # canonical failure is create-user → login attempts against a never-activated account.
    planner_md = (planner_mod.PROMPTS_DIR / "planner.md").read_text()
    assert "Declared follow-up (activation) flows" in planner_md
    assert "REAL PLAN STEPS" in planner_md
    assert "mail-catcher" in planner_md
    assert "NEVER log in with (or" in planner_md  # never use a record before activation


def test_healer_prompt_has_activation_gap_diagnosis():
    # A fresh account failing its first login is an activation-gap suspect BEFORE it is a
    # selector suspect; the fix is ADDING the missing activation steps, live-verified.
    healer_md = (healer_mod.PROMPTS_DIR / "healer.md").read_text()
    assert "A freshly-created account can't log in" in healer_md
    assert "activation flow" in healer_md
    assert "ADD the missing activation steps" in healer_md


def test_healer_prompt_has_locator_kind_escalation():
    # On a persistently-failing step the Healer must escalate the locator KIND down the ladder
    # (the behavior the user asked for: roll a stuck id over to a verified XPath).
    healer_md = (healer_mod.PROMPTS_DIR / "healer.md").read_text()
    assert "Locator-kind escalation" in healer_md
    assert "escalat" in healer_md.lower()


def test_planner_prompt_drives_and_keeps_spec():
    # The Planner must DRIVE the scenario (not just verify selectors), emit recovery steps for
    # side-effects, and keep the manual case's expectation even when the live app diverges.
    planner_md = (planner_mod.PROMPTS_DIR / "planner.md").read_text()
    assert "needn't submit" not in planner_md  # retired: it no longer skips submitting
    assert "PERFORM each step" in planner_md
    assert "Recovery steps are real steps" in planner_md
    assert "Keep the spec's expectation" in planner_md


def test_healer_prompt_is_full_browser_agent():
    # The Healer is reframed as a full browser agent that reproduces failures live, MAY trigger
    # session-invalidating actions, and adds recovery steps — the old blanket prohibition is gone.
    healer_md = (healer_mod.PROMPTS_DIR / "healer.md").read_text()
    assert "full browser agent" in healer_md
    assert "DO NOT trigger these" not in healer_md  # retired prohibition
    assert "Session-invalidating actions" in healer_md and "ALLOWED" in healer_md
    assert "Recovery steps" in healer_md


def test_heal_message_escalation_block_present_when_recurring():
    msg = healer_mod._build_heal_message(*_heal_message_fixtures(), locator_escalation=2)
    assert "PERSISTED across 2" in msg
    assert "ladder" in msg.lower()
    assert "xpath" in msg.lower()


def test_heal_message_no_escalation_block_on_first_failure():
    msg = healer_mod._build_heal_message(*_heal_message_fixtures(), locator_escalation=0)
    assert "PERSISTED" not in msg


def test_prompts_carry_page_context_contract():
    # The distilled-page-context contract: the Planner extracts page_url + container
    # per step (observed, never invented); the Generator scopes locators when a step
    # carries a container.
    planner_md = (planner_mod.PROMPTS_DIR / "planner.md").read_text()
    assert "page_url" in planner_md
    assert "container" in planner_md
    assert "never invented" in planner_md
    generator_md = (planner_mod.PROMPTS_DIR / "generator.md").read_text()
    assert "container" in generator_md
    assert "page.getByRole('dialog')" in generator_md


def test_prompts_carry_verified_assertion_contract():
    # Verified-assertion contract: the Planner captures a proof locator (assert_selector)
    # or a URL for assert steps; the Generator asserts those and NEVER invents visible text.
    planner_md = (planner_mod.PROMPTS_DIR / "planner.md").read_text()
    assert "assert_selector" in planner_md
    generator_md = (planner_mod.PROMPTS_DIR / "generator.md").read_text()
    assert "assert_selector" in generator_md
    assert "waitForURL" in generator_md
    # The field exists on the data contract with a default so plans without it still validate.
    step = models.PlanStep(action="verify dashboard")
    assert step.assert_selector is None


def test_heal_message_includes_intent_plan_and_notes():
    # Path A: the heal message must surface the original intent, the plan's verified selectors,
    # and the Planner's notes — not just the failing code + error.
    case = models.ManualTestCase(
        key="QA-7",
        title="Create org",
        steps=[
            models.ManualStep(action="click Add org", expected="dialog opens"),
            models.ManualStep(action="fill name", expected="org created"),
        ],
    )
    plan = models.TestPlan(
        test_case_key="QA-7",
        title="Create org",
        target_url="https://staging.example.internal",
        steps=[
            models.PlanStep(
                action="click Add org",
                target_selector="getByTestId('add-org')",
                assert_selector="getByRole('dialog')",
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
    assert "getByRole('dialog')" in msg  # verified assertion target carried from the plan
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


@pytest.mark.parametrize("build", [build_planner, build_healer])
@pytest.mark.parametrize("vision_calls", [0, 2])
def test_agent_always_disables_parallel_tool_calls(cfg, monkeypatch, build, vision_calls):
    # Browser agents are ALWAYS sequential — vision on or off. pydantic-ai executes a turn's
    # tool calls concurrently, so batched browser actions could click/navigate out of order
    # (and race a vision screenshot); one tool call per turn is the only correct order.
    monkeypatch.delenv("PLANNER_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("HEALER_REASONING_EFFORT", raising=False)
    agent = build(dataclasses.replace(cfg, vision_max_calls=vision_calls))
    assert agent.model_settings is not None
    assert agent.model_settings.get("parallel_tool_calls") is False


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
    case = models.ManualTestCase(
        key="QA-7", title="Create org", steps=[models.ManualStep(action="click Add org")]
    )
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


def _plan_with_page_context():
    return models.TestPlan(
        test_case_key="QA-8",
        title="Invite member",
        target_url="https://staging.example.internal",
        steps=[
            models.PlanStep(
                action="click Add in the invite dialog",
                target_selector="getByRole('button', { name: 'Add', exact: true })",
                page_url="https://staging.example.internal/users",
                container="dialog 'Create user'",
            )
        ],
    )


def test_generation_message_carries_step_page_context():
    # The plan JSON is the Generator's whole world view — the distilled context must
    # be present there for the scoping rule in generator.md to act on.
    msg = generator_mod._build_generation_message(_plan_with_page_context())
    assert "dialog 'Create user'" in msg
    assert "https://staging.example.internal/users" in msg


def test_heal_message_shows_plan_time_page_context():
    case = models.ManualTestCase(
        key="QA-8", title="Invite member", steps=[models.ManualStep(action="click Add")]
    )
    test = models.GeneratedTest(file_name="QA-8.spec.ts", code="// spec", description="x")
    failure = models.TestRunResult(
        status="failed", stdout="", stderr="boom",
        error_message="strict mode violation: resolved 2 elements",
    )
    msg = healer_mod._build_heal_message(test, failure, _plan_with_page_context(), case)
    assert "container (observed at plan time): dialog 'Create user'" in msg
    assert "page: https://staging.example.internal/users" in msg


def test_plan_step_page_context_is_optional():
    # Old plan JSON (pre-page-context) must still validate; defaults are None.
    plan = models.TestPlan.model_validate(
        {
            "test_case_key": "QA-1",
            "title": "Login",
            "target_url": "https://staging.example.internal",
            "steps": [{"action": "log in"}],
        }
    )
    assert plan.steps[0].page_url is None
    assert plan.steps[0].container is None


def test_heal_message_quotes_dying_line_and_execution_boundary():
    # The misdiagnosis fix: the Healer must know WHERE the run died and that code
    # after that line never executed — otherwise a downstream timeout reads as a
    # downstream bug and it "fixes" tail steps while the real blocker stays broken.
    case = models.ManualTestCase(
        key="QA-9", title="Login", steps=[models.ManualStep(action="log in")]
    )
    plan = models.TestPlan(
        test_case_key="QA-9", title="Login",
        target_url="https://staging.example.internal", steps=[],
    )
    test = models.GeneratedTest(
        file_name="QA-9.spec.ts",
        code="line one\nawait page.getByTestId('logon-btn').click();\nline three",
        description="x",
    )
    failure = models.TestRunResult(
        status="failed", stdout="", stderr="", error_message="timeout", error_line=2
    )
    msg = healer_mod._build_heal_message(test, failure, plan, case)
    assert "The run DIED at line 2" in msg
    assert "getByTestId('logon-btn')" in msg
    assert "NEVER EXECUTED" in msg


def test_heal_message_has_no_boundary_without_error_line():
    case = models.ManualTestCase(
        key="QA-9", title="Login", steps=[models.ManualStep(action="log in")]
    )
    plan = models.TestPlan(
        test_case_key="QA-9", title="Login",
        target_url="https://staging.example.internal", steps=[],
    )
    test = models.GeneratedTest(file_name="QA-9.spec.ts", code="// spec", description="x")
    failure = models.TestRunResult(
        status="failed", stdout="", stderr="", error_message="timeout"
    )
    msg = healer_mod._build_heal_message(test, failure, plan, case)
    assert "The run DIED" not in msg


def test_healer_prompt_has_diagnosis_order():
    healer_md = (healer_mod.PROMPTS_DIR / "healer.md").read_text()
    assert "Diagnosis order" in healer_md
    assert "IN ORDER" in healer_md  # replay locators from the top, login first
    assert "login first" in healer_md


def test_healer_prompt_allows_intent_reconciliation():
    # The Healer may now restructure to reconcile with intent (add a skipped step / drop a
    # hallucinated one); the old blanket "DO NOT restructure" must be gone, while live-verified
    # selectors are still required.
    healer_md = (healer_mod.PROMPTS_DIR / "healer.md").read_text()
    assert "DO NOT restructure the test." not in healer_md
    assert "browser_generate_locator" in healer_md
