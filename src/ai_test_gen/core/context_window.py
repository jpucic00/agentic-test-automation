"""How full an agent's context window gets over one run, and what fills it.

``RunUsage.input_tokens`` is the SUM of every request's prompt — a 41-request Planner run
re-sends its growing history 41 times — so it says nothing about how full the window got.
Every ``ModelResponse`` in a run's captured history carries its own ``usage.input_tokens``:
the exact size of the prompt that produced it. ``profile_context`` reads those into a
per-request curve (first / peak / final) and, when the model's window size is configured
(``MODEL_CONTEXT_WINDOWS``, parsed by ``core/config.py``), the peak as a share of that window.

**Composition** of the peak request is an estimate, attributed from measured numbers:

- Each step's growth (``input_tokens`` of request k minus request k-1) is exactly what the
  history gained in between — the model's previous turn plus the tool results that answered
  it. That measured delta is split over those parts by character share, so a big
  ``browser_snapshot`` return gets the bulk of its step's growth.
- The first request (system prompt + task message + tool schemas + chat-template framing)
  has no delta to split: the prompt parts are prose, estimated at ~4 characters per token,
  and the remainder is reported as ``tool schemas + framing`` — the tool definitions are sent
  with every request but are not part of the message history. (The run's own growth ratio is
  NOT used here: JSON tool calls and results tokenise far denser than prose, ~2.7 chars/token
  on a demo run, which would inflate the prompt estimate and swallow the tool schemas.)

The categories therefore sum to the peak request's measured size (up to rounding) unless
history trimming (``SNAPSHOT_HISTORY_KEEP``) shrank the prompt mid-run, in which case the
negative steps are not attributed. A reasoning part counts where the history holds it; whether
the server's chat template actually replays earlier reasoning is model-specific.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TypedDict

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

logger = logging.getLogger(__name__)

WARN_PEAK_SHARE = 0.8
"""A peak at or above this share of the model's window is logged at WARNING."""

PROSE_CHARS_PER_TOKEN = 4.0
SCHEMAS = "tool schemas + framing"


class ContextProfile(TypedDict):
    requests: int
    per_request: list[int]
    first: int
    peak: int
    peak_request: int
    final: int
    window: int | None
    peak_pct: float | None
    composition: dict[str, int]


def _user_prompt_chars(part: UserPromptPart) -> int:
    if isinstance(part.content, str):
        return len(part.content)
    return sum(len(item) for item in part.content if isinstance(item, str))


def _part_chars(part: object) -> int:
    """Characters of one part as it goes to the model (text-only; images count 0)."""
    try:
        if isinstance(part, ToolReturnPart):
            return len(part.model_response_str())
        if isinstance(part, RetryPromptPart):
            return len(part.model_response())
        if isinstance(part, UserPromptPart):
            return _user_prompt_chars(part)
        if isinstance(part, ToolCallPart):
            return len(part.tool_name) + len(part.args_as_json_str())
        content = getattr(part, "content", "")
        return len(content) if isinstance(content, str) else 0
    except Exception:  # telemetry must never break a run
        return 0


def _category(part: object, *, first_request: bool) -> str:
    if isinstance(part, ToolReturnPart):
        return f"tool: {part.tool_name}"
    if isinstance(part, RetryPromptPart):
        return "retry prompts"
    if isinstance(part, SystemPromptPart):
        return "system prompt"
    if isinstance(part, UserPromptPart):
        return "task message" if first_request else "user messages"
    if isinstance(part, ToolCallPart):
        return "model tool calls"
    if isinstance(part, TextPart):
        return "model text"
    if isinstance(part, ThinkingPart):
        return "model reasoning"
    return "other"


def _chars_by_category(
    messages: Sequence[ModelMessage], *, first_request: bool
) -> dict[str, int]:
    """Characters per category across ``messages`` (instructions only in the first request)."""
    chars: dict[str, int] = {}
    for message in messages:
        if first_request and isinstance(message, ModelRequest) and message.instructions:
            chars["system prompt"] = chars.get("system prompt", 0) + len(message.instructions)
        for part in message.parts:
            size = _part_chars(part)
            if size:
                key = _category(part, first_request=first_request)
                chars[key] = chars.get(key, 0) + size
    return chars


def _composition(
    messages: Sequence[ModelMessage], response_indices: list[int], tokens: list[int], upto: int
) -> dict[str, int]:
    """Estimated tokens per category in the prompt of profiled request ``upto`` (0-based)."""
    alloc: dict[str, float] = {}
    for k in range(1, upto + 1):
        delta = tokens[k] - tokens[k - 1]
        if delta <= 0:
            continue  # trimmed (or unchanged) history: nothing to attribute
        step = _chars_by_category(
            messages[response_indices[k - 1] : response_indices[k]], first_request=False
        )
        total = sum(step.values())
        if not total:
            alloc[SCHEMAS] = alloc.get(SCHEMAS, 0.0) + delta
            continue
        for key, size in step.items():
            alloc[key] = alloc.get(key, 0.0) + delta * size / total

    base = _chars_by_category(messages[: response_indices[0]], first_request=True)
    estimated = {key: size / PROSE_CHARS_PER_TOKEN for key, size in base.items()}
    scale = min(1.0, tokens[0] / sum(estimated.values())) if estimated else 1.0
    for key, value in estimated.items():
        alloc[key] = alloc.get(key, 0.0) + value * scale
    alloc[SCHEMAS] = alloc.get(SCHEMAS, 0.0) + max(0.0, tokens[0] - sum(estimated.values()))

    rounded = {key: round(value) for key, value in alloc.items() if round(value) > 0}
    return dict(sorted(rounded.items(), key=lambda item: item[1], reverse=True))


def profile_context(
    messages: Sequence[ModelMessage],
    model: str,
    windows: Mapping[str, int] | None = None,
) -> ContextProfile | None:
    """Context profile of one agent run from its captured history; ``None`` without usage data.

    ``windows`` maps model names to context-window sizes (``config.model_context_windows``);
    without an entry for ``model`` the profile carries no window share.

    Only responses that report ``input_tokens`` are profiled — a gateway that returns no
    per-response usage gives no profile rather than a curve of zeros.
    """
    response_indices = [
        i
        for i, message in enumerate(messages)
        if isinstance(message, ModelResponse) and message.usage.input_tokens > 0
    ]
    if not response_indices:
        return None
    tokens = [messages[i].usage.input_tokens for i in response_indices]  # type: ignore[union-attr]
    peak_at = max(range(len(tokens)), key=tokens.__getitem__)
    window = (windows or {}).get(model)
    return ContextProfile(
        requests=len(tokens),
        per_request=tokens,
        first=tokens[0],
        peak=tokens[peak_at],
        peak_request=peak_at + 1,
        final=tokens[-1],
        window=window,
        peak_pct=round(100 * tokens[peak_at] / window, 1) if window else None,
        composition=_composition(messages, response_indices, tokens, peak_at),
    )


def format_peak(profile: ContextProfile | None) -> str:
    """``38,120 (29%)`` — or just the tokens without a configured window; ``-`` without data."""
    if profile is None:
        return "-"
    if profile["peak_pct"] is None:
        return f"{profile['peak']:,}"
    return f"{profile['peak']:,} ({profile['peak_pct']:.0f}%)"


def describe_context(profile: ContextProfile, *, top: int = 6) -> str:
    """One line: the peak and where it came from, then the biggest composition shares."""
    window = f" of {profile['window']:,}" if profile["window"] else ""
    head = (
        f"peak {format_peak(profile)}{window} at request {profile['peak_request']}/"
        f"{profile['requests']}, first {profile['first']:,}, final {profile['final']:,}"
    )
    total = sum(profile["composition"].values())
    if not total or top <= 0:
        return head
    shares = ", ".join(
        f"{key} {tokens:,} ({100 * tokens / total:.0f}%)"
        for key, tokens in list(profile["composition"].items())[:top]
    )
    return f"{head} — {shares}"


def log_context(agent: str, profile: ContextProfile | None) -> None:
    """INFO line per run; WARNING when the peak reaches ``WARN_PEAK_SHARE`` of the window."""
    if profile is None:
        return
    near_limit = profile["peak_pct"] is not None and profile["peak_pct"] >= 100 * WARN_PEAK_SHARE
    logger.log(
        logging.WARNING if near_limit else logging.INFO,
        "%s context: %s%s",
        agent,
        describe_context(profile),
        " — NEAR THE CONTEXT LIMIT" if near_limit else "",
    )
