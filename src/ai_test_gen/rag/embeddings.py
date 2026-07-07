"""Gateway embedding + rerank client (RETRIEVAL_MEMORY_PLAN.md §1.3–1.4).

Plain synchronous httpx against the OpenAI-compatible gateway, built with the
repo's direct-connect policy (``trust_env=False`` unless ``USE_HTTP_PROXY`` opts
back in; optional private CA / mTLS via ``mtls.py``) — the same policy as the
Xray/GitLab clients, because an env-configured proxy silently drops the gateway
connection.

The reranker is the fixed cross-encoder ``bge-reranker-v2-m3`` on ``/rerank``
(``Config.rerank_endpoint`` overrides the location, never the choice). Response
parsing tolerates the three shapes seen on real gateways — Cohere-style
``results[]``, TEI-style ``data[]``, and a bare list — the logic proven live by
``scripts/step0b_verify_embeddings.py``.

Error text carries the endpoint, status and a body excerpt, NEVER the request
headers — the API key stays out of logs (house rule from the run-log work).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from .. import mtls
from ..config import Config


class RagGatewayError(RuntimeError):
    """An /embeddings or /rerank call failed (transport, status, or shape)."""


def build_client(timeout: float = 30.0) -> httpx.Client:
    """An httpx client carrying the gateway direct-connect policy (see mtls.py)."""
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "trust_env": mtls.get_trust_env(),
        "verify": mtls.get_verify_arg(),
    }
    cert = mtls.get_cert_arg()
    if cert is not None:
        kwargs["cert"] = cert
    return httpx.Client(**kwargs)


def embed(
    config: Config, texts: Sequence[str], *, client: httpx.Client | None = None
) -> list[list[float]]:
    """Embed ``texts`` via the gateway ``/embeddings``; vectors in input order.

    The vector dimension is whatever the model returns — callers probe
    ``len(vector)`` (plan §1.4); nothing here assumes a width.
    """
    if not texts:
        return []
    url = config.llm_base_url.rstrip("/") + "/embeddings"
    body = _post_json(
        config,
        url,
        {"model": config.embedding_model, "input": list(texts)},
        client=client,
        what="/embeddings",
    )
    try:
        items = body["data"]
        ordered = sorted(items, key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in ordered]
    except (KeyError, TypeError, AttributeError):
        raise RagGatewayError(
            f"/embeddings returned an unexpected shape (no data[].embedding): "
            f"{_excerpt(body)}"
        ) from None
    if len(vectors) != len(texts) or any(
        not isinstance(v, list) or not v for v in vectors
    ):
        raise RagGatewayError(
            f"/embeddings returned {len(vectors)} vector(s) for {len(texts)} input(s), "
            "or an empty vector"
        )
    return vectors


def rerank(
    config: Config,
    query: str,
    documents: Sequence[str],
    top_n: int,
    *,
    client: httpx.Client | None = None,
) -> list[tuple[int, float]]:
    """Score ``documents`` against ``query`` with the cross-encoder reranker.

    Returns ``(document_index, score)`` pairs sorted by score descending,
    truncated to ``top_n``. Indexes refer to the ``documents`` argument.
    """
    if not documents:
        return []
    url = config.rerank_endpoint or config.llm_base_url.rstrip("/") + "/rerank"
    body = _post_json(
        config,
        url,
        {
            "model": config.reranker_model,
            "query": query,
            "documents": list(documents),
            "top_n": top_n,
        },
        client=client,
        what="/rerank",
    )

    # Cohere-style {"results": [...]}, TEI-style {"data": [...]}, or a bare list.
    candidates: Any = None
    if isinstance(body, dict):
        candidates = body.get("results") or body.get("data")
    elif isinstance(body, list):
        candidates = body
    if not isinstance(candidates, list) or not candidates:
        raise RagGatewayError(f"/rerank returned no ranked results: {_excerpt(body)}")

    scored: list[tuple[int, float]] = []
    for item in candidates:
        index = item.get("index") if isinstance(item, dict) else None
        score = (
            item.get("relevance_score", item.get("score")) if isinstance(item, dict) else None
        )
        if index is None or score is None:
            raise RagGatewayError(
                f"/rerank result item lacks index/score: {_excerpt(item)}"
            )
        if not 0 <= int(index) < len(documents):
            raise RagGatewayError(
                f"/rerank returned index {index} outside the {len(documents)} documents sent"
            )
        scored.append((int(index), float(score)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_n]


def _post_json(
    config: Config,
    url: str,
    payload: dict[str, Any],
    *,
    client: httpx.Client | None,
    what: str,
) -> Any:
    """POST and return parsed JSON; errors are RagGatewayError with key-free text."""
    owns_client = client is None
    http = client or build_client()
    try:
        try:
            response = http.post(
                url,
                headers={
                    "Authorization": f"Bearer {config.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.HTTPError as exc:
            # str(exc) on transport errors never includes request headers.
            raise RagGatewayError(
                f"{what} request to {url} failed: {type(exc).__name__}: {exc}"
            ) from exc
    finally:
        if owns_client:
            http.close()
    if response.status_code != 200:
        raise RagGatewayError(
            f"{what} returned HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError:
        raise RagGatewayError(f"{what} returned non-JSON: {response.text[:300]}") from None


def _excerpt(value: Any, limit: int = 200) -> str:
    return str(value)[:limit]
