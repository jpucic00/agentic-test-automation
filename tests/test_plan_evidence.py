"""Unit tests for the Planner's plan-evidence validator (agents/runtime/plan_evidence.py)."""
from __future__ import annotations

import asyncio
import dataclasses
import logging

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_test_gen.agents.planner import build_planner
from ai_test_gen.agents.runtime.plan_evidence import (
    MAX_BOUNCES,
    Evidence,
    check_plan_literals,
    run_evidence,
)
from ai_test_gen.core import models

# A real Playwright MCP snapshot excerpt from the demo app's empty notes list (2026-09-30).
SNAPSHOT = """- main [ref=e8]:
    - generic [ref=e31]:
      - heading "Your notes" [level=1] [ref=e33]
      - generic [ref=e34] [cursor=pointer]: New note
    - paragraph [ref=e35]: No notes yet. Click “New note” to add one.
"""


def _plan(*steps: models.PlanStep) -> models.TestPlan:
    return models.TestPlan(test_case_key="NOTE-5", title="t", target_url="/", steps=list(steps))


def _step(target: str | None = None, assert_: str | None = None) -> models.PlanStep:
    return models.PlanStep(action="a", target_selector=target, assert_selector=assert_)


def test_straightened_quotes_are_restored_to_the_page_text():
    # The 2026-09-30 NOTE-5 plan: gpt-oss retyped the page's curly quotes as straight ones.
    plan = _plan(
        _step(
            "getByText('New note').nth(1)",
            "getByText('No notes yet. Click \"New note\" to add one.')",
        )
    )
    fixed, repaired, unverified = check_plan_literals(plan, Evidence([SNAPSHOT]))
    assert (repaired, unverified) == (1, [])
    assert fixed.steps[0].assert_selector == (
        "getByText('No notes yet. Click “New note” to add one.')"
    )
    assert fixed.steps[0].target_selector == "getByText('New note').nth(1)"


def test_verbatim_and_partial_text_passes_unchanged():
    plan = _plan(_step("getByText('New note', { exact: true })", "getByText('No notes yet')"))
    fixed, repaired, unverified = check_plan_literals(plan, Evidence([SNAPSHOT]))
    assert (fixed, repaired, unverified) == (plan, 0, [])


def test_text_no_page_showed_is_unverified():
    plan = _plan(
        _step(), _step("getByTestId('login-email')", "getByRole('heading', { name: 'Login' })")
    )
    evidence = Evidence([SNAPSHOT, "getByTestId('login-email')"])
    _, repaired, unverified = check_plan_literals(plan, evidence)
    assert repaired == 0
    assert [(n, field, lit.value) for n, field, lit in unverified] == [
        (2, "assert_selector", "Login")
    ]


def test_escaped_quotes_in_the_evidence_still_match():
    plan = _plan(_step("getByRole('button', { name: 'Say \"hi\"' })"))
    _, _, unverified = check_plan_literals(plan, Evidence(['- button "Say \\"hi\\"" [ref=e2]']))
    assert unverified == []


def test_regex_literals_are_not_checked():
    plan = _plan(_step("getByRole('link', { name: /About/i })"))
    assert check_plan_literals(plan, Evidence([""]))[2] == []


def test_run_evidence_reads_tool_returns_typed_values_and_snapshot_files(tmp_path):
    (tmp_path / "page-1.yml").write_text(SNAPSHOT)
    (tmp_path / "page-1.png").write_bytes(b"\x89PNG")
    messages: list[ModelMessage] = [
        ModelResponse(
            parts=[
                ToolCallPart("browser_type", {"ref": "e40", "text": "Disposable-42"}),
                ToolCallPart("browser_find", {"text": "Invented heading"}),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart("browser_generate_locator", "getByTestId('login-email')"),
                ToolReturnPart("count_matches", [{"type": "text", "text": "matches exactly 1"}]),
            ]
        ),
    ]
    texts = run_evidence(messages, tmp_path)
    assert "Disposable-42" in texts
    assert "getByTestId('login-email')" in texts
    assert "matches exactly 1" in texts
    assert SNAPSHOT in texts
    # A search the model ran is not evidence that the page showed that text.
    assert all("Invented heading" not in t for t in texts)


def test_tool_results_echoing_their_own_query_are_not_evidence():
    # 2026-09-30: the Planner recorded getByText('unique new-user email per the') — a phrase from
    # its own prompt. count_matches repeats the selector it was asked about, even on 0 matches.
    selector = "text=unique new-user email per the"
    messages: list[ModelMessage] = [
        ModelResponse(parts=[ToolCallPart("count_matches", {"selector": selector}, "c1")]),
        ModelRequest(
            parts=[ToolReturnPart("count_matches", f"`{selector}` matches 0 elements.", "c1")]
        ),
    ]
    plan = _plan(_step("getByText('unique new-user email per the')"))
    _, _, unverified = check_plan_literals(plan, Evidence(run_evidence(messages, None)))
    assert [lit.value for _, _, lit in unverified] == ["unique new-user email per the"]


def test_raw_css_and_xpath_selectors_are_left_to_count_matches():
    plan = _plan(_step("locator('xpath=//h2[normalize-space()=\"Delete note\"]')"))
    assert check_plan_literals(plan, Evidence([""]))[2] == []


def test_run_evidence_without_a_snapshot_folder(tmp_path):
    assert run_evidence([], tmp_path / "missing") == []


# --- the validator as wired into the Planner ---


def _planner_run(cfg, plans: list[dict], evidence: str) -> tuple[models.TestPlan, list[str]]:
    """Run the real Planner build with a scripted model: one tool call, then the given plans."""
    queue = iter(plans)
    retries: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for part in messages[-1].parts:
            if isinstance(part, RetryPromptPart):
                retries.append(part.model_response())
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("read_page", {})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, next(queue))])

    agent = build_planner(cfg)

    @agent.tool_plain(name="read_page")
    def _read_page() -> str:  # pyright: ignore[reportUnusedFunction]
        return evidence

    with agent.override(model=FunctionModel(model), toolsets=[]):
        out = asyncio.run(agent.run("plan")).output
    return out, retries


def _plan_args(assert_selector: str) -> dict:
    return {
        "test_case_key": "NOTE-5",
        "title": "t",
        "target_url": "/",
        "steps": [{"action": "Delete", "assert_selector": assert_selector}],
    }


def test_planner_retries_then_restores_page_characters(cfg, tmp_path):
    cfg = dataclasses.replace(cfg, snapshots_dir=tmp_path / "snapshots")
    out, retries = _planner_run(
        cfg,
        [
            _plan_args("getByRole('heading', { name: 'Login' })"),
            _plan_args("getByText('No notes yet. Click \"New note\"')"),
        ],
        evidence=SNAPSHOT,
    )
    assert len(retries) == 1 and "'Login'" in retries[0]
    assert out.steps[0].assert_selector == "getByText('No notes yet. Click “New note”')"


def test_planner_accepts_after_max_bounces(cfg, tmp_path, caplog):
    cfg = dataclasses.replace(cfg, snapshots_dir=tmp_path / "snapshots")
    invented = _plan_args("getByRole('heading', { name: 'Login' })")
    with caplog.at_level(logging.WARNING):
        out, retries = _planner_run(cfg, [invented] * (MAX_BOUNCES + 1), evidence=SNAPSHOT)
    assert len(retries) == MAX_BOUNCES
    assert out.steps[0].assert_selector == "getByRole('heading', { name: 'Login' })"
    assert "accepting the plan" in caplog.text
