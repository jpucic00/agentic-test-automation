"""Unit tests for the gateway model builder + mTLS/proxy config — offline."""
from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from ai_test_gen import mtls
from ai_test_gen.config import Config
from ai_test_gen.llm import build_openai_model


def test_mtls_defaults_to_direct_connection(monkeypatch):
    for var in ("USE_HTTP_PROXY", "SSL_CERT_FILE", "MTLS_PKCS12_FILE", "MTLS_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    assert mtls.get_trust_env() is False  # ignore env HTTP(S)_PROXY by default
    assert mtls.get_verify_arg() is True
    assert mtls.get_cert_arg() is None


def test_use_http_proxy_opts_back_in(monkeypatch):
    monkeypatch.setenv("USE_HTTP_PROXY", "true")
    assert mtls.get_trust_env() is True


def test_verify_arg_points_at_corp_ca_when_set(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp/ca.pem")
    assert mtls.get_verify_arg() == "/etc/corp/ca.pem"


def test_build_openai_model_offline(monkeypatch):
    for var in ("USE_HTTP_PROXY", "SSL_CERT_FILE", "MTLS_PKCS12_FILE", "MTLS_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    cfg = SimpleNamespace(llm_base_url="https://gateway.internal/v1", llm_api_key="k")
    model = build_openai_model(cast(Config, cfg), "openai/gpt-oss-120b")
    from pydantic_ai.models.openai import OpenAIChatModel

    assert isinstance(model, OpenAIChatModel)
