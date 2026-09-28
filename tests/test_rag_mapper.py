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
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_test_gen.config import PROJECT_ROOT, Config
from ai_test_gen.rag.mapper import (
    PROMPTS_DIR,
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


# Cache corruptions: each takes a real, valid cache file and breaks it one way.
def _write_invalid_json(cache_path: Path) -> None:
    cache_path.write_text("{not json")


def _write_garbage_draft(cache_path: Path) -> None:
    # Valid JSON, right version — but "draft" no longer validates as a MapDraft.
    data = json.loads(cache_path.read_text())
    data["draft"] = "garbage"
    cache_path.write_text(json.dumps(data))


def _drop_draft_key(cache_path: Path) -> None:
    data = json.loads(cache_path.read_text())
    del data["draft"]
    cache_path.write_text(json.dumps(data))


def _garble_section_hashes(cache_path: Path) -> None:
    data = json.loads(cache_path.read_text())
    data["section_hashes"] = []
    data["corpus_files"] = 5
    cache_path.write_text(json.dumps(data))


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

    def test_added_file_invalidates_every_section(self, cfg: Config, corpus: Path) -> None:
        # A file appearing (or vanishing) changes the corpus STRUCTURE — the per-file
        # section hashes can't see it, so the whole map regenerates.
        stub = FakeMapper()
        _build(cfg, corpus, run_draft=stub)
        _write(corpus, "extra/NewHelper.java", "class NewHelper {}\n")
        second = _build(cfg, corpus, run_draft=stub)

        assert stub.calls == 2  # the Mapper ran again
        assert not second.from_cache
        assert set(second.stale_sections) == {
            "suites",
            "locator_idioms",
            "core_helpers",
            "lifecycle",
            "data",
            "conventions",
            "unmapped",
        }
        assert "clicks v2" in second.path.read_text()

    @pytest.mark.parametrize(
        "corrupt",
        [
            pytest.param(_write_invalid_json, id="invalid-json"),
            pytest.param(_write_garbage_draft, id="garbage-draft"),
            pytest.param(_drop_draft_key, id="missing-draft"),
            pytest.param(_garble_section_hashes, id="non-mapping-bookkeeping"),
        ],
    )
    def test_corrupted_cache_is_a_miss_not_a_crash(
        self, cfg: Config, corpus: Path, corrupt: Callable[[Path], None]
    ) -> None:
        # A truncated or hand-edited cache file must read as a cache MISS (regenerate)
        # — never abort a seeding run with a JSON/validation error.
        stub = FakeMapper()
        first = _build(cfg, corpus, run_draft=stub)
        cache_path = first.path.parent / "NOTE.suite_map.cache.json"
        assert cache_path.exists()
        corrupt(cache_path)

        result = _build(cfg, corpus, run_draft=stub)  # must not raise
        assert stub.calls == 2  # regenerated from the model, not from the bad cache
        assert not result.from_cache


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


class TestPromptContract:
    def test_mapper_prompt_demands_citations_and_honest_unmapped(self) -> None:
        # Locks the Mapper prompt contract offline (verbatim phrases, so a prompt rewrite
        # that drops either demand fails CI): every claim cites a real file, snippets are
        # copied not invented, and uncertainty goes to 'unmapped' instead of a guess.
        prompt = (PROMPTS_DIR / "mapper.md").read_text()
        assert "MUST cite a real file" in prompt
        assert "never paraphrase a selector, never invent one" in prompt
        assert "prefer flagging uncertainty here over guessing" in prompt
        assert 'A wrong citation is worse than an honest "unmapped"' in prompt


def test_mapper_uses_the_distill_request_deadline(cfg: Config, corpus: Path, monkeypatch) -> None:
    # A whole-corpus MapDraft turn is as long as a distill turn; the browser agents'
    # 180s default would cut a healthy Mapper turn off and pay for it twice on retry.
    from ai_test_gen.rag import mapper as mapper_mod
    from ai_test_gen.rag.distiller import _DISTILL_TIMEOUT_S

    seen: dict[str, float | None] = {}
    real = mapper_mod.build_openai_model

    def spy(config, model_name, **kwargs):
        seen["timeout_s"] = kwargs.get("timeout_s")
        return real(config, model_name, **kwargs)

    monkeypatch.setattr(mapper_mod, "build_openai_model", spy)
    mapper_mod.build_mapper(cfg, RepoTools([corpus]))
    assert seen["timeout_s"] == _DISTILL_TIMEOUT_S
