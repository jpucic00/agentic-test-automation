"""Seeding workflow tests — Distiller faked, embeddings faked, real local KB.

Runs the actual demo legacy suite through run_seeding() end-to-end offline:
dry-run review files, live upserts into Qdrant local mode (tmp dir), skip-on-
re-run resumability, --force re-distilling, --limit, and the xray-map fallback.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_test_gen.config import PROJECT_ROOT
from ai_test_gen.rag import seeding
from ai_test_gen.rag.distiller import DistilledCase
from ai_test_gen.rag.extract import TestBundle
from ai_test_gen.rag.models import KBSelector
from ai_test_gen.rag.seeding import run_seeding
from ai_test_gen.rag.store import KBStore

LEGACY = PROJECT_ROOT / "packages" / "demo-notes-app" / "legacy-suite"
CASES = PROJECT_ROOT / "packages" / "demo-notes-app" / "test-cases"


class FakeDistillerAgent:
    """Deterministic stand-in: distills every bundle without a model call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_sync(self, message: str) -> SimpleNamespace:
        self.calls.append(message)
        title = "Distilled: " + message.split("`", 2)[1] if "`" in message else "Distilled"
        return SimpleNamespace(
            output=DistilledCase(
                title=title,
                intent_text=f"{title}\nSteps: do the flow\nExpected: it works",
                steps=["Open the app", "Do the flow", "Assert the outcome"],
                selectors=[
                    KBSelector(kind="testid", value="getByTestId('login-submit')")
                ],
                routes=["/login"],
            )
        )


@pytest.fixture
def fake_agent(monkeypatch) -> FakeDistillerAgent:
    agent = FakeDistillerAgent()
    monkeypatch.setattr(seeding, "build_distiller", lambda config: agent)
    return agent


@pytest.fixture
def fake_embed(monkeypatch):
    calls: list[list[str]] = []

    def embed(config, texts):
        calls.append(list(texts))
        return [[1.0, float(len(text) % 5), 0.5] for text in texts]

    monkeypatch.setattr(seeding.embeddings, "embed", embed)
    return calls


def _run(cfg, **overrides):
    kwargs = dict(
        project="NOTE",
        selenium_root=LEGACY,
        playwright_dir=LEGACY / "playwright",
        cases=[str(CASES)],
    )
    kwargs.update(overrides)
    return run_seeding(cfg, **kwargs)


class TestDryRun:
    def test_reviews_written_kb_untouched_no_embeddings(
        self, cfg, fake_agent, fake_embed
    ) -> None:
        stats = _run(cfg, dry_run=True)

        assert stats.discovered == 3  # 2 Java tests + 1 Playwright spec
        assert stats.distilled == 3
        assert stats.upserted == 0
        assert fake_embed == []  # nothing embedded
        assert not cfg.kb_path.exists()  # store never even created
        review_files = sorted(p.name for p in stats.review_dir.glob("*.md"))
        assert "summary.md" in review_files
        assert len(review_files) == 4  # 3 records + summary

    def test_review_file_carries_the_quality_signals(self, cfg, fake_agent, fake_embed) -> None:
        stats = _run(cfg, dry_run=True)
        login_review = next(
            p for p in stats.review_dir.glob("*.md") if "NOTE-4" in p.name
        )
        text = login_review.read_text()
        assert "intent_text (what gets embedded)" in text
        assert "unresolved:ReportingClient.record" in text  # visible, never silent
        assert "```java" in text  # source excerpt

    def test_limit_caps_distillation(self, cfg, fake_agent, fake_embed) -> None:
        stats = _run(cfg, dry_run=True, limit=1)
        assert stats.distilled == 1


class TestLiveRunAndResume:
    def test_upserts_then_skips_on_rerun(self, cfg, fake_agent, fake_embed) -> None:
        first = _run(cfg)
        assert first.upserted == 3
        with KBStore(cfg.kb_path) as store:
            assert store.count("NOTE") == 3

        second = _run(cfg)
        assert second.skipped_existing == 3
        assert second.distilled == 0  # no LLM calls paid on resume
        with KBStore(cfg.kb_path) as store:
            assert store.count("NOTE") == 3  # idempotent — no duplicates

    def test_force_redistills_without_duplicating(self, cfg, fake_agent, fake_embed) -> None:
        _run(cfg)
        forced = _run(cfg, force=True)
        assert forced.distilled == 3
        with KBStore(cfg.kb_path) as store:
            assert store.count("NOTE") == 3

    def test_records_carry_provenance_and_payloads(self, cfg, fake_agent, fake_embed) -> None:
        _run(cfg)
        with KBStore(cfg.kb_path) as store:
            results = store.search("NOTE", [1.0, 1.0, 0.5], top_n=10)
        by_source = {}
        for record, _ in results:
            by_source.setdefault(record.source, []).append(record)
        assert len(by_source["selenium-import"]) == 2
        assert len(by_source["playwright-import"]) == 1
        selenium = by_source["selenium-import"][0]
        assert selenium.source_lang == "java"
        assert selenium.source_code  # original code retained
        assert selenium.spec == ""  # Selenium never becomes a Generator example
        playwright = by_source["playwright-import"][0]
        assert playwright.spec  # spec present → Generator-example eligible
        assert playwright.xray_key == "NOTE-3"


class TestXrayMapFallback:
    def test_map_links_unannotated_tests(self, tmp_path: Path) -> None:
        bundle = TestBundle(
            ref="specs/foo.spec.ts",
            test_name="foo",
            class_name="foo",
            language="ts",
            xray_key=None,
            code="// foo",
        )
        mapping = tmp_path / "map.json"
        mapping.write_text(json.dumps({"specs/foo.spec.ts": "NOTE-9"}))

        seeding._apply_xray_map([bundle], mapping)

        assert bundle.xray_key == "NOTE-9"

    def test_annotation_wins_over_map(self, tmp_path: Path) -> None:
        bundle = TestBundle(
            ref="a.java#t",
            test_name="t",
            class_name="A",
            language="java",
            xray_key="NOTE-1",
            code="// a",
        )
        mapping = tmp_path / "map.json"
        mapping.write_text(json.dumps({"a.java#t": "NOTE-2"}))

        seeding._apply_xray_map([bundle], mapping)

        assert bundle.xray_key == "NOTE-1"
