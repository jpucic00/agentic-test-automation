"""Offline tests for the verification bounce loop (rag/verify.py).

Pin the §1.14 contract in isolation: a claim whose value exists at its citation is
verified; a wrong-file citation whose value exists elsewhere is auto-fixed (never
dropped); a value found nowhere is flagged unverified; template values check by
their static skeleton parts. Also pins the §1.13 escalation signals.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_test_gen.models import ManualStep
from ai_test_gen.rag.models import (
    ReconstructedPlan,
    ReconstructedSelector,
    ReconstructedStep,
)
from ai_test_gen.rag.tools import RepoTools
from ai_test_gen.rag.verify import (
    build_revalidation_message,
    collect_claims,
    escalation_signals,
    literal_fragments,
    verify_plan,
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    _write(
        root,
        "pages/LoginPage.java",
        'class LoginPage {\n'
        '  public static final By EMAIL = By.id("login-email");\n'
        '  public static final By SUBMIT = By.id("login-submit");\n'
        "}\n",
    )
    _write(
        root,
        "res/locators.properties",
        "delete.confirm=//div[contains(@class,'modal')]//div[normalize-space()='Delete']\n",
    )
    _write(
        root,
        "pages/NotesPage.java",
        "class NotesPage {\n"
        '  private static final String ROW = "//li[contains(@class,\'note-item\')]'
        "[.//h3[normalize-space()='%s']]\";\n"
        "}\n",
    )
    return root


def _selector(value: str, provenance: str) -> ReconstructedSelector:
    return ReconstructedSelector(kind="css", value=value, provenance=provenance)


def _plan(*steps: ReconstructedStep) -> ReconstructedPlan:
    return ReconstructedPlan(title="t", steps=list(steps))


class TestLiteralFragments:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ('By.id("login-email")', ["login-email"]),
            ("getByTestId('save')", ["save"]),
            # Outer double-quoted content wins even when it contains single quotes.
            (
                "By.xpath(\"//div[normalize-space()='New note']\")",
                ["//div[normalize-space()='New note']"],
            ),
            # No quotes at all → the whole value is the fragment.
            ("LOGIN_ID", ["LOGIN_ID"]),
            # A runtime-built template checks by its static skeleton parts.
            (
                '"//li[.//h3[text()=\'" + title + "\']]"',
                ["//li[.//h3[text()='", "']]"],
            ),
            ("", []),
        ],
    )
    def test_fragments(self, value: str, expected: list[str]) -> None:
        assert literal_fragments(value) == expected


class TestVerifyPlan:
    def test_claim_verified_at_its_citation(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        step = ReconstructedStep(
            action="enter email",
            selector=_selector('By.id("login-email")', "pages/LoginPage.java#EMAIL"),
        )
        plan = _plan(step)
        outcome = verify_plan(plan, tools)
        assert outcome.cited == 1
        assert outcome.verified == 1
        assert outcome.unverified == []
        assert step.selector is not None and step.selector.verified

    def test_wrong_file_citation_is_auto_fixed_not_dropped(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        step = ReconstructedStep(
            action="confirm delete",
            # Value really lives in res/locators.properties; the citation is wrong.
            selector=_selector(
                "//div[contains(@class,'modal')]//div[normalize-space()='Delete']",
                "pages/NotesPage.java#CONFIRM",
            ),
        )
        plan = _plan(step)
        outcome = verify_plan(plan, tools)
        assert outcome.verified == 1
        assert len(outcome.auto_fixed) == 1
        assert step.selector is not None
        assert step.selector.provenance == "res/locators.properties"
        assert step.selector.verified

    def test_fake_citation_is_flagged_unverified(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        step = ReconstructedStep(
            action="click ghost",
            selector=_selector('By.id("no-such-element")', "pages/LoginPage.java#GHOST"),
        )
        plan = _plan(step)
        outcome = verify_plan(plan, tools)
        assert outcome.verified == 0
        assert len(outcome.unverified) == 1
        assert step.selector is not None and not step.selector.verified

    def test_assert_hints_are_claims_too(self, corpus: Path) -> None:
        step = ReconstructedStep(
            action="submit",
            selector=_selector('By.id("login-submit")', "pages/LoginPage.java#SUBMIT"),
            assert_hint=_selector('By.id("login-email")', "pages/LoginPage.java#EMAIL"),
        )
        plan = _plan(step)
        assert {(c.slot) for c in collect_claims(plan)} == {"selector", "assert_hint"}
        outcome = verify_plan(plan, RepoTools([corpus]))
        assert outcome.cited == 2
        assert outcome.verified == 2

    def test_template_value_checks_by_its_skeleton_parts(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        step = ReconstructedStep(
            action="find row",
            selector=_selector(
                '"//li[contains(@class,\'note-item\')][.//h3[normalize-space()=\'" + title',
                "pages/NotesPage.java#ROW",
            ),
        )
        outcome = verify_plan(_plan(step), tools)
        assert outcome.verified == 1

    def test_text_cache_is_shared_across_calls(self, corpus: Path) -> None:
        tools = RepoTools([corpus])
        cache: dict[str, str] = {}
        step = ReconstructedStep(
            action="a",
            selector=_selector('By.id("login-email")', "pages/LoginPage.java"),
        )
        verify_plan(_plan(step), tools, text_cache=cache)
        assert "pages/LoginPage.java" in cache


class TestRevalidationMessage:
    def test_message_names_each_failed_claim(self, corpus: Path) -> None:
        step = ReconstructedStep(
            action="click ghost",
            selector=_selector('By.id("no-such-element")', "pages/LoginPage.java#GHOST"),
        )
        plan = _plan(step)
        outcome = verify_plan(plan, RepoTools([corpus]))
        message = build_revalidation_message(outcome.unverified)
        assert "no-such-element" in message
        assert "pages/LoginPage.java#GHOST" in message
        assert "recheck" in message.lower() or "re-check" in message.lower()
        assert "remove" in message


class TestEscalationSignals:
    def test_selectorless_ui_record_is_flagged(self) -> None:
        plan = _plan(ReconstructedStep(action="do something"))
        assert "selectorless-ui" in escalation_signals(plan, "ui", [], 1, [])

    def test_api_record_without_selectors_is_fine(self) -> None:
        plan = _plan(ReconstructedStep(action="POST /api/notes"))
        assert escalation_signals(plan, "api", [], 1, []) == []

    def test_shallow_plan_vs_manual_steps(self) -> None:
        plan = _plan(ReconstructedStep(action="only step"))
        manual = [ManualStep(action="a"), ManualStep(action="b"), ManualStep(action="c")]
        signals = escalation_signals(plan, "api", manual, 1, [])
        assert any(s.startswith("shallow-plan") for s in signals)

    def test_zero_files_opened_and_unresolved(self) -> None:
        plan = _plan(
            ReconstructedStep(
                action="s", selector=_selector("By.id(\"x\")", "f.java")
            )
        )
        signals = escalation_signals(plan, "ui", [], 0, ["helper.doThing()"])
        assert "no-files-opened" in signals
        assert any(s.startswith("self-reported-unresolved") for s in signals)
