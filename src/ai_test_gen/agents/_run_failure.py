"""Log WHAT failed when a browser-agent run dies in pydantic-ai's retry loop.

"Exceeded maximum retries (N) for output validation" carries no detail on its own: the
per-retry validation errors and the model's attempted output live only in the run's message
history, which pydantic-ai discards with the run — the log then cannot discriminate a
TRUNCATED emission (server-side token cap: JSON cut off mid-string at the tail) from a
MANGLED one (tool-call parser: field/type errors) from thinking text leaking into the
output. Wrapping the run in ``capture_run_messages`` keeps that history reachable, and
``summarize_run_failure`` renders the evidence: the exception cause chain, each retry
prompt (the validation errors), and every model part with its size — tool-call args
clipped head+tail, because the tail is what proves truncation. Logged at ERROR so the
default INFO run log carries the diagnosis.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import RetryPromptPart, ToolCallPart
from pydantic_ai.usage import UsageLimits

from ._context import agent_request_limit

logger = logging.getLogger(__name__)

# Clip long payloads but ALWAYS keep the tail — a token-capped emission stops mid-JSON at
# the END of the args, so a head-only clip would hide exactly the evidence we need.
_HEAD_CHARS = 900
_TAIL_CHARS = 500

# Parts whose content is replay noise in a failure summary (accessibility snapshots, the
# original task prompt, the system prompt); everything else in the tail window is evidence.
_SKIPPED_PARTS = {"ToolReturnPart", "UserPromptPart", "SystemPromptPart"}

_TAIL_MESSAGES = 6
_CAUSE_DEPTH = 4


def _clip(text: str) -> str:
    if len(text) <= _HEAD_CHARS + _TAIL_CHARS:
        return text
    return f"{text[:_HEAD_CHARS]} …[clipped, {len(text)} chars total]… {text[-_TAIL_CHARS:]}"


def _part_payload(part: Any) -> str | None:
    """One evidence line for a message part, or None when the part carries none."""
    if isinstance(part, ToolCallPart):
        args = part.args if isinstance(part.args, str) else json.dumps(part.args or {})
        return f"tool={part.tool_name} args {len(args)} chars: {_clip(args)}"
    if isinstance(part, RetryPromptPart):
        content = (
            part.content if isinstance(part.content, str) else json.dumps(part.content, default=str)
        )
        return f"validation errors: {_clip(content)}"
    content = getattr(part, "content", None)
    if isinstance(content, str):
        return f"{len(content)} chars: {_clip(content)}"
    return None


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten (possibly nested) exception groups to their leaf exceptions."""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_leaf_exceptions(sub))
        return leaves
    return [exc]


def summarize_run_failure(exc: BaseException, messages: Sequence[Any]) -> str:
    """Render the failure evidence: exception cause chain + the last messages' parts."""
    lines: list[str] = []
    cause = exc.__cause__ or exc.__context__
    depth = 0
    while cause is not None and depth < _CAUSE_DEPTH:
        lines.append(f"cause: {cause!r}")
        cause = cause.__cause__ or cause.__context__
        depth += 1
    for message in list(messages)[-_TAIL_MESSAGES:]:
        for part in getattr(message, "parts", ()):
            kind = type(part).__name__
            if kind in _SKIPPED_PARTS:
                continue
            payload = _part_payload(part)
            if payload is not None:
                lines.append(f"{kind} {payload}")
    return "\n".join(lines) or "(no captured messages)"


async def run_agent_logged[OutputT](
    agent: Agent[None, OutputT], user_message: str, *, agent_label: str
) -> OutputT:
    """Run a browser agent with its MCP context, logging failure evidence before re-raising.

    Drop-in for the Planner/Healer run sites: enters the agent as an async context manager
    (starting/stopping the Playwright MCP subprocess around the run) and applies the shared
    request limit. When pydantic-ai exhausts a retry budget — tool errors OR final-output
    validation — the captured message tail is logged at ERROR so the run log shows what the
    model actually emitted and why it was rejected. The exception re-raises unchanged, so
    orchestrator flow (heal accounting, clean-failure wrapping) is untouched.
    """
    # Version marker: this line in a run log PROVES the evidence-capture code is running —
    # its absence means the run used an older checkout, not that nothing failed.
    logger.info("%s run started (failure-evidence capture armed)", agent_label)
    with capture_run_messages() as messages:
        try:
            async with agent:
                result = await agent.run(
                    user_message, usage_limits=UsageLimits(request_limit=agent_request_limit())
                )
                return result.output
        except UnexpectedModelBehavior as exc:
            logger.error(
                "%s run aborted by pydantic-ai retry exhaustion: %s\n%s",
                agent_label,
                exc,
                summarize_run_failure(exc, messages),
            )
            raise
        except BaseExceptionGroup as group:
            # The agent/MCP internals run in anyio task groups, so a failure can surface
            # wrapped as "unhandled errors in a TaskGroup" — str(exc) then hides the leaves,
            # and a plain `except UnexpectedModelBehavior` never fires. Log every leaf, and
            # the full evidence when retry exhaustion is among them.
            leaves = _leaf_exceptions(group)
            logger.error(
                "%s run aborted by a task-group failure; leaf exception(s): %s",
                agent_label,
                "; ".join(repr(leaf) for leaf in leaves),
            )
            exhausted = next(
                (leaf for leaf in leaves if isinstance(leaf, UnexpectedModelBehavior)), None
            )
            if exhausted is not None:
                logger.error(
                    "%s retry-exhaustion evidence:\n%s",
                    agent_label,
                    summarize_run_failure(exhausted, messages),
                )
            raise
        except BaseException as exc:
            # Catch-all backstop (logged and RE-RAISED — flow unchanged, cancellation
            # included): nothing may leave an agent run without its evidence in the log.
            logger.error(
                "%s run aborted: %r\n%s",
                agent_label,
                exc,
                summarize_run_failure(exc, messages),
            )
            raise
