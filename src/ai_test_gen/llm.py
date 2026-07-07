"""Build the gateway LLM model for the agents, with the corp mTLS / proxy policy.

The Planner / Generator / Healer all reach the LLM gateway through pydantic-ai's
``OpenAIProvider``. The gateway needs the same httpx policy Phase 0 proved out (see
``ai_test_gen.mtls``):

- ``trust_env`` defaults to False — connect DIRECTLY and IGNORE the environment
  ``HTTP(S)_PROXY``. Routing the gateway call through the env proxy drops it with
  "Server disconnected without sending a response" even though the TLS handshake
  succeeds (set ``USE_HTTP_PROXY=true`` to opt back in).
- ``verify`` points at the corporate CA bundle (``SSL_CERT_FILE``) when set.
- ``cert`` carries an optional mTLS client certificate (``MTLS_*`` env vars).

Without this, the agents fail to reach the gateway on the company laptop with an
``APIConnectionError`` even though the Phase 0 ``scripts/step0_*`` checks pass.
"""
from __future__ import annotations

from typing import Any

import httpx
from openai import DefaultAsyncHttpxClient
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from . import mtls
from .config import Config


def judge_reasoning_effort_support(
    low_tokens: int | None,
    high_tokens: int | None,
    *,
    min_ratio: float = 1.5,
) -> str:
    """Verdict on whether a gateway honored the ``reasoning_effort`` request param.

    Compares the (reasoning or completion) token usage of the SAME prompt sent at
    ``low`` vs ``high`` effort. A gateway that silently drops the param produces
    near-identical usage; an honoring one deliberates materially longer at high.
    Pure comparison logic so ``scripts/step0d_verify_reasoning_effort.py``'s verdict
    is unit-testable without a network.

    Returns one of:
    - ``"honored"`` — high-effort usage >= ``min_ratio`` x low-effort usage.
    - ``"not-honored"`` — both probes answered but usage is too similar; assume the
      gateway dropped the param (fail-closed: don't trust the knob).
    - ``"inconclusive"`` — usage missing/zero on either probe; nothing to compare.
    """
    if not low_tokens or not high_tokens or low_tokens <= 0 or high_tokens <= 0:
        return "inconclusive"
    return "honored" if high_tokens >= low_tokens * min_ratio else "not-honored"


def build_openai_model(
    config: Config,
    model_name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_s: float | None = None,
) -> OpenAIChatModel:
    """Return an ``OpenAIChatModel`` for ``model_name`` on the corp gateway.

    Applies the proven gateway httpx policy (direct-by-default, corp CA, optional
    mTLS) from ``ai_test_gen.mtls`` so every agent shares one connection config.

    ``base_url`` / ``api_key`` override the shared gateway for a single agent — the
    Planner passes ``config.planner_base_url`` / ``config.planner_api_key`` so it can
    target a separately-hosted (optionally keyless) OpenAI-compatible model. When both
    are omitted the shared ``config.llm_base_url`` / ``config.llm_api_key`` are used, so
    every other caller is unchanged.

    ``timeout_s`` bounds a single request (connect capped at 30s) for callers that
    must not dangle — the client library's default is 10 MINUTES per attempt (times
    its retries), which reads as a silently hung process. Omitted → that default
    stands, so the browser agents' long turns are unaffected.
    """
    extra: dict[str, Any] = {}
    if timeout_s is not None:
        extra["timeout"] = httpx.Timeout(timeout_s, connect=30.0)
    http_client = DefaultAsyncHttpxClient(
        trust_env=mtls.get_trust_env(),
        verify=mtls.get_verify_arg(),
        cert=mtls.get_cert_arg(),  # None when no mTLS is configured
        **extra,
    )
    provider = OpenAIProvider(
        base_url=base_url or config.llm_base_url,
        api_key=api_key or config.llm_api_key,
        http_client=http_client,
    )
    return OpenAIChatModel(model_name, provider=provider)
