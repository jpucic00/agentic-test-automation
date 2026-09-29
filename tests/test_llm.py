"""Unit tests for the gateway model builder + mTLS/proxy config — offline."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel

from ai_test_gen import mtls
from ai_test_gen.config import Config
from ai_test_gen.llm import (
    DeadlineModel,
    ModelRequestTimeoutError,
    build_openai_model,
    judge_reasoning_effort_support,
)


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


def _gateway_cfg(api_key: str = "k", **overrides: object) -> Config:
    fields: dict[str, object] = {
        "llm_base_url": "https://gateway.internal/v1",
        "llm_api_key": api_key,
        "agent_request_timeout_s": 180.0,
        "agent_request_attempts": 2,
    }
    fields.update(overrides)
    return cast(Config, SimpleNamespace(**fields))


def _inner(model: DeadlineModel) -> OpenAIChatModel:
    assert isinstance(model.wrapped, OpenAIChatModel)
    return model.wrapped


def test_build_openai_model_offline(monkeypatch):
    for var in ("USE_HTTP_PROXY", "SSL_CERT_FILE", "MTLS_PKCS12_FILE", "MTLS_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    model = build_openai_model(_gateway_cfg(), "openai/gpt-oss-120b")

    # Every built model is deadline-wrapped around the real OpenAIChatModel, which keeps
    # its identity (name/system/profile) so model settings and agents are unaffected.
    assert isinstance(model, DeadlineModel)
    assert isinstance(model.wrapped, OpenAIChatModel)
    assert model.model_name == "openai/gpt-oss-120b"
    assert model.system == "openai"


def test_build_openai_model_endpoint_override_and_shared_default(monkeypatch):
    # base_url/api_key override the shared gateway for ONE agent (the Planner's
    # PLANNER_LLM_* path); omitted → cfg.llm_base_url/llm_api_key, so every other
    # caller is unchanged. Introspected via the provider's OpenAI client — no network.
    for var in ("USE_HTTP_PROXY", "SSL_CERT_FILE", "MTLS_PKCS12_FILE", "MTLS_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    cfg = _gateway_cfg(api_key="shared-key")

    shared = _inner(build_openai_model(cfg, "m"))
    assert str(shared.client.base_url).rstrip("/") == "https://gateway.internal/v1"
    assert shared.client.api_key == "shared-key"

    overridden = _inner(
        build_openai_model(cfg, "m", base_url="https://planner.host/v1", api_key="planner-key")
    )
    assert str(overridden.client.base_url).rstrip("/") == "https://planner.host/v1"
    assert overridden.client.api_key == "planner-key"


def test_build_openai_model_applies_request_deadline_from_config(monkeypatch):
    # AGENT_REQUEST_TIMEOUT_S/ATTEMPTS reach the wrapper AND the httpx read timeout (connect
    # capped at 30s), so a silent stall fails fast in the client too.
    for var in ("USE_HTTP_PROXY", "SSL_CERT_FILE", "MTLS_PKCS12_FILE", "MTLS_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    model = build_openai_model(
        _gateway_cfg(agent_request_timeout_s=90.0, agent_request_attempts=3), "m"
    )
    assert model.deadline_s == 90.0
    assert model.attempts == 3
    timeout = _inner(model).client.timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 90.0
    assert timeout.connect == 30.0


def test_build_openai_model_explicit_timeout_overrides_the_knob(monkeypatch):
    # An explicit timeout_s (the Distiller's 240s) wins over AGENT_REQUEST_TIMEOUT_S for
    # that one model: it becomes both the deadline and the read timeout.
    for var in ("USE_HTTP_PROXY", "SSL_CERT_FILE", "MTLS_PKCS12_FILE", "MTLS_CERT_FILE"):
        monkeypatch.delenv(var, raising=False)
    model = build_openai_model(_gateway_cfg(), "m", timeout_s=240)
    assert model.deadline_s == 240
    timeout = _inner(model).client.timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 240
    assert timeout.connect == 30.0


# --- DeadlineModel: total wall-clock bound per model request ------------------------


def _slow_then_fast(delays: list[float], calls: list[int]) -> FunctionModel:
    """Fake gateway model: call N sleeps delays[N] (last value repeats), then answers."""

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = len(calls)
        calls.append(index)
        await asyncio.sleep(delays[min(index, len(delays) - 1)])
        return ModelResponse(parts=[TextPart(f"answer {index + 1}")])

    return FunctionModel(respond)


def _request(model: DeadlineModel) -> ModelResponse:
    return asyncio.run(
        model.request([ModelRequest.user_text_prompt("hi")], None, ModelRequestParameters())
    )


def test_deadline_model_retries_a_hung_request_then_raises(caplog):
    calls: list[int] = []
    model = DeadlineModel(_slow_then_fast([5.0], calls), deadline_s=0.05, attempts=2)

    with caplog.at_level(logging.WARNING, logger="ai_test_gen.llm"):
        with pytest.raises(ModelRequestTimeoutError, match=r"within 0\.05s on 2 attempt"):
            _request(model)

    assert len(calls) == 2  # the first try + exactly one retry, then give up
    assert "no response within 0.05s — retrying (attempt 2/2)" in caplog.text
    # The orchestrator's `except Exception` boundaries must catch it (run → status error).
    assert issubclass(ModelRequestTimeoutError, Exception)


def test_deadline_model_recovers_when_the_retry_answers():
    calls: list[int] = []
    model = DeadlineModel(_slow_then_fast([5.0, 0.0], calls), deadline_s=0.05, attempts=2)

    response = _request(model)

    assert len(calls) == 2
    assert response.parts == [TextPart("answer 2")]


def test_deadline_model_leaves_a_fast_model_untouched():
    calls: list[int] = []
    model = DeadlineModel(_slow_then_fast([0.0], calls), deadline_s=0.05, attempts=2)

    response = _request(model)

    assert len(calls) == 1
    assert response.parts == [TextPart("answer 1")]


def test_deadline_model_does_not_retry_an_inner_timeout_error():
    # A TimeoutError raised BY the wrapped request (not our deadline) propagates unchanged.
    calls: list[int] = []

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(len(calls))
        raise TimeoutError("inner")

    model = DeadlineModel(FunctionModel(respond), deadline_s=5.0, attempts=2)
    with pytest.raises(TimeoutError, match="inner"):
        _request(model)
    assert len(calls) == 1


_INVALID = (
    "Invalid response from openai chat completions endpoint: 1 validation error for "
    "ChatCompletion\nchoices.0.finish_reason\n  Input should be 'stop' [input_value='error']"
)


def _failing_then_ok(errors: list[Exception], calls: list[int]) -> FunctionModel:
    """Fake gateway model: call N raises errors[N] while there is one, then answers."""

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        index = len(calls)
        calls.append(index)
        if index < len(errors):
            raise errors[index]
        return ModelResponse(parts=[TextPart(f"answer {index + 1}")])

    return FunctionModel(respond)


def test_deadline_model_retries_an_invalid_gateway_response(caplog):
    # OpenRouter passes an upstream provider failure through as finish_reason "error", which
    # the OpenAI client rejects; the request had no side effects, so one retry is safe.
    calls: list[int] = []
    model = DeadlineModel(
        _failing_then_ok([UnexpectedModelBehavior(_INVALID)], calls), deadline_s=1.0, attempts=2
    )

    with caplog.at_level(logging.WARNING, logger="ai_test_gen.llm"):
        response = _request(model)

    assert len(calls) == 2
    assert response.parts == [TextPart("answer 2")]
    assert "invalid response from the gateway — retrying (attempt 2/2)" in caplog.text


def test_deadline_model_gives_up_on_repeated_invalid_responses():
    calls: list[int] = []
    errors: list[Exception] = [UnexpectedModelBehavior(_INVALID)] * 2
    model = DeadlineModel(_failing_then_ok(errors, calls), deadline_s=1.0, attempts=2)

    with pytest.raises(UnexpectedModelBehavior, match="Invalid response from"):
        _request(model)
    assert len(calls) == 2


def test_deadline_model_does_not_retry_other_model_errors():
    calls: list[int] = []
    errors: list[Exception] = [UnexpectedModelBehavior("Exceeded maximum output retries (15)")]
    model = DeadlineModel(_failing_then_ok(errors, calls), deadline_s=1.0, attempts=2)

    with pytest.raises(UnexpectedModelBehavior, match="output retries"):
        _request(model)
    assert len(calls) == 1


def test_agent_runs_through_a_deadline_wrapped_model():
    # Agents construct and run unchanged on the wrapper (settings pass straight through).
    calls: list[int] = []
    model = DeadlineModel(_slow_then_fast([0.0], calls), deadline_s=1.0, attempts=2)
    agent: Agent[None, str] = Agent(model=model, output_type=str)

    result = asyncio.run(agent.run("hi", model_settings={"parallel_tool_calls": False}))

    assert result.output == "answer 1"


# --- reasoning-effort support verdict (consumed by scripts/step0d_*) ---------------


def test_reasoning_effort_honored_when_high_materially_larger():
    assert judge_reasoning_effort_support(100, 400) == "honored"
    assert judge_reasoning_effort_support(100, 150) == "honored"  # exactly min_ratio


def test_reasoning_effort_not_honored_when_usage_near_identical():
    # The silent-drop case: the gateway accepted the param but usage barely moves.
    assert judge_reasoning_effort_support(100, 110) == "not-honored"
    assert judge_reasoning_effort_support(100, 100) == "not-honored"


def test_reasoning_effort_inconclusive_without_usable_usage():
    assert judge_reasoning_effort_support(None, 400) == "inconclusive"
    assert judge_reasoning_effort_support(100, None) == "inconclusive"
    assert judge_reasoning_effort_support(0, 0) == "inconclusive"


def test_reasoning_effort_custom_ratio():
    assert judge_reasoning_effort_support(100, 130, min_ratio=1.2) == "honored"
    assert judge_reasoning_effort_support(100, 130, min_ratio=2.0) == "not-honored"


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
