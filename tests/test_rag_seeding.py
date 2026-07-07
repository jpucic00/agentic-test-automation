"""Seeding workflow tests — Distiller faked, embeddings faked, real local KB.

Runs the actual demo legacy suite through run_seeding() end-to-end offline:
dry-run review files, live upserts into Qdrant local mode (tmp dir), skip-on-
re-run resumability, --force re-distilling, --limit, the xray-map fallback,
manual-case auto-fetch (local + live, per-key tolerant) and the deterministic
selector ground-truth enforcement.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_test_gen.config import PROJECT_ROOT
from ai_test_gen.models import ManualTestCase
from ai_test_gen.rag import seeding
from ai_test_gen.rag.distiller import DistilledCase
from ai_test_gen.rag.extract import TestBundle
from ai_test_gen.rag.models import KBSelector
from ai_test_gen.rag.seeding import run_seeding
from ai_test_gen.rag.store import KBStore

LEGACY = PROJECT_ROOT / "packages" / "demo-notes-app" / "legacy-suite"
CASES = PROJECT_ROOT / "packages" / "demo-notes-app" / "test-cases"


class FakeDistillerAgent:
    """Deterministic stand-in: distills every bundle without a model call.

    Emits one selector as a BARE inner value (the drift the live dry-run showed)
    and one INVENTED selector — enforcement must canonicalize the first and
    drop the second.
    """

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
                    KBSelector(kind="testid", value="login-submit", description="submit"),
                    KBSelector(kind="role", value="getByRole('button')", description="invented"),
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
    kwargs: dict[str, Any] = dict(
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


class TestManualCaseAutoFetch:
    """Without --cases, the keys named by the @Xray annotations load themselves."""

    def test_local_source_auto_loads_linked_cases(self, cfg, fake_agent, fake_embed) -> None:
        local_cfg = dataclasses.replace(
            cfg, testcase_source="local", local_testcase_dir=CASES
        )
        stats = _run(local_cfg, cases=[], dry_run=True)

        assert stats.cases_loaded >= 2  # NOTE-2, NOTE-4 (+ NOTE-3 for the spec)
        assert stats.case_misses == {}
        assert any("Linked manual test case NOTE-4" in m for m in fake_agent.calls)

    def test_live_fetch_is_per_key_tolerant(self, cfg, fake_agent, fake_embed, monkeypatch) -> None:
        import ai_test_gen.xray_client as xray_client_module

        class StubClient:
            def __init__(self, config) -> None:
                pass

            def fetch(self, key: str) -> ManualTestCase:
                if key == "NOTE-4":
                    raise RuntimeError("410 gone")
                return ManualTestCase(
                    key=key, title=f"Case {key}", steps=["s1"], expected_results=["e1"]
                )

        monkeypatch.setattr(xray_client_module, "XrayClient", StubClient)
        stats = _run(cfg, cases=[], dry_run=True)  # cfg: xray source, Jira configured

        assert stats.cases_loaded >= 1  # the healthy keys still loaded
        assert "NOTE-4" in stats.case_misses
        assert "410 gone" in stats.case_misses["NOTE-4"]

    def test_no_source_reports_every_linked_key(self, cfg, fake_agent, fake_embed) -> None:
        no_jira = dataclasses.replace(cfg, jira_base_url=None)
        stats = _run(no_jira, cases=[], dry_run=True)

        assert stats.cases_loaded == 0
        assert {"NOTE-2", "NOTE-4"} <= set(stats.case_misses)
        review = next(p for p in stats.review_dir.glob("*.md") if "NOTE-4" in p.name)
        assert "NOT LOADED" in review.read_text()  # visible per record, never silent

    def test_no_fetch_skips_case_loading(self, cfg, fake_agent, fake_embed) -> None:
        stats = _run(cfg, cases=[], dry_run=True, no_fetch=True)
        assert stats.cases_loaded == 0
        assert stats.case_misses == {}

    def test_manual_steps_stored_verbatim_on_the_record(self, cfg, fake_agent, fake_embed) -> None:
        _run(cfg)  # --cases <dir> mode, live upsert
        with KBStore(cfg.kb_path) as store:
            results = store.search("NOTE", [1.0, 1.0, 0.5], top_n=10)
        linked = [r for r, _ in results if r.xray_key == "NOTE-2" and r.source_lang == "java"]
        assert linked and linked[0].manual_steps  # the ticket's own steps, verbatim
        review = next(
            p
            for p in (cfg.output_dir / "kb_review" / "NOTE").glob("*.md")
            if "NOTE-2" in p.name and "seeded" in p.name
        )
        assert "Steps (verbatim):" in review.read_text()


class TestGroundTruthEnforcement:
    def test_bare_values_canonicalize_inventions_drop(self, cfg, fake_agent, fake_embed) -> None:
        _run(cfg)
        with KBStore(cfg.kb_path) as store:
            results = store.search("NOTE", [1.0, 1.0, 0.5], top_n=10)
        java_record = next(
            r for r, _ in results if r.xray_key == "NOTE-4" and r.source_lang == "java"
        )
        values = {s.value for s in java_record.selectors}
        assert 'By.id("login-submit")' in values  # bare "login-submit" canonicalized
        assert "getByRole('button')" not in values  # invention dropped
        assert 'By.id("login-email")' in values  # extraction appended even if model omits

    def test_dropped_inventions_are_reported(self, cfg, fake_agent, fake_embed) -> None:
        stats = _run(cfg, dry_run=True)
        assert stats.dropped_selectors >= 1
        review = next(p for p in stats.review_dir.glob("*.md") if "NOTE-4" in p.name)
        assert "Model selectors DROPPED" in review.read_text()


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
