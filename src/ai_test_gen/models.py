"""Shared Pydantic data models that flow between agents.

These classes are the data contract for the pipeline: Xray client → Planner →
Generator → Test runner → Healer → GitLab client (AI_TEST_GENERATION_GUIDE.md §3.5).

Pydantic AI uses these as structured-output schemas, so EVERY field carries a
``description`` — the descriptions are serialized into the JSON schema the model
sees, and materially affect output quality.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# === Input from Xray ===


class ManualTestCase(BaseModel):
    """A test case as it lives in Jira/Xray, normalized to a simple shape."""

    key: str = Field(description="Jira issue key, e.g. 'QA-1234'")
    title: str = Field(description="Human-readable test case title (the Jira summary)")
    description: str = Field(default="", description="Free-text description; may be empty")
    preconditions: list[str] = Field(
        default_factory=list,
        description="Preconditions that must hold before the test runs",
    )
    steps: list[str] = Field(
        default_factory=list, description="Action to perform, one per step"
    )
    expected_results: list[str] = Field(
        default_factory=list,
        description="Expected result, paired with steps by index",
    )
    labels: list[str] = Field(
        default_factory=list, description="Jira labels on the test case issue"
    )


# === Planner output ===


class PlanStep(BaseModel):
    action: str = Field(
        description="Imperative description of what the test does at this step"
    )
    target_selector: str | None = Field(
        default=None,
        description=(
            "Best selector for the target element if known, e.g. '#login-button' "
            "or 'role=button[name=\"Submit\"]'"
        ),
    )
    expected: str | None = Field(
        default=None, description="Expected outcome to assert, if any"
    )


class TestPlan(BaseModel):
    """Structured plan produced by the Planner agent."""

    test_case_key: str = Field(
        description="Jira issue key this plan was derived from, e.g. 'QA-1234'"
    )
    title: str = Field(description="Human-readable title of the planned test")
    target_url: str = Field(
        description="The URL where the test should start, e.g. staging login page"
    )
    preconditions: list[str] = Field(
        default_factory=list,
        description="Preconditions to satisfy before executing the steps",
    )
    steps: list[PlanStep] = Field(
        description="Ordered plan steps the Generator turns into Playwright code"
    )
    notes: str = Field(
        default="",
        description=(
            "Free-form notes from the Planner, e.g. flaky behaviors observed, "
            "auth quirks, alternative selectors"
        ),
    )


# === Generator output ===


class GeneratedTest(BaseModel):
    """Output of the Generator agent."""

    file_name: str = Field(
        description="Filename for the test file, e.g. 'QA-1234-login-happy-path.spec.ts'"
    )
    code: str = Field(description="Complete Playwright TypeScript test code")
    description: str = Field(description="One-line description of what the test does")


# === Test runner output ===


class TestRunResult(BaseModel):
    status: Literal["passed", "failed", "error"] = Field(
        description="Outcome of the test run"
    )
    stdout: str = Field(description="Captured standard output from the Playwright run")
    stderr: str = Field(description="Captured standard error from the Playwright run")
    failed_test: str | None = Field(
        default=None, description="Title of the first failing test, if any"
    )
    error_message: str | None = Field(
        default=None, description="Error message extracted from the failure, if any"
    )
    trace_path: str | None = Field(
        default=None, description="Path to the Playwright trace.zip, if one was produced"
    )


# === Healer output ===


class HealedTest(BaseModel):
    file_name: str = Field(description="Filename of the healed test file")
    code: str = Field(description="Complete healed Playwright TypeScript test code")
    changes_summary: str = Field(description="What the healer changed and why")
