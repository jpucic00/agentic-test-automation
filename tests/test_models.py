"""Unit tests for ai_test_gen.models — fully local (no network).

Models are referenced via the ``models`` module rather than imported by name:
``TestPlan`` / ``TestRunResult`` start with "Test", so importing them as
module-level names would make pytest try (and warn about) collecting them as
test classes.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from ai_test_gen import models

# One representative, fully-populated instance of every model.
_INSTANCES: list[BaseModel] = [
    models.ManualTestCase(
        key="QA-1234",
        title="Login happy path",
        description="User can log in with valid credentials.",
        preconditions=["User is logged out"],
        steps=["Navigate to /login", "Fill credentials and submit"],
        expected_results=["Login form is visible", "Redirected to /dashboard"],
        labels=["smoke", "auth"],
    ),
    models.PlanStep(
        action="Click the login submit button",
        target_selector="#login-submit",
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
                target_selector="#login-email",
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
        error_message="locator '#login-submit' not found",
        trace_path="output/runs/trace.zip",
    ),
    models.HealedTest(
        file_name="QA-1234-login-happy-path.spec.ts",
        code="import { test, expect } from '@playwright/test';",
        changes_summary="Updated the submit selector from #submit to #login-submit.",
    ),
]

_MODEL_CLASSES: list[type[BaseModel]] = [
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
