"""Offline tests for the seeding orchestration (rag/seeding.py).

The Mapper and Distiller are canned (recorded-transcript style: they read real
files through the real ``RepoTools``); embeddings are fixed vectors; the store is
real Qdrant local mode in a tmp dir. Pins the acceptance criteria: plan-shaped
records upserted; a planted wrong-file citation auto-fixed and a planted fake
citation flagged ``verified=False``; review files with triplets + ✓/⚠ +
instrumentation; complete summary counters; per-test failure containment;
``--dry-run`` embeds nothing; resume/force/limit; the bundled demo corpus's
``.properties`` locator recovered through a citation no static parser could see.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ai_test_gen.config import PROJECT_ROOT, Config
from ai_test_gen.models import ManualStep
from ai_test_gen.rag.discover import DiscoveredTest
from ai_test_gen.rag.distiller import DistillOutput
from ai_test_gen.rag.mapper import CitedNote, LifecycleNote, MapDraft, SuiteNote
from ai_test_gen.rag.models import (
    KBRecord,
    ReconstructedPlan,
    ReconstructedSelector,
    ReconstructedStep,
    build_intent_text,
    make_record_id,
)
from ai_test_gen.rag.seeding import SeedStats, run_seeding
from ai_test_gen.rag.store import KBStore
from ai_test_gen.rag.tools import RepoTools

VEC = [0.1, 0.2, 0.3, 0.4]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Two marker-annotated Java tests + a page object + a .properties locator file."""
    root = tmp_path / "corpus"
    _write(
        root,
        "suite/pages/LoginPage.java",
        "class LoginPage {\n"
        '  public static final By EMAIL = By.id("login-email");\n'
        '  public static final By SUBMIT = By.id("login-submit");\n'
        "}\n",
    )
    _write(
        root,
        "suite/res/locators.properties",
        "delete.confirm=//div[contains(@class,'modal')]//div[normalize-space()='Delete']\n",
    )
    _write(
        root,
        "suite/tests/LoginTest.java",
        "class LoginTest {\n"
        '  @Xray(testCase = "TC-1")\n'
        "  public void loginWorks() { new LoginPage(driver).open(baseUrl); }\n"
        "}\n",
    )
    _write(
        root,
        "suite/tests/DeleteTest.java",
        "class DeleteTest {\n"
        '  @Xray(testCase = "TC-2")\n'
        "  public void deleteWorks() { flows.remove(); }\n"
        "}\n",
    )
    return root


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    """Raw-Xray-shaped JSON for TC-1 only — TC-2 is a deliberate case miss."""
    directory = tmp_path / "cases"
    directory.mkdir()
    (directory / "TC-1.json").write_text(
        json.dumps(
            {
                "key": "TC-1",
                "fields": {"summary": "Login works", "description": "", "labels": []},
                "steps": [
                    {
                        "step": {"raw": "Log in"},
                        "data": {"raw": "demo / pw"},
                        "result": {"raw": "Notes list shown"},
                    }
                ],
            }
        )
    )
    return directory


class StubMapper:
    """Recorded-transcript Mapper: reads a real file, returns a minimal cited draft."""

    async def __call__(self, tools: RepoTools, _message: str) -> MapDraft:
        files = tools.inventory()
        src = next((f for f in files if f.endswith(".java")), files[0])
        tools.read_file(src)
        return MapDraft(
            suites=[SuiteNote(path=src.split("/", 1)[0], role="the suite")],
            lifecycle=LifecycleNote(
                summary="log in via /login", login_steps=["open /login"], sources=[src]
            ),
            conventions=[CitedNote(text="use ids", source=src)],
            unmapped=[],
        )


class FakeTurns:
    """Canned distiller turns; successive outputs serve first() then revalidate()."""

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


def _step(action: str, value: str | None = None, provenance: str = "") -> ReconstructedStep:
    selector = (
        ReconstructedSelector(kind="css", value=value, provenance=provenance)
        if value is not None
        else None
    )
    return ReconstructedStep(action=action, selector=selector)


def _output(*steps: ReconstructedStep, kind: str = "ui") -> DistillOutput:
    return DistillOutput(
        plan=ReconstructedPlan(title="Reconstructed flow", steps=list(steps)),
        kind=kind,  # type: ignore[arg-type]
        routes=["/login"],
    )


def _default_outputs() -> dict[str, list[DistillOutput]]:
    """TC-1: one good + one wrong-file citation (auto-fix). TC-2: a fake citation
    that survives its one bounce → flagged."""
    tc1 = _output(
        _step("enter email", 'By.id("login-email")', "suite/pages/LoginPage.java#EMAIL"),
        _step(
            "confirm delete",
            "//div[contains(@class,'modal')]//div[normalize-space()='Delete']",
            "suite/pages/LoginPage.java#CONFIRM",  # wrong file — value lives in .properties
        ),
    )
    tc2_bad = _output(_step("click ghost", 'By.id("ghost-button")', "suite/pages/LoginPage.java"))
    return {"TC-1": [tc1], "TC-2": [tc2_bad, tc2_bad.model_copy(deep=True)]}


def _factory(outputs: dict[str, list[DistillOutput]], created: dict[str, FakeTurns] | None = None):
    def factory(config: Config, tools: RepoTools, test: DiscoveredTest) -> FakeTurns:
        tools.read_file("suite/pages/LoginPage.java")  # simulated exploration
        turns = FakeTurns(*[o.model_copy(deep=True) for o in outputs[test.xray_key]])
        if created is not None:
            created[test.xray_key] = turns
        return turns

    return factory


def _run(
    cfg: Config,
    corpus: Path,
    *,
    cases_dir: Path | None = None,
    outputs: dict[str, list[DistillOutput]] | None = None,
    created: dict[str, FakeTurns] | None = None,
    embed_calls: list[list[str]] | None = None,
    turns_factory=None,
    **kwargs,
) -> SeedStats:
    calls = embed_calls if embed_calls is not None else []

    def fake_embed(config: Config, texts) -> list[list[float]]:
        calls.append(list(texts))
        return [VEC for _ in texts]

    return asyncio.run(
        run_seeding(
            cfg,
            project="TC",
            selenium_root=corpus,
            cases=[str(cases_dir)] if cases_dir else [],
            no_fetch=cases_dir is None,  # never fall through to a live Jira fetch in tests
            run_draft=StubMapper(),
            turns_factory=turns_factory or _factory(outputs or _default_outputs(), created),
            embed=fake_embed,
            **kwargs,
        )
    )


def _records(cfg: Config) -> dict[str, KBRecord]:
    with KBStore(cfg.kb_path) as store:
        hits = store.search("TC", VEC, 10)
    return {record.record_id: record for record, _ in hits}


class TestSeedingE2E:
    def test_plan_shaped_records_are_upserted(self, cfg: Config, corpus: Path, cases_dir: Path):
        created: dict[str, FakeTurns] = {}
        stats = _run(cfg, corpus, cases_dir=cases_dir, created=created)
        assert stats.distilled == 2
        assert stats.upserted == 2
        assert stats.knowledge_upserted == 2
        by_id = _records(cfg)
        assert len(by_id) == 4  # 2 distilled + 2 core-knowledge

        record = by_id[make_record_id("TC", "selenium-import", "TC-1")]
        assert record.kind == "ui"
        assert record.source == "selenium-import"
        assert record.source_lang == "java"
        assert record.spec == ""  # Selenium is knowledge, never a Generator example
        assert record.routes == ["/login"]
        assert len(record.plan.steps) == 2
        # intent_text is code-built from the manual case — never model-authored.
        assert record.intent_text == build_intent_text(
            "Login works",
            [ManualStep(action="Log in", data="demo / pw", expected="Notes list shown")],
        )
        assert record.title == "Login works"
        # The manual snapshot keeps the data cell.
        assert record.manual_steps[0].data == "demo / pw"
        # Honest instrumentation from the per-test tools.
        assert record.explored.files_opened == ["suite/pages/LoginPage.java"]
        assert record.explored.selectors_cited == 2

    def test_wrong_file_citation_auto_fixed_and_fake_citation_flagged(
        self, cfg: Config, corpus: Path, cases_dir: Path
    ):
        created: dict[str, FakeTurns] = {}
        stats = _run(cfg, corpus, cases_dir=cases_dir, created=created)
        by_id = _records(cfg)

        fixed = by_id[make_record_id("TC", "selenium-import", "TC-1")].plan.steps[1].selector
        assert fixed is not None
        assert fixed.provenance == "suite/res/locators.properties"  # auto-fixed
        assert fixed.verified

        flagged = by_id[make_record_id("TC", "selenium-import", "TC-2")].plan.steps[0].selector
        assert flagged is not None
        assert not flagged.verified  # flagged, kept
        assert created["TC-2"].revalidate_messages  # the one bounce round ran
        assert len(created["TC-2"].revalidate_messages) == 1
        assert created["TC-1"].revalidate_messages == []  # auto-fix is not a bounce

        assert stats.citations_auto_fixed == 1
        assert stats.records_bounced == 1
        assert stats.claims_bounced == 1
        assert stats.claims_cited == 3
        assert stats.claims_verified == 2
        assert stats.claims_unverified == 1

    def test_distill_message_carries_map_index_and_suite_block(
        self, cfg: Config, corpus: Path, cases_dir: Path
    ):
        created: dict[str, FakeTurns] = {}
        _run(cfg, corpus, cases_dir=cases_dir, created=created)
        message = created["TC-1"].first_messages[0]
        assert "**TC**" in message  # map §0 index
        assert "- `suite` — the suite" in message  # the test's own suite block
        assert "- Data: demo / pw" in message  # manual triplets with the data cell


class TestReviewArtifacts:
    def test_review_files_show_triplets_marks_and_instrumentation(
        self, cfg: Config, corpus: Path, cases_dir: Path
    ):
        stats = _run(cfg, corpus, cases_dir=cases_dir)
        assert stats.review_dir is not None
        good = (stats.review_dir / "tc-1-logintest-loginworks.md").read_text()
        assert "- Data: demo / pw" in good
        assert "- Expected: Notes list shown" in good
        assert "✓" in good
        assert "auto-fixed: 1" in good
        assert "files opened (1): suite/pages/LoginPage.java" in good
        assert "```java" in good

        bad = (stats.review_dir / "tc-2-deletetest-deleteworks.md").read_text()
        assert "⚠" in bad and "UNVERIFIED" in bad
        assert "NOT LOADED" in bad  # the case miss is visible where it matters
        assert "1 claim(s) sent to one revalidation round" in bad

    def test_summary_counters_are_complete(self, cfg: Config, corpus: Path, cases_dir: Path):
        stats = _run(cfg, corpus, cases_dir=cases_dir)
        assert stats.review_dir is not None
        summary = (stats.review_dir / "summary.md").read_text()
        assert "markers seen (Java): 2" in summary  # discovery parity
        assert "- distilled: 2" in summary
        assert "claims cited: 3 · verified: 2 · unverified: 1 (rate: 33%)" in summary
        assert "citations auto-fixed: 1" in summary
        assert "1 record(s) bounced (1 claim(s) sent)" in summary
        assert "selectorless ui-records: 0" in summary
        assert "TC-2: no TC-2.json" in summary  # case miss with its reason
        assert "- suite: 2" in summary  # per-suite counts

    def test_escalation_signals_reach_the_summary(self, cfg: Config, corpus: Path):
        outputs = {
            "TC-1": [_output(_step("do a thing"))],  # ui + no selectors → selectorless
            "TC-2": [_output(_step("do a thing"))],
        }

        def factory(config: Config, tools: RepoTools, test: DiscoveredTest) -> FakeTurns:
            return FakeTurns(*outputs[test.xray_key])  # no reads → no-files-opened

        stats = _run(cfg, corpus, turns_factory=factory)
        assert set(stats.selectorless_ui) == {
            "suite/tests/LoginTest.java#loginWorks",
            "suite/tests/DeleteTest.java#deleteWorks",
        }
        assert stats.review_dir is not None
        summary = (stats.review_dir / "summary.md").read_text()
        assert "selectorless-ui" in summary
        assert "no-files-opened" in summary


class TestFaultContainment:
    def test_a_distill_failure_skips_that_record_only(
        self, cfg: Config, corpus: Path, cases_dir: Path
    ):
        outputs = _default_outputs()

        def factory(config: Config, tools: RepoTools, test: DiscoveredTest) -> FakeTurns:
            if test.xray_key == "TC-2":
                raise RuntimeError("model exploded")
            return FakeTurns(*outputs[test.xray_key])

        stats = _run(cfg, corpus, cases_dir=cases_dir, turns_factory=factory)
        assert stats.distilled == 1
        assert stats.upserted == 1
        assert list(stats.failed) == ["suite/tests/DeleteTest.java#deleteWorks"]
        assert "model exploded" in stats.failed["suite/tests/DeleteTest.java#deleteWorks"]
        assert stats.retried == 1 and stats.recovered == 0  # the one retry also failed
        assert stats.review_dir is not None
        assert "model exploded" in (stats.review_dir / "summary.md").read_text()

    def test_transient_failure_recovers_on_the_single_retry(
        self, cfg: Config, corpus: Path, cases_dir: Path
    ):
        outputs = _default_outputs()
        attempts: dict[str, int] = {}

        def factory(config: Config, tools: RepoTools, test: DiscoveredTest) -> FakeTurns:
            attempts[test.xray_key] = attempts.get(test.xray_key, 0) + 1
            if test.xray_key == "TC-2" and attempts["TC-2"] == 1:
                raise RuntimeError("provider hiccup")  # transient — gone on the retry
            tools.read_file("suite/pages/LoginPage.java")
            return FakeTurns(*[o.model_copy(deep=True) for o in outputs[test.xray_key]])

        stats = _run(cfg, corpus, cases_dir=cases_dir, turns_factory=factory)
        assert attempts["TC-2"] == 2
        assert stats.retried == 1 and stats.recovered == 1
        assert stats.failed == {}
        assert stats.distilled == 2
        assert stats.upserted == 2

    def test_dry_run_embeds_and_stores_nothing(self, cfg: Config, corpus: Path, cases_dir: Path):
        embed_calls: list[list[str]] = []
        stats = _run(cfg, corpus, cases_dir=cases_dir, embed_calls=embed_calls, dry_run=True)
        assert embed_calls == []
        assert stats.upserted == 0 and stats.knowledge_upserted == 0
        assert not cfg.kb_path.exists()  # the store was never even opened
        assert stats.distilled == 2  # …but the review loop ran in full
        assert stats.review_dir is not None
        assert (stats.review_dir / "summary.md").exists()


class TestResumeForceLimit:
    def test_resume_skips_already_stored_without_model_calls(
        self, cfg: Config, corpus: Path, cases_dir: Path
    ):
        _run(cfg, corpus, cases_dir=cases_dir)
        calls: list[str] = []

        def counting_factory(config: Config, tools: RepoTools, test: DiscoveredTest):
            calls.append(test.ref)
            raise AssertionError("resume must not distill stored records")

        stats = _run(cfg, corpus, cases_dir=cases_dir, turns_factory=counting_factory)
        assert calls == []
        assert stats.skipped_existing == 2
        assert stats.distilled == 0

    def test_force_redistills_stored_records(self, cfg: Config, corpus: Path, cases_dir: Path):
        _run(cfg, corpus, cases_dir=cases_dir)
        stats = _run(cfg, corpus, cases_dir=cases_dir, force=True)
        assert stats.skipped_existing == 0
        assert stats.distilled == 2

    def test_limit_bounds_the_distill_count(self, cfg: Config, corpus: Path, cases_dir: Path):
        stats = _run(cfg, corpus, cases_dir=cases_dir, limit=1)
        assert stats.planned == 1
        assert stats.distilled == 1

    def test_map_only_stops_before_distillation(self, cfg: Config, corpus: Path):
        def untouchable(config: Config, tools: RepoTools, test: DiscoveredTest):
            raise AssertionError("map-only must not distill")

        stats = _run(cfg, corpus, turns_factory=untouchable, map_only=True)
        assert stats.distilled == 0
        assert stats.knowledge_upserted == 2

    def test_workers_parallel_smoke(self, cfg: Config, corpus: Path, cases_dir: Path):
        stats = _run(cfg, corpus, cases_dir=cases_dir, workers=4)
        assert stats.distilled == 2
        assert stats.upserted == 2


class TestNoFetch:
    def test_intent_text_falls_back_to_the_plan_via_the_same_builder(
        self, cfg: Config, corpus: Path
    ):
        stats = _run(cfg, corpus)  # no cases dir → no_fetch
        assert stats.cases_loaded == 0
        by_id = _records(cfg)
        record = by_id[make_record_id("TC", "selenium-import", "TC-1")]
        assert record.manual_steps == []
        expected = build_intent_text(
            "Reconstructed flow",
            [
                ManualStep(action="enter email"),
                ManualStep(action="confirm delete"),
            ],
        )
        assert record.intent_text == expected


class TestDemoCorpus:
    """Acceptance shape: the bundled demo's .properties locator — invisible to any
    static read of the page objects — is recovered through a cited, string-verified
    claim (recorded-transcript Distiller against the real corpus files)."""

    def test_properties_locator_is_recovered_and_verified(self, cfg: Config):
        demo = PROJECT_ROOT / "packages/demo-notes-app/legacy-suite"
        cases = PROJECT_ROOT / "packages/demo-notes-app/test-cases"
        if not demo.exists():
            pytest.skip("bundled demo corpus not present")
        registry = "core/main/resources/locators.properties"

        def factory(config: Config, tools: RepoTools, test: DiscoveredTest) -> FakeTurns:
            if test.xray_key == "NOTE-5":
                text = tools.read_file(registry)  # the recorded exploration
                line = next(
                    line
                    for line in text.splitlines()
                    if line.startswith("notes.delete.confirm=")
                )
                value = line.split("=", 1)[1]
                citation = f"{registry}#notes.delete.confirm"
                output = _output(_step("confirm deletion in the dialog", value, citation))
            else:
                tools.read_file(test.path)
                output = _output(
                    _step("log in", 'By.id("login-email")', test.path)
                )
            return FakeTurns(output)

        def fake_embed(config: Config, texts) -> list[list[float]]:
            return [VEC for _ in texts]

        stats = asyncio.run(
            run_seeding(
                cfg,
                project="NOTE",
                selenium_root=demo,
                cases=[str(cases)],
                run_draft=StubMapper(),
                turns_factory=factory,
                embed=fake_embed,
            )
        )
        assert stats.distilled == 3  # NOTE-2, NOTE-4, NOTE-5 (java)
        with KBStore(cfg.kb_path) as store:
            hits = store.search("NOTE", VEC, 10)
        by_id = {record.record_id: record for record, _ in hits}
        record = by_id[make_record_id("NOTE", "selenium-import", "NOTE-5")]
        selector = record.plan.steps[0].selector
        assert selector is not None
        assert selector.verified  # string-verified at the cited registry file
        assert selector.provenance.startswith(registry)
        assert "modal" in selector.value  # the real xpath from locators.properties
        # The NOTE-5 manual case loaded from the bundled test-cases dir.
        assert record.title == "Delete a note"
        assert record.manual_steps[0].data == "demo@demo.test / Passw0rd!"
