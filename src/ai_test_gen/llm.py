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

Every model is also wrapped in :class:`DeadlineModel`: a total wall-clock deadline per
model request (``AGENT_REQUEST_TIMEOUT_S``) with a bounded retry (``AGENT_REQUEST_ATTEMPTS``).
httpx's read timeout is per CHUNK, so a gateway that trickles keep-alive bytes on a
non-streaming request resets it forever and the run hangs with no error.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from openai import DefaultAsyncHttpxClient
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from . import mtls
from .config import Config

logger = logging.getLogger(__name__)

# The connect phase stays short whatever the request deadline: an unreachable gateway
# should fail in seconds, not after the full per-request budget.
_CONNECT_TIMEOUT_S = 30.0


class ModelRequestTimeoutError(TimeoutError):
    """A model request produced no response within its deadline on every attempt.

    A plain ``Exception`` subclass, so the orchestrator's existing ``except Exception``
    boundaries end the run as ``status: "error"`` instead of letting it hang.
    """


@dataclass(init=False)
class DeadlineModel(WrapperModel):
    """Bound every ``request`` of the wrapped model by a TOTAL wall-clock deadline.

    ``asyncio.timeout`` cancels the in-flight HTTP call (closing its connection) when the
    deadline passes, whatever the socket is doing. A timed-out request is retried up to
    ``attempts`` tries in total, then :class:`ModelRequestTimeoutError` is raised.

    Retrying is side-effect free: a model request only RETURNS a response — any tool calls
    in it (browser clicks, navigation, file reads) are executed by the agent loop AFTER the
    response arrives, so a request cut off mid-flight has executed nothing to duplicate.

    ``request_stream`` is deliberately not bounded: no pipeline agent streams (all use
    ``Agent.run``), and a total deadline on a stream would cut off healthy long outputs.
    """

    deadline_s: float
    attempts: int

    def __init__(self, wrapped: Model, *, deadline_s: float, attempts: int) -> None:
        super().__init__(wrapped)
        self.deadline_s = deadline_s
        self.attempts = max(1, attempts)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        for attempt in range(1, self.attempts + 1):
            deadline = asyncio.timeout(self.deadline_s)
            try:
                async with deadline:
                    return await self.wrapped.request(
                        messages, model_settings, model_request_parameters
                    )
            except TimeoutError:
                if not deadline.expired():
                    raise  # a TimeoutError from inside the request, not our deadline
                if attempt < self.attempts:
                    logger.warning(
                        "%s: no response within %gs — retrying (attempt %d/%d)",
                        self.model_name,
                        self.deadline_s,
                        attempt + 1,
                        self.attempts,
                    )
        raise ModelRequestTimeoutError(
            f"{self.model_name}: model request got no response within {self.deadline_s:g}s "
            f"on {self.attempts} attempt(s) (AGENT_REQUEST_TIMEOUT_S / AGENT_REQUEST_ATTEMPTS)"
        )


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
) -> DeadlineModel:
    """Return an ``OpenAIChatModel`` for ``model_name`` on the corp gateway, deadline-wrapped.

    Applies the proven gateway httpx policy (direct-by-default, corp CA, optional
    mTLS) from ``ai_test_gen.mtls`` so every agent shares one connection config.

    ``base_url`` / ``api_key`` override the shared gateway for a single agent — the
    Planner passes ``config.planner_base_url`` / ``config.planner_api_key`` so it can
    target a separately-hosted (optionally keyless) OpenAI-compatible model. When both
    are omitted the shared ``config.llm_base_url`` / ``config.llm_api_key`` are used, so
    every other caller is unchanged.

    Every request is bounded by :class:`DeadlineModel`: ``config.agent_request_timeout_s``
    (``AGENT_REQUEST_TIMEOUT_S``) total per try, ``config.agent_request_attempts`` tries.
    The httpx read timeout is set to the same value (connect capped at 30s) so an ordinary
    silent stall also fails fast inside the client instead of on its 10-minute default.
    ``timeout_s`` overrides the knob for ONE caller (both the deadline and the read
    timeout) — the offline Distiller keeps its own longer per-turn bound this way.
    """
    deadline_s = timeout_s if timeout_s is not None else config.agent_request_timeout_s
    http_client = DefaultAsyncHttpxClient(
        trust_env=mtls.get_trust_env(),
        verify=mtls.get_verify_arg(),
        cert=mtls.get_cert_arg(),  # None when no mTLS is configured
        timeout=httpx.Timeout(deadline_s, connect=min(_CONNECT_TIMEOUT_S, deadline_s)),
    )
    provider = OpenAIProvider(
        base_url=base_url or config.llm_base_url,
        api_key=api_key or config.llm_api_key,
        http_client=http_client,
    )
    return DeadlineModel(
        OpenAIChatModel(model_name, provider=provider),
        deadline_s=deadline_s,
        attempts=config.agent_request_attempts,
    )
