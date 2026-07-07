"""Embedded per-project knowledge-base store (Qdrant local mode).

Strictly in-process: ``QdrantClient(path=...)`` persists under ``config.kb_path``
(default ``qdrant_storage/``, gitignored). No server, no ports, no ``qdrant_url``
(RETRIEVAL_MEMORY_PLAN.md §1.1). One collection per project — ``kb_<PROJECT_KEY>``
— so selectors and flows never cross applications (§1.2); ``search`` only ever
reads the named project's collection.

Import discipline: qdrant is imported lazily inside ``KBStore`` so that merely
importing this module (or anything that type-checks against it) stays free.
Pipeline code must only construct a ``KBStore`` behind ``config.rag_enabled``.

Local mode holds a file lock on the storage dir (one process at a time) — use
``KBStore`` as a context manager, or call ``close()``, to release it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import KBRecord

if TYPE_CHECKING:
    from pathlib import Path

# Collection dimension is probed from the first vector actually stored — never a
# hardcoded constant (plan §1.4): the embedding model's width is the gateway's
# business, and a stale constant would break silently on a model change.


def collection_name(project_key: str) -> str:
    """``kb_<PROJECT_KEY>`` — uppercased, validated.

    The key must look like a Jira project key (alphanumeric + underscore) so a
    typo can't silently create a junk collection.
    """
    key = project_key.strip().upper()
    if not key or not key.replace("_", "").isalnum():
        raise ValueError(
            f"project_key {project_key!r} is not a usable Jira project key "
            "(letters/digits/underscore, e.g. 'QA')"
        )
    return f"kb_{key}"


class KBStore:
    """Synchronous facade over Qdrant local mode for KBRecord round-trips."""

    def __init__(self, kb_path: Path) -> None:
        # Lazy import: only RAG-enabled code paths (or the seeding CLI) pay it.
        from qdrant_client import QdrantClient

        kb_path.mkdir(parents=True, exist_ok=True)
        self._client = QdrantClient(path=str(kb_path))

    # --- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KBStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- operations ----------------------------------------------------------
    def ensure_collection(self, project_key: str, dim: int) -> None:
        """Create ``kb_<key>`` with cosine vectors of ``dim`` if it doesn't exist."""
        from qdrant_client.models import Distance, VectorParams

        name = collection_name(project_key)
        if not self._client.collection_exists(name):
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    def upsert(
        self, project_key: str, records: list[KBRecord], vectors: list[list[float]]
    ) -> None:
        """Upsert records with their intent vectors; idempotent by ``record_id``.

        The collection is created on first use with the dimension of the vectors
        actually supplied (probed, not configured).
        """
        from qdrant_client.models import PointStruct

        if len(records) != len(vectors):
            raise ValueError(f"{len(records)} records but {len(vectors)} vectors")
        if not records:
            return
        if any(not v for v in vectors):
            raise ValueError("cannot upsert an empty embedding vector")
        self.ensure_collection(project_key, dim=len(vectors[0]))
        points = [
            PointStruct(id=record.record_id, vector=vector, payload=record.model_dump())
            for record, vector in zip(records, vectors)
        ]
        self._client.upsert(collection_name=collection_name(project_key), points=points)

    def search(
        self, project_key: str, vector: list[float], top_n: int
    ) -> list[tuple[KBRecord, float]]:
        """Top-``top_n`` nearest records from THIS project's collection only.

        An absent collection (project never seeded) is an empty result, not an
        error — retrieval is fail-open by design.
        """
        name = collection_name(project_key)
        if not self._client.collection_exists(name):
            return []
        response = self._client.query_points(
            collection_name=name, query=vector, limit=top_n, with_payload=True
        )
        return [
            (KBRecord.model_validate(point.payload), point.score)
            for point in response.points
        ]

    def count(self, project_key: str) -> int:
        """Number of records in the project's collection (0 if it doesn't exist)."""
        name = collection_name(project_key)
        if not self._client.collection_exists(name):
            return 0
        return self._client.count(collection_name=name, exact=True).count

    def existing_ids(self, project_key: str, record_ids: list[str]) -> set[str]:
        """Which of ``record_ids`` are already stored — lets seeding resume/skip."""
        name = collection_name(project_key)
        if not record_ids or not self._client.collection_exists(name):
            return set()
        points = self._client.retrieve(
            collection_name=name, ids=record_ids, with_payload=False, with_vectors=False
        )
        return {str(point.id) for point in points}
