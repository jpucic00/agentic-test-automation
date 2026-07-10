"""Offline tests for the suite-map pass (rag/mapper.py).

The Mapper agent is replaced by a stub that (like a recorded transcript) reads a
file through the real RepoTools and returns a canned, versioned MapDraft. That lets
these tests pin the acceptance criteria without a gateway: a cited, sectioned map is
produced; §unmapped is present even when empty; the per-section cache re-refines only
the sections whose cited files changed; overrides survive regeneration; and the
lifecycle/conventions sections become kind=knowledge records.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_test_gen.config import PROJECT_ROOT, Config
from ai_test_gen.rag.mapper import (
    CitedNote,
    CodeExample,
    HelperSummary,
    LifecycleNote,
    LocatorIdiom,
    MapDraft,
    SuiteNote,
    build_suite_map,
)
from ai_test_gen.rag.models import make_record_id
from ai_test_gen.rag.tools import RepoTools


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A tiny Java suite where each map section cites a DISTINCT file (so an edit to
    one file makes exactly one section stale)."""
    root = tmp_path / "corpus"
    _write(
        root,
        "pages/LoginPage.java",
        'class LoginPage {\n  By EMAIL = By.id("login-email");\n}\n',
    )
    _write(root, "core/BasePage.java", "class BasePage {\n  void click(By b) {}\n}\n")
    _write(root, "core/Waits.java", "class Waits {\n  static void visible(By b) {}\n}\n")
    _write(
        root,
        "tests/LoginTest.java",
        'class LoginTest {\n  @Xray(testCase = "NOTE-1")\n  void login() {}\n}\n',
    )
    _write(root, "data/fixtures.sql", "INSERT INTO users VALUES ('demo@demo.test');\n")
    return root


class FakeMapper:
    """A stand-in for the Mapper agent: reads a file (transcript) + returns a draft.

    Section content embeds the call index so a regenerated section is distinguishable
    from a cache-preserved one. ``ghost`` makes core_helpers cite a non-existent file.
    """

    def __init__(self, *, ghost: bool = False) -> None:
        self.calls = 0
        self.ghost = ghost

    async def __call__(self, tools: RepoTools, _message: str) -> MapDraft:
        self.calls += 1
        n = self.calls
        tools.read_file("core/BasePage.java")  # simulate exploration → instrumentation
        helper_source = "core/Ghost.java#x" if self.ghost else "core/BasePage.java#click"
        return MapDraft(
            suites=[SuiteNote(path="core", role=f"shared base v{n}")],
            locator_idioms=[
                LocatorIdiom(
                    name="By.id constant",
                    how=f"ids v{n}",
                    examples=[
                        CodeExample(
                            code='By.id("login-email")',
                            source="pages/LoginPage.java#EMAIL",
                        )
                    ],
                )
            ],
            core_helpers=[
                HelperSummary(
                    symbol="BasePage.click(By)", summary=f"clicks v{n}", source=helper_source
                )
            ],
            lifecycle=LifecycleNote(
                summary=f"log in v{n}",
                login_steps=["open /login", "enter email + password", "submit"],
                sources=["tests/LoginTest.java"],
            ),
            data=[CitedNote(text=f"seeded demo user v{n}", source="data/fixtures.sql")],
            conventions=[
                CitedNote(text=f"wait for visible before click v{n}", source="core/Waits.java")
            ],
            unmapped=[],
        )


def _build(cfg: Config, corpus: Path, *, refresh: bool = False, run_draft=None):
    map_dir = cfg.output_dir / "maps"
    return asyncio.run(
        build_suite_map(
            cfg,
            "NOTE",
            selenium_root=corpus,
            map_dir=map_dir,
            refresh=refresh,
            run_draft=run_draft,
        )
    )


class TestGeneration:
    def test_produces_a_cited_sectioned_map(self, cfg: Config, corpus: Path) -> None:
        stub = FakeMapper()
        result = _build(cfg, corpus, run_draft=stub)

        assert not result.from_cache
        assert result.path.exists()
        md = result.path.read_text()
        for heading in (
            "## §0 At a glance",
            "## Locator idioms",
            "## Core helpers",
            "## Lifecycle & login",
            "## Conventions & gotchas",
            "## Unmapped / uncertain",
        ):
            assert heading in md
        # Every claim carries a path citation (the cited files appear in the map).
        assert "pages/LoginPage.java" in md
        assert "core/BasePage.java" in md
        # §unmapped is present even when the model flagged nothing.
        assert "(nothing flagged)" in md
        # The transcript ran through the real tools → honest instrumentation.
        assert result.tool_calls > 0
        assert "core/BasePage.java" in result.files_opened

    def test_index_is_present_and_bounded(self, cfg: Config, corpus: Path) -> None:
        result = _build(cfg, corpus, run_draft=FakeMapper())
        assert result.index
        assert len(result.index) <= 1200
        assert "NOTE" in result.index

    def test_knowledge_records_are_lifecycle_and_conventions(
        self, cfg: Config, corpus: Path
    ) -> None:
        result = _build(cfg, corpus, run_draft=FakeMapper())
        assert [r.kind for r in result.knowledge_records] == ["knowledge", "knowledge"]
        by_ref = {r.record_id: r for r in result.knowledge_records}
        life_id = make_record_id("NOTE", "selenium-import", "suite-map#lifecycle")
        conv_id = make_record_id("NOTE", "selenium-import", "suite-map#conventions")
        assert life_id in by_ref and conv_id in by_ref
        assert "log in" in by_ref[life_id].intent_text
        assert by_ref[life_id].source == "selenium-import"
        assert by_ref[life_id].manual_steps == []

    def test_knowledge_records_upsert_into_the_store_as_kind_knowledge(
        self, cfg: Config, corpus: Path, tmp_path: Path
    ) -> None:
        # The exact upsert path seed_kb uses (embeddings mocked out with fixed vectors):
        # the map's lifecycle/conventions records round-trip through Qdrant as kind=knowledge.
        from ai_test_gen.rag.store import KBStore

        result = _build(cfg, corpus, run_draft=FakeMapper())
        vectors = [[0.1, 0.2, 0.3, 0.4] for _ in result.knowledge_records]
        with KBStore(tmp_path / "kb") as store:
            store.upsert("NOTE", result.knowledge_records, vectors)
            assert store.count("NOTE") == 2
            hits = store.search("NOTE", [0.1, 0.2, 0.3, 0.4], 5)
        assert {record.kind for record, _ in hits} == {"knowledge"}

    def test_unresolved_citation_is_flagged_not_dropped(self, cfg: Config, corpus: Path) -> None:
        result = _build(cfg, corpus, run_draft=FakeMapper(ghost=True))
        assert "core/Ghost.java" in result.unresolved_citations
        md = result.path.read_text()
        assert "cited but not found" in md
        assert "Ghost.java" in md


class TestPerSectionCache:
    def test_unchanged_corpus_is_a_pure_cache_hit(self, cfg: Config, corpus: Path) -> None:
        stub = FakeMapper()
        first = _build(cfg, corpus, run_draft=stub)
        second = _build(cfg, corpus, run_draft=stub)
        assert stub.calls == 1  # the model was NOT called the second time
        assert second.from_cache
        assert second.path.read_text() == first.path.read_text()

    def test_only_the_section_whose_file_changed_re_refreshes(
        self, cfg: Config, corpus: Path
    ) -> None:
        stub = FakeMapper()
        _build(cfg, corpus, run_draft=stub)  # v1 for every section
        # Edit only the file core_helpers cites.
        (corpus / "core/BasePage.java").write_text(
            "class BasePage {\n  void click(By b) { /*x*/ }\n}\n"
        )
        second = _build(cfg, corpus, run_draft=stub)

        assert stub.calls == 2
        assert not second.from_cache
        assert second.stale_sections == ["core_helpers"]
        md = second.path.read_text()
        assert "clicks v2" in md  # the stale section took fresh content
        assert "ids v1" in md  # an unchanged section was byte-preserved from cache
        assert "log in v1" in md

    def test_refresh_map_regenerates_everything(self, cfg: Config, corpus: Path) -> None:
        stub = FakeMapper()
        _build(cfg, corpus, run_draft=stub)
        result = _build(cfg, corpus, refresh=True, run_draft=stub)
        assert stub.calls == 2
        assert not result.from_cache
        assert set(result.stale_sections) == {
            "suites",
            "locator_idioms",
            "core_helpers",
            "lifecycle",
            "data",
            "conventions",
            "unmapped",
        }
        assert "clicks v2" in result.path.read_text()


class TestOverrides:
    def test_overrides_are_merged_and_survive_regeneration(self, cfg: Config, corpus: Path) -> None:
        stub = FakeMapper()
        first = _build(cfg, corpus, run_draft=stub)
        overrides = first.path.parent / "NOTE.suite_map.overrides.md"
        overrides.write_text("## Conventions & gotchas\nAlways prefer ids over text selectors.\n")

        # A refresh regenerates every section; the human correction must persist.
        result = _build(cfg, corpus, refresh=True, run_draft=stub)
        md = result.path.read_text()
        assert "Always prefer ids over text selectors." in md
        assert "Human corrections" in md
        # And the correction rides into the conventions knowledge record.
        conv = next(r for r in result.knowledge_records if "conventions" in r.title)
        assert "Always prefer ids" in conv.intent_text


class DemoStub:
    """A mocked Mapper that reads a real demo file (transcript) and cites it."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, tools: RepoTools, _message: str) -> MapDraft:
        self.calls += 1
        files = tools.inventory()
        java = [f for f in files if f.endswith(".java")]
        src = java[0] if java else files[0]
        tools.read_file(src)  # a recorded read against the actual corpus
        return MapDraft(
            suites=[SuiteNote(path="notes-suite", role="the notes app suite")],
            locator_idioms=[
                LocatorIdiom(
                    name="By.id",
                    how="ids via a By constant",
                    examples=[CodeExample(code="By.id(...)", source=src)],
                )
            ],
            core_helpers=[HelperSummary(symbol="BasePage.click", summary="clicks", source=src)],
            lifecycle=LifecycleNote(
                summary="log in via /login", login_steps=["open /login", "submit"], sources=[src]
            ),
            data=[CitedNote(text="seeded demo user", source=src)],
            conventions=[CitedNote(text="every control carries an id", source=src)],
            unmapped=[],
        )


class TestDemoCorpus:
    """The acceptance criterion: a map is generated for the bundled demo corpus."""

    def test_map_generated_for_the_bundled_demo_corpus(self, cfg: Config, tmp_path: Path) -> None:
        demo = PROJECT_ROOT / "packages/demo-notes-app/legacy-suite"
        if not demo.exists():
            pytest.skip("bundled demo corpus not present")
        stub = DemoStub()
        result = asyncio.run(
            build_suite_map(
                cfg, "NOTE", selenium_root=demo, map_dir=tmp_path / "maps", run_draft=stub
            )
        )
        assert result.path.exists()
        md = result.path.read_text()
        for heading in ("## §0 At a glance", "## Locator idioms", "## Lifecycle & login"):
            assert heading in md
        # The demo's @Xray-annotated tests were discovered into the skeleton.
        assert "test(s) discovered" in md
        # Citations pointed at real corpus files, so nothing is flagged unresolved.
        assert result.unresolved_citations == []
        assert len(result.knowledge_records) == 2
