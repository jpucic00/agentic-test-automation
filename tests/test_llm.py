"""Unit tests for the gateway model builder + mTLS/proxy config — offline."""
from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

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


# --- requests-based clients (Xray/GitLab) share the gateway proxy/CA policy --------
_MTLS_VARS = ("USE_HTTP_PROXY", "SSL_CERT_FILE", "MTLS_PKCS12_FILE", "MTLS_CERT_FILE")


def test_build_requests_session_defaults_to_direct(monkeypatch):
    for var in _MTLS_VARS:
        monkeypatch.delenv(var, raising=False)
    session = mtls.build_requests_session()
    # The whole point: ignore env HTTP(S)_PROXY by default, like the gateway httpx client
    # (requests' own default is trust_env=True, which is what dropped Xray/GitLab calls).
    assert session.trust_env is False
    assert session.verify is True
    assert session.cert is None


def test_build_requests_session_honors_proxy_and_corp_ca(monkeypatch):
    for var in ("MTLS_PKCS12_FILE", "MTLS_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("USE_HTTP_PROXY", "true")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp/ca.pem")
    session = mtls.build_requests_session()
    assert session.trust_env is True
    assert session.verify == "/etc/corp/ca.pem"


def test_apply_requests_policy_mutates_existing_session(monkeypatch):
    """python-gitlab owns its session; the policy is applied in place."""
    import requests

    for var in _MTLS_VARS:
        monkeypatch.delenv(var, raising=False)
    session = requests.Session()
    assert session.trust_env is True  # requests default — the bug being fixed
    mtls.apply_requests_policy(session)
    assert session.trust_env is False


def test_requests_session_rejects_encrypted_pem_key(monkeypatch):
    # requests' cert= can't take an encrypted-key 3-tuple — fail with a clear message.
    monkeypatch.delenv("MTLS_PKCS12_FILE", raising=False)
    monkeypatch.setenv("MTLS_CERT_FILE", "/c.pem")
    monkeypatch.setenv("MTLS_KEY_FILE", "/k.pem")
    monkeypatch.setenv("MTLS_KEY_PASSWORD", "pw")
    with pytest.raises(ValueError, match="encrypted PEM key"):
        mtls.build_requests_session()
