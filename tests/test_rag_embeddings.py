"""Offline tests for the gateway embedding/rerank client — mocked transport only.

Covers the /embeddings parsing, ALL THREE /rerank response shapes seen on real
gateways (Cohere ``results[]``, TEI ``data[]``, bare list), the error paths
(status, non-JSON, malformed shape) with key-redacted messages, and the
direct-connect client policy.
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import httpx
import pytest

from ai_test_gen.rag import embeddings
from ai_test_gen.rag.embeddings import RagGatewayError, build_client, embed, rerank


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- /embeddings -------------------------------------------------------------


class TestEmbed:
    def test_returns_vectors_in_input_order(self, cfg) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://gateway.internal/v1/embeddings"
            payload = json.loads(request.content)
            assert payload["model"] == "embed-model"
            assert payload["input"] == ["first", "second"]
            # Deliberately out of order — the client must sort by index.
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )

        with _client(handler) as http:
            vectors = embed(cfg, ["first", "second"], client=http)
        assert vectors == [[1.0, 0.0], [0.0, 1.0]]

    def test_empty_input_short_circuits_without_http(self, cfg) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call expected for empty input")

        with _client(handler) as http:
            assert embed(cfg, [], client=http) == []

    def test_vector_count_mismatch_raises(self, cfg) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0]}]}
            )

        with _client(handler) as http, pytest.raises(RagGatewayError, match="vector"):
            embed(cfg, ["a", "b"], client=http)

    def test_shape_without_embeddings_raises(self, cfg) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0}]})

        with _client(handler) as http, pytest.raises(RagGatewayError, match="shape"):
            embed(cfg, ["a"], client=http)


# --- /rerank ------------------------------------------------------------------

_DOCS = ["login flow doc", "cart doc", "delete doc"]


def _rerank_with_body(cfg, body: object) -> list[tuple[int, float]]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://gateway.internal/v1/rerank"
        payload = json.loads(request.content)
        assert payload["model"] == "rerank-model"
        assert payload["query"] == "user login"
        assert payload["documents"] == _DOCS
        return httpx.Response(200, json=body)

    with _client(handler) as http:
        return rerank(cfg, "user login", _DOCS, top_n=2, client=http)


class TestRerankShapes:
    def test_cohere_style_results(self, cfg) -> None:
        ranked = _rerank_with_body(
            cfg,
            {
                "results": [
                    {"index": 2, "relevance_score": 0.11},
                    {"index": 0, "relevance_score": 0.93},
                    {"index": 1, "relevance_score": 0.40},
                ]
            },
        )
        assert ranked == [(0, 0.93), (1, 0.40)]  # sorted desc, top_n=2

    def test_tei_style_data(self, cfg) -> None:
        ranked = _rerank_with_body(
            cfg,
            {"data": [{"index": 1, "score": 0.2}, {"index": 0, "score": 0.8}]},
        )
        assert ranked == [(0, 0.8), (1, 0.2)]

    def test_bare_list(self, cfg) -> None:
        ranked = _rerank_with_body(
            cfg, [{"index": 0, "score": 0.5}, {"index": 2, "score": 0.7}]
        )
        assert ranked == [(2, 0.7), (0, 0.5)]

    def test_empty_documents_short_circuits(self, cfg) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call expected for empty documents")

        with _client(handler) as http:
            assert rerank(cfg, "q", [], top_n=3, client=http) == []

    def test_rerank_endpoint_override_is_used(self, cfg) -> None:
        cfg_override = dataclasses.replace(
            cfg, rerank_endpoint="https://elsewhere.internal/api/rerank"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://elsewhere.internal/api/rerank"
            return httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})

        with _client(handler) as http:
            ranked = rerank(cfg_override, "q", ["only doc"], top_n=1, client=http)
        assert ranked == [(0, 1.0)]


class TestRerankErrors:
    def test_unknown_shape_raises(self, cfg) -> None:
        with pytest.raises(RagGatewayError, match="no ranked results"):
            _rerank_with_body(cfg, {"weird": []})

    def test_item_without_score_raises(self, cfg) -> None:
        with pytest.raises(RagGatewayError, match="index/score"):
            _rerank_with_body(cfg, {"results": [{"index": 0}]})

    def test_out_of_range_index_raises(self, cfg) -> None:
        with pytest.raises(RagGatewayError, match="outside"):
            _rerank_with_body(cfg, {"results": [{"index": 9, "score": 0.5}]})


# --- error hygiene + client policy ---------------------------------------------


class TestErrorHygiene:
    @pytest.mark.parametrize("status", [401, 500])
    def test_http_errors_carry_status_and_body_but_never_the_key(
        self, cfg, status: int
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, text="gateway said boom")

        with _client(handler) as http, pytest.raises(RagGatewayError) as excinfo:
            embed(cfg, ["a"], client=http)

        message = str(excinfo.value)
        assert f"HTTP {status}" in message
        assert "gateway said boom" in message
        assert cfg.llm_api_key not in message
        assert "Authorization" not in message

    def test_non_json_response_raises_key_free(self, cfg) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>proxy page</html>")

        with _client(handler) as http, pytest.raises(RagGatewayError) as excinfo:
            rerank(cfg, "q", ["d"], top_n=1, client=http)
        assert "non-JSON" in str(excinfo.value)
        assert cfg.llm_api_key not in str(excinfo.value)

    def test_transport_error_raises_key_free(self, cfg) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with _client(handler) as http, pytest.raises(RagGatewayError) as excinfo:
            embed(cfg, ["a"], client=http)
        assert "ConnectError" in str(excinfo.value)
        assert cfg.llm_api_key not in str(excinfo.value)


class TestBuildClient:
    def test_uses_direct_connect_policy(self, monkeypatch) -> None:
        captured: dict = {}

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        for var in (
            "USE_HTTP_PROXY",
            "SSL_CERT_FILE",
            "MTLS_PKCS12_FILE",
            "MTLS_CERT_FILE",
            "MTLS_KEY_FILE",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(embeddings.httpx, "Client", FakeClient)

        build_client()

        assert captured["trust_env"] is False  # env proxies are ignored (direct)
        assert captured["verify"] is True
        assert "cert" not in captured  # no mTLS configured → no cert kwarg
