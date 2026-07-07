"""Offline tests for the retriever — store/embed/rerank all mocked.

Covers the acceptance criteria of the retriever task: rerank order beats vector
order, the score threshold and top-K cap, the provenance rules (selenium-import
never reaches generator_examples; pipeline supersedes its legacy twin), strict
per-project scoping, size caps, and fail-open on every failure point.
"""
from __future__ import annotations

import logging

import pytest

from ai_test_gen.models import ManualTestCase
from ai_test_gen.rag import retriever
from ai_test_gen.rag.models import KBRecord, KBSelector, make_record_id
from ai_test_gen.rag.retriever import RetrievedContext, retrieve


def _case(key: str = "QA-77") -> ManualTestCase:
    return ManualTestCase(
        key=key,
        title="Create a user",
        steps=["Log in as admin", "Open user management", "Create the user"],
        expected_results=["The new user appears in the list"],
    )


def _record(
    ref: str,
    title: str,
    *,
    source: str = "selenium-import",
    spec: str = "",
    xray_key: str | None = None,
    project_key: str = "QA",
) -> KBRecord:
    return KBRecord(
        record_id=make_record_id(project_key, source, ref),  # type: ignore[arg-type]
        project_key=project_key,
        xray_key=ref if xray_key is None else xray_key,
        title=title,
        intent_text=f"{title}. Steps and outcomes.",
        steps=["Log in", "Do the flow", "Assert the result"],
        selectors=[
            KBSelector(kind="testid", value="getByTestId('save')", description="save button")
        ],
        routes=["/admin"],
        spec=spec,
        outcome="legacy" if source != "pipeline" else "green",
        source=source,  # type: ignore[arg-type]
    )


class FakeStore:
    """Search returns pre-canned (record, vector_score) pairs; records project isolation."""

    def __init__(self, results: list[tuple[KBRecord, float]]) -> None:
        self._results = results
        self.searched_projects: list[str] = []
        self.closed = False

    def search(self, project_key: str, vector, top_n: int):
        self.searched_projects.append(project_key)
        return self._results[:top_n]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def no_embed(monkeypatch):
    """embed() returns a fixed vector without HTTP."""
    monkeypatch.setattr(retriever.embeddings, "embed", lambda config, texts: [[1.0, 0.0]])


@pytest.fixture
def rerank_calls(monkeypatch):
    """Patch rerank via monkeypatch; test sets .ranked before calling retrieve."""
    holder: dict = {"ranked": []}

    def fake_rerank(config, query, documents, top_n):
        holder["query"] = query
        holder["documents"] = list(documents)
        return holder["ranked"][:top_n]

    monkeypatch.setattr(retriever.embeddings, "rerank", fake_rerank)
    return holder


class TestRetrieveRanking:
    def test_rerank_order_beats_vector_order(self, cfg, no_embed, rerank_calls) -> None:
        first_by_vector = _record("QA-1", "Vector favourite")
        second_by_vector = _record("QA-2", "Rerank favourite")
        store = FakeStore([(first_by_vector, 0.99), (second_by_vector, 0.80)])
        rerank_calls["ranked"] = [(1, 0.95), (0, 0.60)]  # reranker flips the order

        context = retrieve(cfg, _case(), store=store)

        assert context.retrieved[0].startswith("QA-2 · Rerank favourite")
        assert "Rerank favourite" in context.planner_hints.split("Vector favourite")[0]

    def test_threshold_drops_weak_matches_and_top_k_caps(
        self, cfg, no_embed, rerank_calls
    ) -> None:
        records = [(_record(f"QA-{i}", f"Case {i}"), 0.9) for i in range(6)]
        store = FakeStore(records)
        rerank_calls["ranked"] = [
            (0, 0.90),
            (1, 0.80),
            (2, 0.70),
            (3, 0.60),  # would fit top_k... but capped at 3
            (4, 0.10),  # below threshold
            (5, 0.05),
        ]

        context = retrieve(cfg, _case(), store=store, top_k=3, min_score=0.30)

        assert len(context.retrieved) == 3
        assert all("Case" in line for line in context.retrieved)

    def test_all_below_threshold_yields_empty_context(
        self, cfg, no_embed, rerank_calls
    ) -> None:
        store = FakeStore([(_record("QA-1", "Weak"), 0.9)])
        rerank_calls["ranked"] = [(0, 0.01)]

        context = retrieve(cfg, _case(), store=store)

        assert context.is_empty
        assert context.retrieved == []

    def test_searches_only_the_cases_project(self, cfg, no_embed, rerank_calls) -> None:
        store = FakeStore([])
        rerank_calls["ranked"] = []

        retrieve(cfg, _case("NOTE-2"), store=store)

        assert store.searched_projects == ["NOTE"]


class TestProvenanceRules:
    def test_selenium_feeds_hints_but_never_generator_examples(
        self, cfg, no_embed, rerank_calls
    ) -> None:
        selenium = _record("QA-1", "Selenium knowledge", source="selenium-import")
        store = FakeStore([(selenium, 0.9)])
        rerank_calls["ranked"] = [(0, 0.9)]

        context = retrieve(cfg, _case(), store=store)

        assert "Selenium knowledge" in context.planner_hints
        assert context.generator_examples == ""

    def test_playwright_sources_become_examples(self, cfg, no_embed, rerank_calls) -> None:
        spec = "import { test } from '@playwright/test';"
        imported = _record("QA-1", "Hand-written", source="playwright-import", spec=spec)
        solved = _record("QA-2", "Pipeline solved", source="pipeline", spec=spec)
        third = _record("QA-3", "Also solved", source="pipeline", spec=spec)
        store = FakeStore([(imported, 0.9), (solved, 0.8), (third, 0.7)])
        rerank_calls["ranked"] = [(0, 0.9), (1, 0.8), (2, 0.7)]

        context = retrieve(cfg, _case(), store=store)

        assert context.generator_examples.count("```typescript") == 2  # ≤2 examples
        assert "Hand-written" in context.generator_examples
        assert "Pipeline solved" in context.generator_examples

    def test_pipeline_record_supersedes_its_legacy_twin(
        self, cfg, no_embed, rerank_calls
    ) -> None:
        legacy = _record("QA-9", "Legacy twin", source="selenium-import")
        solved = _record("QA-9", "Solved twin", source="pipeline", spec="spec code")
        store = FakeStore([(legacy, 0.95), (solved, 0.90)])
        # After supersede only the pipeline record remains → rerank sees ONE document.
        rerank_calls["ranked"] = [(0, 0.9)]

        context = retrieve(cfg, _case(), store=store)

        assert rerank_calls["documents"] == [solved.intent_text]
        assert "Legacy twin" not in context.planner_hints
        assert "Solved twin" in context.planner_hints


class TestRenderingCaps:
    def test_planner_hints_respect_word_budget_and_hint_framing(
        self, cfg, no_embed, rerank_calls
    ) -> None:
        records = [
            (
                _record(
                    f"QA-{i}",
                    "A rather long verbose title for budget testing " + "word " * 20,
                ),
                0.9,
            )
            for i in range(3)
        ]
        store = FakeStore(records)
        rerank_calls["ranked"] = [(0, 0.9), (1, 0.8), (2, 0.7)]

        context = retrieve(cfg, _case(), store=store)

        assert "HINTS ONLY" in context.planner_hints
        assert "verify every selector live" in context.planner_hints
        # budget: header + first block always fit; total stays near the cap
        assert len(context.planner_hints.split()) <= retriever.PLANNER_HINT_WORD_BUDGET + 60

    def test_empty_collection_short_circuits_before_rerank(
        self, cfg, no_embed, monkeypatch
    ) -> None:
        def exploding_rerank(*args, **kwargs):
            raise AssertionError("rerank must not be called for an empty candidate set")

        monkeypatch.setattr(retriever.embeddings, "rerank", exploding_rerank)

        context = retrieve(cfg, _case(), store=FakeStore([]))

        assert context.is_empty


class TestFailOpen:
    def test_store_failure_is_swallowed_with_warning(
        self, cfg, no_embed, caplog
    ) -> None:
        class BrokenStore:
            def search(self, *args):
                raise RuntimeError("storage locked")

            def close(self) -> None:  # pragma: no cover - not reached
                pass

        with caplog.at_level(logging.WARNING):
            context = retrieve(cfg, _case(), store=BrokenStore())

        assert context.is_empty
        assert "continuing unassisted" in caplog.text

    def test_embed_failure_is_swallowed(self, cfg, monkeypatch, caplog) -> None:
        def broken_embed(config, texts):
            raise RuntimeError("embeddings down")

        monkeypatch.setattr(retriever.embeddings, "embed", broken_embed)
        with caplog.at_level(logging.WARNING):
            context = retrieve(cfg, _case(), store=FakeStore([]))

        assert context.is_empty
        assert "embeddings down" in caplog.text

    def test_rerank_failure_is_swallowed(self, cfg, no_embed, monkeypatch, caplog) -> None:
        monkeypatch.setattr(
            retriever.embeddings,
            "rerank",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rerank down")),
        )
        with caplog.at_level(logging.WARNING):
            context = retrieve(cfg, _case(), store=FakeStore([(_record("QA-1", "X"), 0.9)]))

        assert context.is_empty

    def test_retrieve_never_raises_even_on_weird_payloads(
        self, cfg, no_embed, rerank_calls
    ) -> None:
        # A record with an out-of-range rerank index would raise inside — fail-open catches it.
        store = FakeStore([(_record("QA-1", "X"), 0.9)])
        rerank_calls["ranked"] = [(7, 0.9)]  # index out of range → IndexError inside

        context = retrieve(cfg, _case(), store=store)

        assert isinstance(context, RetrievedContext)
        assert context.is_empty
