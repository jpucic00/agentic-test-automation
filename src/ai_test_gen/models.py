"""Shared Pydantic data models that flow between agents.

These classes are the data contract for the pipeline: Xray client → Planner →
Generator → Test runner → Healer → GitLab client (AI_TEST_GENERATION_GUIDE.md §3.5).

Pydantic AI uses these as structured-output schemas, so EVERY field carries a
``description`` — the descriptions are serialized into the JSON schema the model
sees, and materially affect output quality.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _safe_spec_filename(value: str) -> str:
    """Reduce an LLM-generated test filename to a bare, traversal-free basename.

    ``file_name`` is later used as a filesystem path (``test_runner``) and a GitLab
    commit path (``gitlab_client``). Taking ``Path(value).name`` strips any directory
    parts or absolute prefix, so a model-emitted ``../x`` or ``/abs/x`` can never be
    written outside ``output/tests/`` / ``tests/generated/``. A value that reduces to
    nothing is rejected rather than producing a nameless file.
    """
    name = Path(value).name.strip()
    # Path("..").name is ".." (not ""), so the dot-segments must be rejected explicitly —
    # otherwise a name like "../" would slip through as a traversal component.
    if not name or name in {".", ".."}:
        raise ValueError(f"file_name {value!r} does not reduce to a usable filename")
    return name


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
            "Verified Playwright locator expression (no 'page.' prefix), the most robust kind "
            "the element supports along the resilience ladder id > accessible > CSS > XPath: "
            "getByTestId('login-submit') (id); getByRole('button', { name: 'Save', exact: true }) "
            "or getByLabel('Email', { exact: true }) (accessible); locator('css=...') (CSS); or "
            "locator('xpath=//...') (XPath, for inaccessible elements). Always captured + verified "
            "live, never invented. None if none could be verified."
        ),
    )
    expected: str | None = Field(
        default=None, description="Expected outcome to assert, if any"
    )
    page_url: str | None = Field(
        default=None,
        description=(
            "URL of the page this step was performed on, copied from the live session's "
            "Page URL header. None if unknown."
        ),
    )
    container: str | None = Field(
        default=None,
        description=(
            "Closest enclosing dialog/menu/drawer of the target element exactly as the "
            "live snapshot names it, e.g. \"dialog 'Create user'\" — observed only, never "
            "invented. None for page-level elements. The Generator scopes the step's "
            "locator to this container."
        ),
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

    @field_validator("file_name")
    @classmethod
    def _basename_only(cls, v: str) -> str:
        return _safe_spec_filename(v)


# === Test runner output ===


class TestRunResult(BaseModel):
    status: Literal["passed", "failed", "error"] = Field(
        description="Outcome of the test run"
    )
    did_run: bool = Field(
        default=True,
        description=(
            "False when Playwright produced no parseable JSON report — the spec failed "
            "to compile/collect and never executed. The orchestrator routes that class "
            "back to the Generator (no browser needed), not the Healer."
        ),
    )
    stdout: str = Field(description="Captured standard output from the Playwright run")
    stderr: str = Field(description="Captured standard error from the Playwright run")
    failed_test: str | None = Field(
        default=None, description="Title of the first failing test, if any"
    )
    error_message: str | None = Field(
        default=None, description="Error message extracted from the failure, if any"
    )
    error_line: int | None = Field(
        default=None,
        description=(
            "1-based line in the spec file where the run died (from the Playwright "
            "error location/stack). Code after this line never executed."
        ),
    )
    trace_path: str | None = Field(
        default=None, description="Path to the Playwright trace.zip, if one was produced"
    )


# === Healer output ===


class HealedTest(BaseModel):
    file_name: str = Field(description="Filename of the healed test file")
    code: str = Field(description="Complete healed Playwright TypeScript test code")
    changes_summary: str = Field(description="What the healer changed and why")

    @field_validator("file_name")
    @classmethod
    def _basename_only(cls, v: str) -> str:
        return _safe_spec_filename(v)
