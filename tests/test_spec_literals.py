"""Unit tests for the Generator's spec-literal validator (agents/runtime/spec_literals.py)."""
from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_test_gen.agents.generator import build_generator
from ai_test_gen.agents.runtime.spec_literals import (
    check_spec_literals,
    locator_literals,
    spec_literal_validator,
)
from ai_test_gen.core import models


def _plan(*steps: models.PlanStep) -> models.TestPlan:
    return models.TestPlan(
        test_case_key="NOTE-3", title="t", target_url="/", steps=list(steps)
    )


def _values(code: str) -> list[str]:
    return [lit.value for lit in locator_literals(code)]


# --- extraction ---


def test_extracts_positional_and_keyed_literals():
    code = """
      page.getByTestId('login-email');
      page.getByRole('button', { name: 'Log in', exact: true });
      page.getByText("Save note", { exact: true });
      page.locator('xpath=//h2[normalize-space()="Delete note"]');
      page.locator('li').filter({ hasText: 'Weekend plan' }).getByText('Edit');
      page.getByLabel(`Title`);
    """
    assert _values(code) == [
        "login-email",
        "Log in",
        "Save note",
        'xpath=//h2[normalize-space()="Delete note"]',
        "li",
        "Weekend plan",
        "Edit",
        "Title",
    ]


def test_skips_role_type_variables_templates_comments_and_step_labels():
    code = """
      // page.getByText('in a comment')
      /* page.getByText('block comment') */
      await test.step(`Click 'getByText('x')'`, async () => {
        await expect(page.getByRole('dialog')).toBeVisible();
        await page.getByText(newUserEmail).click();
        await page.getByText(`qa-${unique}@demo.test`).click();
        await expect(page.getByText('Saved', { exact: true })).toHaveText('Saved!');
      });
    """
    assert _values(code) == ["Saved"]


def test_decodes_escapes_and_keeps_regex_source():
    code = r"""page.getByText('Don\'t save'); page.getByRole('link', { name: /About/i });"""
    assert _values(code) == ["Don't save", "/About/i"]


def test_quoted_option_key_counts():
    assert _values("page.getByRole('button', { 'name': 'Go' })") == ["Go"]


def test_division_is_not_a_regex():
    code = "const half = total / 2; page.getByText('Go');"
    assert _values(code) == ["Go"]


# --- check / repair / bounce ---

# The 2026-09-30 regression: the page renders typographic quotes, the model straightened them.
_EMPTY_STATE = models.PlanStep(
    action="Confirm deletion",
    target_selector="getByText('Delete').nth(3)",
    assert_selector="getByText('No notes yet. Click “New note')",
)


def test_straightened_quotes_are_restored_in_place():
    code = "await expect(page.getByText('No notes yet. Click \"New note', { exact: true }));"
    fixed, repaired, unverified = check_spec_literals(code, _plan(_EMPTY_STATE))
    assert unverified == []
    assert repaired == 1
    assert "page.getByText('No notes yet. Click “New note', { exact: true })" in fixed


def test_repair_escapes_for_the_quote_in_use():
    plan = _plan(models.PlanStep(action="a", target_selector="getByText(\"Don't “go”\")"))
    fixed, repaired, unverified = check_spec_literals(
        "page.getByText('Don’t \"go\"')", plan
    )
    assert (repaired, unverified) == (1, [])
    assert fixed == "page.getByText('Don\\'t “go”')"
    assert _values(fixed) == ["Don't “go”"]


def test_whitespace_drift_is_restored():
    plan = _plan(models.PlanStep(action="a", target_selector="getByText('Save  note')"))
    fixed, repaired, _ = check_spec_literals("page.getByText('Save note')", plan)
    assert repaired == 1
    assert fixed == "page.getByText('Save  note')"


def test_invented_heading_is_unverified():
    # The other 2026-09-30 regression: "Open the app" had no assert_selector and the model
    # asserted a heading it made up from the `expected` prose ("Login page is displayed").
    plan = _plan(
        models.PlanStep(action="Open the app", expected="Login page is displayed"),
        models.PlanStep(action="Fill email", target_selector="getByTestId('login-email')"),
    )
    code = """
      await expect(page.getByRole('heading', { name: 'Login', exact: true })).toBeVisible();
      await page.getByTestId('login-email').fill('demo@demo.test');
    """
    fixed, repaired, unverified = check_spec_literals(code, plan)
    assert fixed == code and repaired == 0
    assert [lit.value for lit in unverified] == ["Login"]
    assert unverified[0].call == "getByRole('heading', { name: 'Login', exact: true })"


def test_case_difference_is_not_drift():
    plan = _plan(models.PlanStep(action="a", target_selector="getByText('Log in')"))
    _, repaired, unverified = check_spec_literals("page.getByText('Log In')", plan)
    assert repaired == 0
    assert [lit.value for lit in unverified] == ["Log In"]


def test_plan_literals_in_any_form_pass_unchanged():
    plan = _plan(
        models.PlanStep(
            action="Delete",
            target_selector=(
                "locator('xpath=//h2[normalize-space()=\"Delete note\"]/following::*[1]')"
            ),
            assert_selector="getByRole('link', { name: /About/i })",
            container="dialog 'Delete note'",
        )
    )
    code = """
      const box = page.getByRole('dialog', { name: 'Delete note' });
      await box.locator("xpath=//h2[normalize-space()=\\"Delete note\\"]/following::*[1]").click();
      await expect(page.getByRole('link', { name: /About/i })).toBeVisible();
    """
    fixed, repaired, unverified = check_spec_literals(code, plan)
    assert (fixed, repaired, unverified) == (code, 0, [])


def test_regex_not_in_plan_is_unverified():
    plan = _plan(models.PlanStep(action="a", target_selector="getByText('Log in')"))
    _, _, unverified = check_spec_literals("page.getByRole('heading', { name: /Log in/i })", plan)
    assert [lit.value for lit in unverified] == ["/Log in/i"]


# --- the validator as wired into the Generator ---


def test_validator_bounces_with_the_offending_locator():
    validate = spec_literal_validator(_plan(_EMPTY_STATE))
    out = models.GeneratedTest(
        file_name="NOTE-3.spec.ts",
        code="await expect(page.getByRole('heading', { name: 'Login' })).toBeVisible();",
        description="d",
    )
    with pytest.raises(ModelRetry) as exc:
        validate(out)
    assert "getByRole('heading', { name: 'Login' })" in exc.value.message
    # The retry must not steer the model to rewrite other steps (a 2026-09-30 run turned the
    # selector-less "Open the app" navigation into an UNVERIFIED throw after such a hint).
    assert "keep every other line" in exc.value.message
    assert "UNVERIFIED" not in exc.value.message


def test_validator_returns_repaired_code():
    validate = spec_literal_validator(_plan(_EMPTY_STATE))
    out = models.GeneratedTest(
        file_name="NOTE-3.spec.ts",
        code="page.getByText('No notes yet. Click \"New note');",
        description="d",
    )
    assert validate(out).code == "page.getByText('No notes yet. Click “New note');"


def test_generator_run_retries_until_locators_come_from_the_plan(cfg):
    plan = _plan(_EMPTY_STATE)
    drafts = iter(
        [
            "await expect(page.getByRole('heading', { name: 'Login' })).toBeVisible();",
            "await expect(page.getByText('No notes yet. Click \"New note'));",
        ]
    )
    seen_retry: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for part in messages[-1].parts:
            if isinstance(part, RetryPromptPart):
                seen_retry.append(part.model_response())
        args = {"file_name": "NOTE-3.spec.ts", "code": next(drafts), "description": "d"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    agent = build_generator(cfg)
    agent.output_validator(spec_literal_validator(plan))
    with agent.override(model=FunctionModel(model)):
        out = asyncio.run(agent.run("plan")).output
    assert out.code == "await expect(page.getByText('No notes yet. Click “New note'));"
    assert len(seen_retry) == 1 and "'Login'" in seen_retry[0]
