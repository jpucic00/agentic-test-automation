"""Offline tests for the embedded KB store — Qdrant local mode in a tmp dir.

No network, no server: local mode runs in-process against ``tmp_path``. The
subprocess test at the bottom guards the import discipline — the default
pipeline (RAG off) must never pay the qdrant import.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ai_test_gen.models import ManualStep
from ai_test_gen.rag.models import (
    KBRecord,
    ReconstructedPlan,
    ReconstructedSelector,
    ReconstructedStep,
    make_record_id,
)
from ai_test_gen.rag.store import KBStore, collection_name


def _record(
    project_key: str = "QA",
    ref: str = "QA-1",
    title: str = "Login works",
    source: str = "pipeline",
    **overrides: object,
) -> KBRecord:
    fields: dict = dict(
        record_id=make_record_id(project_key, source, ref),  # type: ignore[arg-type]
        project_key=project_key,
        xray_key=ref if ref.startswith(project_key) else "",
        title=title,
        intent_text=f"{title}. The user logs in and sees the dashboard.",
        plan=ReconstructedPlan(
            title=title,
            start_route="/login",
            steps=[
                ReconstructedStep(action="Open the app", route="/login"),
                ReconstructedStep(
                    action="Log in as admin",
                    selector=ReconstructedSelector(
                        kind="testid",
                        value="getByTestId('login-submit')",
                        provenance="LoginPage.java#submit",
                        verified=True,
                    ),
                    route="/login",
                ),
                ReconstructedStep(
                    action="Assert the dashboard heading", expected="Dashboard is shown"
                ),
            ],
        ),
        manual_steps=[ManualStep(action="Log in as admin", data="admin/pw", expected="Dashboard")],
        routes=["/login"],
        spec="import { test, expect } from '@playwright/test';",
        outcome="green",
        source=source,
    )
    fields.update(overrides)
    return KBRecord(**fields)


@pytest.fixture
def store(tmp_path: Path):
    """One local-mode store per test; closed so the storage-dir lock releases."""
    with KBStore(tmp_path / "kb") as kb_store:
        yield kb_store


class TestCollectionName:
    def test_uppercases_and_prefixes(self) -> None:
        assert collection_name("qa") == "kb_QA"
        assert collection_name(" NOTE ") == "kb_NOTE"
        assert collection_name("TEAM_2") == "kb_TEAM_2"

    @pytest.mark.parametrize("bad", ["", "  ", "bad key", "qa/1", "a.b"])
    def test_rejects_non_project_keys(self, bad: str) -> None:
        with pytest.raises(ValueError, match="project_key"):
            collection_name(bad)


class TestStoreRoundTrip:
    def test_upsert_and_search_returns_full_record(self, store: KBStore) -> None:
        record = _record()
        store.upsert("QA", [record], [[1.0, 0.0, 0.0]])

        results = store.search("QA", [1.0, 0.0, 0.0], top_n=5)

        assert len(results) == 1
        found, score = results[0]
        assert found == record  # full payload round-trip, plan included
        assert score == pytest.approx(1.0, abs=1e-6)

    def test_search_orders_by_cosine_similarity(self, store: KBStore) -> None:
        login = _record(ref="QA-1", title="Login works")
        delete = _record(ref="QA-2", title="Delete a user")
        store.upsert("QA", [login, delete], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

        results = store.search("QA", [0.9, 0.1, 0.0], top_n=5)

        assert [r.title for r, _ in results] == ["Login works", "Delete a user"]
        assert results[0][1] > results[1][1]

    def test_upsert_is_idempotent_by_record_id(self, store: KBStore) -> None:
        record = _record()
        store.upsert("QA", [record], [[1.0, 0.0, 0.0]])
        store.upsert("QA", [record], [[1.0, 0.0, 0.0]])

        assert store.count("QA") == 1

    def test_projects_are_fully_isolated(self, store: KBStore) -> None:
        qa = _record(project_key="QA", ref="QA-1")
        note = _record(project_key="NOTE", ref="NOTE-9", title="Create a note")
        store.upsert("QA", [qa], [[1.0, 0.0, 0.0]])
        store.upsert("NOTE", [note], [[1.0, 0.0, 0.0]])

        qa_hits = store.search("QA", [1.0, 0.0, 0.0], top_n=10)
        note_hits = store.search("NOTE", [1.0, 0.0, 0.0], top_n=10)

        assert [r.project_key for r, _ in qa_hits] == ["QA"]
        assert [r.project_key for r, _ in note_hits] == ["NOTE"]

    def test_search_on_never_seeded_project_is_empty_not_an_error(
        self, store: KBStore
    ) -> None:
        assert store.search("ZZ", [1.0, 0.0, 0.0], top_n=3) == []
        assert store.count("ZZ") == 0


class TestStoreValidation:
    def test_mismatched_records_and_vectors_raise(self, store: KBStore) -> None:
        with pytest.raises(ValueError, match="records but"):
            store.upsert("QA", [_record()], [])

    def test_empty_vector_raises(self, store: KBStore) -> None:
        with pytest.raises(ValueError, match="empty embedding"):
            store.upsert("QA", [_record()], [[]])

    def test_empty_upsert_is_a_no_op(self, store: KBStore) -> None:
        store.upsert("QA", [], [])
        assert store.count("QA") == 0


class TestRecordId:
    def test_stable_across_calls(self) -> None:
        assert make_record_id("QA", "pipeline", "QA-1") == make_record_id(
            "QA", "pipeline", "QA-1"
        )

    def test_distinct_per_project_source_and_ref(self) -> None:
        ids = {
            make_record_id("QA", "pipeline", "QA-1"),
            make_record_id("QA", "selenium-import", "QA-1"),
            make_record_id("NOTE", "pipeline", "QA-1"),
            make_record_id("QA", "pipeline", "QA-2"),
        }
        assert len(ids) == 4


def test_default_pipeline_never_imports_qdrant() -> None:
    """RAG off = byte-identical pipeline: importing the orchestrator (and config)
    must not pull qdrant_client into the process (wseu0ou acceptance criterion)."""
    code = (
        "import sys; "
        "import ai_test_gen.config, ai_test_gen.orchestrator; "
        "sys.exit(1 if 'qdrant_client' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, (
        f"qdrant_client leaked into the default pipeline import graph\n{result.stderr}"
    )
