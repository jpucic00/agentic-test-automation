"""Distiller agent tests — prompt budget, message assembly, TestModel round-trip."""
from __future__ import annotations

from pathlib import Path

from pydantic_ai.models.test import TestModel

from ai_test_gen.models import ManualTestCase
from ai_test_gen.rag.distiller import (
    DistilledCase,
    build_distill_message,
    build_distiller,
)
from ai_test_gen.rag.extract import ExtractedLocator, TestBundle

PROMPT = Path("src/ai_test_gen/prompts/distiller.md")


def _bundle() -> TestBundle:
    return TestBundle(
        ref="suite/test/java/LoginTest.java#loginFails",
        test_name="loginFails",
        class_name="LoginTest",
        language="java",
        xray_key="QA-4",
        code='// LoginTest\npublic void loginFails() { login.loginAs("a", "b"); }',
        helper_snippets=['// LoginPage.loginAs\npublic void loginAs(...) { click(SUBMIT); }'],
        helper_refs=[
            "LoginPage.loginAs (main/LoginPage.java)",
            "unresolved:ReportingClient.record",
        ],
        locators=[
            ExtractedLocator(
                kind="testid", value='By.id("login-submit")', declared_in="LoginPage.SUBMIT"
            )
        ],
        urls=["/login"],
    )


class TestPrompt:
    def test_stays_within_the_house_word_budget(self) -> None:
        words = len(PROMPT.read_text().split())
        assert words <= 850, f"distiller.md is {words} words — budget is ~800"

    def test_encodes_the_non_negotiable_rules(self) -> None:
        text = PROMPT.read_text()
        assert "ground truth" in text
        assert "NEVER invent a locator" in text
        assert "SAME LANGUAGE" in text  # intent_text stays in the original language
        assert "no tools" in text.lower()


class TestMessageAssembly:
    def test_bundle_sections_all_present(self) -> None:
        case = ManualTestCase(
            key="QA-4",
            title="Login fails with a wrong password",
            steps=["Open the login page", "Submit wrong credentials"],
            expected_results=["An error message is shown"],
        )
        message = build_distill_message(_bundle(), case)

        assert "Selenium/Java" in message
        assert 'login.loginAs("a", "b")' in message  # test code
        assert "LoginPage.loginAs" in message  # helper snippet
        assert "EXTRACTED LOCATORS (ground truth" in message
        assert 'By.id("login-submit")' in message
        assert "unresolved:ReportingClient.record" in message  # opaque-step guidance
        assert "Linked manual test case QA-4" in message
        assert "An error message is shown" in message

    def test_message_without_case_omits_the_section(self) -> None:
        message = build_distill_message(_bundle(), None)
        assert "Linked manual test case" not in message


class TestAgentRoundTrip:
    def test_structured_output_via_testmodel(self, cfg) -> None:
        """The DistilledCase schema is materializable by pydantic-ai (offline)."""
        agent = build_distiller(cfg)
        with agent.override(model=TestModel()):
            result = agent.run_sync(build_distill_message(_bundle(), None))
        assert isinstance(result.output, DistilledCase)

    def test_agent_uses_the_configured_distiller_model(self, cfg) -> None:
        agent = build_distiller(cfg)
        # conftest cfg pins distiller_model="distiller-model"
        assert getattr(agent.model, "model_name", None) == "distiller-model"
