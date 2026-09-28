"""Answer a reasoning-only reply with a retry prompt that names the problem.

gpt-oss sometimes ends a turn with reasoning and nothing else (``finish_reason=stop``): it writes
the tool call it means to make INSIDE its reasoning — often ending on an opening ``{`` — and never
emits it. pydantic-ai then retries with a generic, hardcoded "Please return text or include your
response in a tool call.", which the model often misreads, so the episodes pile up. The output
retry budget (``AGENT_OUTPUT_RETRIES``) counts them across the WHOLE run, so they eventually abort
it even when each one recovers.

``ReasoningOnlyRetry`` is an ``after_model_request`` capability on the Planner and Healer: a
response whose parts are all ``ThinkingPart`` is rejected with a ``ModelRetry`` that says what
happened (nothing ran) and what to do (make the call for real, or return the final result). It
spends the same output-retry budget the generic prompt would. The call is never salvaged from the
reasoning text — the model has to make it itself. Each nudge adds 1 to the run's
``usage.details[REASONING_ONLY_RETRIES]`` so the usage report shows how often it happened.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelResponse, ThinkingPart
from pydantic_ai.models import ModelRequestContext

from ..usage import REASONING_ONLY_RETRIES

logger = logging.getLogger(__name__)

NUDGE = (
    "Your last reply contained only reasoning and no tool call, so NOTHING ran. If your "
    "reasoning describes a tool call, make that call now as a real function call — do not "
    "write it out in your reasoning. If you are finished, call `final_result` with your result."
)


def is_reasoning_only(response: ModelResponse) -> bool:
    """True when the response has at least one part and every part is reasoning."""
    return bool(response.parts) and all(isinstance(p, ThinkingPart) for p in response.parts)


@dataclass
class ReasoningOnlyRetry(AbstractCapability[Any]):
    """Reject a reasoning-only response with ``NUDGE`` and count it in the run's usage."""

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        if not is_reasoning_only(response):
            return response
        details = ctx.usage.details
        details[REASONING_ONLY_RETRIES] = details.get(REASONING_ONLY_RETRIES, 0) + 1
        logger.info(
            "Reasoning-only reply (no tool call, finish_reason=%s) — retrying with a named nudge",
            response.finish_reason,
        )
        raise ModelRetry(NUDGE)
