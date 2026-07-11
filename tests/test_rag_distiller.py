"""Offline tests for the agentic Distiller (rag/distiller.py).

The model is replaced by canned ``DistillTurns``: ``distill_test`` must verify the
first output, bounce AT MOST once (only when claims failed), and ship survivors
flagged — plus honest per-record instrumentation. The degraded two-call mode is
pinned with injected structured-call fakes: file request → code reads → distill.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ai_test_gen.config import Config
from ai_test_gen.models import ManualStep, ManualTestCase
from ai_test_gen.rag.discover import DiscoveredTest
from ai_test_gen.rag.distiller import (
    DistillDraft,
    DistillOutput,
    DraftPlan,
    DraftSelector,
    DraftStep,
    FileRequestList,
    TwoCallTurns,
    build_distill_message,
    distill_test,
    draft_to_output,
    render_manual_triplets,
)
from ai_test_gen.rag.models import (
    ReconstructedPlan,
    ReconstructedSelector,
    ReconstructedStep,
    make_record_id,
)
from ai_test_gen.rag.tools import RepoTools


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
        'class LoginPage {\n  public static final By EMAIL = By.id("login-email");\n}\n',
    )
    _write(
        root,
        "tests/LoginTest.java",
        "class LoginTest {\n"
        '  @Xray(testCase = "TC-1")\n'
        "  public void loginWorks() { new LoginPage(driver).open(baseUrl); }\n"
        "}\n",
    )
    return root


def _test(corpus_kind: str = "java") -> DiscoveredTest:
    ref = "tests/LoginTest.java#loginWorks"
    return DiscoveredTest(
        ref=ref,
        path="tests/LoginTest.java",
        symbol="LoginTest.loginWorks",
        language="java" if corpus_kind == "java" else "ts",
        source="selenium-import" if corpus_kind == "java" else "playwright-import",
        xray_key="TC-1",
        record_id=make_record_id("TC", "selenium-import", "TC-1"),
        code='@Xray(testCase = "TC-1")\npublic void loginWorks() { … }',
    )


def _output(*steps: ReconstructedStep, unresolved: list[str] | None = None) -> DistillOutput:
    return DistillOutput(
        plan=ReconstructedPlan(title="Login works", steps=list(steps)),
        kind="ui",
        routes=["/login"],
        unresolved=unresolved or [],
    )


def _step(value: str, provenance: str) -> ReconstructedStep:
    return ReconstructedStep(
        action="enter email",
        selector=ReconstructedSelector(kind="css", value=value, provenance=provenance),
    )


class FakeTurns:
    """Canned turns: first() and revalidate() pop successive outputs, log messages."""

    def __init__(self, *outputs: DistillOutput) -> None:
        self.outputs = list(outputs)
        self.first_messages: list[str] = []
        self.revalidate_messages: list[str] = []

    async def first(self, message: str) -> DistillOutput:
        self.first_messages.append(message)
        return self.outputs.pop(0)

    async def revalidate(self, message: str) -> DistillOutput:
        self.revalidate_messages.append(message)
        return self.outputs.pop(0)


def _distill(cfg: Config, corpus: Path, turns: FakeTurns, case: ManualTestCase | None = None):
    tools = RepoTools([corpus])
    return asyncio.run(
        distill_test(
            cfg,
            tools,
            _test(),
            case,
            address="tests/LoginTest.java",
            map_index="**TC** — 1 test(s).",
            suite_block="- `tests` — the suite",
            turns=turns,
        )
    )


class TestDistillFlow:
    def test_clean_output_never_bounces(self, cfg: Config, corpus: Path) -> None:
        turns = FakeTurns(_output(_step('By.id("login-email")', "pages/LoginPage.java#EMAIL")))
        result = _distill(cfg, corpus, turns)
        assert turns.revalidate_messages == []
        assert result.bounced_claims == 0
        assert result.verify.verified == 1
        step = result.output.plan.steps[0]
        assert step.selector is not None and step.selector.verified

    def test_failed_claim_bounces_exactly_once(self, cfg: Config, corpus: Path) -> None:
        bad = _output(_step('By.id("ghost")', "pages/LoginPage.java#GHOST"))
        fixed = _output(_step('By.id("login-email")', "pages/LoginPage.java#EMAIL"))
        turns = FakeTurns(bad, fixed)
        result = _distill(cfg, corpus, turns)
        assert len(turns.revalidate_messages) == 1
        assert "ghost" in turns.revalidate_messages[0]
        assert result.bounced_claims == 1
        assert result.verify.verified == 1
        assert result.verify.unverified == []

    def test_bounce_survivor_ships_flagged_never_dropped(self, cfg: Config, corpus: Path) -> None:
        bad = _output(_step('By.id("ghost")', "pages/LoginPage.java#GHOST"))
        still_bad = _output(_step('By.id("ghost")', "pages/LoginPage.java#GHOST"))
        turns = FakeTurns(bad, still_bad)
        result = _distill(cfg, corpus, turns)
        assert len(turns.revalidate_messages) == 1  # ONE round, never a second
        step = result.output.plan.steps[0]
        assert step.selector is not None
        assert not step.selector.verified  # flagged …
        assert result.output.plan.steps  # … but kept
        assert result.trace.selectors_unverified == 1

    def test_trace_reports_exploration_and_unresolved(self, cfg: Config, corpus: Path) -> None:
        output = _output(
            _step('By.id("login-email")', "pages/LoginPage.java#EMAIL"),
            unresolved=["ReportingClient.record(...)"],
        )
        turns = FakeTurns(output)
        tools = RepoTools([corpus])
        tools.read_file("pages/LoginPage.java")  # simulated exploration
        result = asyncio.run(
            distill_test(
                cfg, tools, _test(), None, address="tests/LoginTest.java", turns=turns
            )
        )
        assert result.trace.files_opened == ["pages/LoginPage.java"]
        assert result.trace.tool_calls == 1
        assert result.trace.selectors_cited == 1
        assert result.trace.unresolved == ["ReportingClient.record(...)"]


class TestDistillMessage:
    def test_message_carries_source_case_map_and_suite(self) -> None:
        case = ManualTestCase(
            key="TC-1",
            title="Login",
            steps=[
                ManualStep(action="Log in", data="demo@demo.test / pw", expected="Notes shown")
            ],
        )
        message = build_distill_message(
            _test(), "tests/LoginTest.java", case, "**TC** at a glance", "- `tests` — suite"
        )
        assert "loginWorks" in message and "```java" in message
        assert "Log in" in message
        assert "- Data: demo@demo.test / pw" in message
        assert "- Expected: Notes shown" in message
        assert "**TC** at a glance" in message
        assert "- `tests` — suite" in message
        assert "read `tests/LoginTest.java`" in message  # java scope note

    def test_message_without_case_says_so(self) -> None:
        message = build_distill_message(_test(), "tests/LoginTest.java", None, "", "")
        assert "none available" in message

    def test_manual_triplets_omit_empty_cells(self) -> None:
        rendered = render_manual_triplets(
            [ManualStep(action="Open the app"), ManualStep(action="Save", expected="Saved")]
        )
        assert "- Data:" not in rendered
        assert "- Expected: Saved" in rendered


class TestDraftSchema:
    """The model-facing schema must stay grammar-friendly: live evidence (2026-07-11,
    Gemma 4 via OpenRouter) showed `anyOf [object, null]` unions kill every turn as an
    empty husk, while the union-free Mapper schema worked in the same session."""

    def test_model_facing_schema_has_no_unions(self) -> None:
        schema = json.dumps(DistillDraft.model_json_schema())
        assert "anyOf" not in schema
        assert "oneOf" not in schema
        # `verified` is pipeline-owned — the model never authors it.
        assert '"verified"' not in schema

    def test_draft_maps_to_the_stored_plan_shape(self) -> None:
        draft = DistillDraft(
            plan=DraftPlan(
                title="Login",
                start_route="/login",
                steps=[
                    DraftStep(
                        action="enter email",
                        selectors=[
                            DraftSelector(
                                kind="css", value='By.id("login-email")', provenance="f.java#E"
                            )
                        ],
                        expected="field filled",
                        assert_hints=[
                            DraftSelector(
                                kind="css", value='By.id("login-error")', provenance="f.java#R"
                            )
                        ],
                        route="/login",
                    ),
                    DraftStep(action="submit"),
                ],
                notes="observation",
            ),
            kind="ui",
            routes=["/login"],
            unresolved=["Reporting.record(...)"],
        )
        output = draft_to_output(draft)
        first, second = output.plan.steps
        assert first.selector is not None and first.selector.value == 'By.id("login-email")'
        assert first.selector.verified is False  # verification is pipeline-owned
        assert first.assert_hint is not None and first.assert_hint.provenance == "f.java#R"
        assert second.selector is None and second.assert_hint is None
        assert output.plan.notes == "observation"
        assert output.unresolved == ["Reporting.record(...)"]

    def test_surplus_selectors_keep_first_and_are_surfaced(self) -> None:
        two = [
            DraftSelector(kind="css", value="a", provenance="f#1"),
            DraftSelector(kind="css", value="b", provenance="f#2"),
        ]
        draft = DistillDraft(
            plan=DraftPlan(title="t", steps=[DraftStep(action="click", selectors=two)])
        )
        output = draft_to_output(draft)
        step = output.plan.steps[0]
        assert step.selector is not None and step.selector.value == "a"
        assert "kept the first" in output.plan.notes  # not silently dropped


class TestTwoCallMode:
    def test_request_read_distill_and_revalidate(self, cfg: Config, corpus: Path) -> None:
        distill_messages: list[str] = []

        async def run_request(message: str) -> FileRequestList:
            assert "pages/LoginPage.java" in message  # the inventory was offered
            return FileRequestList(paths=["pages/LoginPage.java"])

        async def run_distill(message: str) -> DistillOutput:
            distill_messages.append(message)
            return _output(_step('By.id("login-email")', "pages/LoginPage.java#EMAIL"))

        tools = RepoTools([corpus])
        turns = TwoCallTurns(cfg, tools, run_request=run_request, run_distill=run_distill)
        output = asyncio.run(turns.first("distill this"))
        assert output.kind == "ui"
        # Call 2 carried the requested file's contents, read through the sandbox.
        assert 'By.id("login-email")' in distill_messages[0]
        assert tools.files_opened == {"pages/LoginPage.java"}

        asyncio.run(turns.revalidate("these failed"))
        assert "these failed" in distill_messages[1]
        assert 'By.id("login-email")' in distill_messages[1]  # same sources re-supplied
