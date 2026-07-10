"""Unit tests for ai_test_gen.models — fully local (no network).

Models are referenced via the ``models`` module rather than imported by name:
``TestPlan`` / ``TestRunResult`` start with "Test", so importing them as
module-level names would make pytest try (and warn about) collecting them as
test classes.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from ai_test_gen import models

# One representative, fully-populated instance of every model.
_INSTANCES: list[BaseModel] = [
    models.ManualStep(
        action="Fill credentials and submit",
        data="qa.bot@example.internal / hunter2",
        expected="Redirected to /dashboard",
    ),
    models.ManualTestCase(
        key="QA-1234",
        title="Login happy path",
        description="User can log in with valid credentials.",
        preconditions=["User is logged out"],
        steps=[
            models.ManualStep(action="Navigate to /login", expected="Login form is visible"),
            models.ManualStep(
                action="Fill credentials and submit",
                data="qa.bot / hunter2",
                expected="Redirected to /dashboard",
            ),
        ],
        labels=["smoke", "auth"],
    ),
    models.PlanStep(
        action="Click the login submit button",
        target_selector="getByTestId('login-submit')",
        expected="Dashboard is visible",
    ),
    models.TestPlan(
        test_case_key="QA-1234",
        title="Login happy path",
        target_url="https://staging.example.internal/login",
        preconditions=["User is logged out"],
        steps=[
            models.PlanStep(
                action="Fill the email field",
                target_selector="getByTestId('login-email')",
                expected="Field accepts input",
            )
        ],
        notes="Auth wall dismissed via storage_state.",
    ),
    models.GeneratedTest(
        file_name="QA-1234-login-happy-path.spec.ts",
        code="import { test, expect } from '@playwright/test';",
        description="Verifies the login happy path.",
    ),
    models.TestRunResult(
        status="failed",
        stdout="Running 1 test...",
        stderr="TimeoutError: locator not found",
        failed_test="login happy path",
        error_message="getByTestId('login-submit') resolved to no element",
        trace_path="output/snapshots/trace.zip",
    ),
    models.HealedTest(
        file_name="QA-1234-login-happy-path.spec.ts",
        code="import { test, expect } from '@playwright/test';",
        changes_summary="Switched the submit locator to getByTestId('login-submit').",
    ),
]

_MODEL_CLASSES: list[type[BaseModel]] = [
    models.ManualStep,
    models.ManualTestCase,
    models.PlanStep,
    models.TestPlan,
    models.GeneratedTest,
    models.TestRunResult,
    models.HealedTest,
]


@pytest.mark.parametrize("instance", _INSTANCES, ids=lambda m: type(m).__name__)
def test_round_trips_through_json(instance: BaseModel):
    cls = type(instance)
    restored = cls.model_validate_json(instance.model_dump_json())
    assert restored == instance


@pytest.mark.parametrize("model_cls", _MODEL_CLASSES, ids=lambda c: c.__name__)
def test_every_field_has_a_description(model_cls: type[BaseModel]):
    for name, field in model_cls.model_fields.items():
        description = field.description
        assert isinstance(description, str) and description.strip(), (
            f"{model_cls.__name__}.{name} is missing a Field(description=...)"
        )


def test_generated_test_file_name_reduced_to_basename():
    """A model-emitted traversal path is neutralized to a bare filename."""
    t = models.GeneratedTest(
        file_name="../../etc/QA-1-login.spec.ts", code="// x", description="login"
    )
    assert t.file_name == "QA-1-login.spec.ts"


def test_healed_test_file_name_reduced_to_basename():
    h = models.HealedTest(
        file_name="/tmp/evil/QA-1-login.spec.ts", code="// x", changes_summary="fix"
    )
    assert h.file_name == "QA-1-login.spec.ts"


def test_generated_test_rejects_unusable_file_name():
    with pytest.raises(ValidationError):
        models.GeneratedTest(file_name="../", code="// x", description="login")
