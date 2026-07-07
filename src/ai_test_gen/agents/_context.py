"""Helpers for assembling agent system prompts with project context.

A single place that injects the two human-authored context files into an agent's
system prompt (AI_TEST_GENERATION_GUIDE.md §3.5b):

- ``project_context.md`` — conventions/quirks; loaded into EVERY agent.
- ``project_map.md`` — routes/flows; loaded only into the agents that drive the
  browser (Planner, Healer), via ``include_map=True``.

Keeping the Generator's context lean (no map) matters: mid-tier models degrade
past ~30K tokens, so every token saved makes structured output more reliable.
The loader therefore strips HTML comments (author guidance, not app facts) before
injection, and warns loudly when a file still carries template placeholders —
an unfilled template reads to the model as real app documentation.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, cast

from pydantic_ai.models.openai import OpenAIChatModelSettings

from ..config import Config

logger = logging.getLogger(__name__)

ReasoningEffort = Literal["low", "medium", "high"]
_VALID_REASONING_EFFORTS = ("low", "medium", "high")

_MISSING_PLACEHOLDER = "(no project context provided)"

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")

# Raw-text markers identifying a context file that is still an unfilled template.
# Checked BEFORE comment-stripping, so template headers inside comments count too.
# Current template style: the header line, the title placeholder, inline examples.
# Legacy template style: bracketed ALL-CAPS instructions.
_TEMPLATE_MARKERS = (
    "TEMPLATE — copy to",
    "<APP NAME>",
    "<e.g. ",
    "[REPLACE",
    "[EXAMPLE",
    "[FILL IN",
    "[CUSTOMIZE",
)


def _load_context_file(path: Path) -> str:
    """Return ``path``'s text prepared for prompt injection.

    - Missing file → a short placeholder (agents must still build and run when the
      context files have not been filled in yet; the repo ships templates).
    - HTML comments are stripped: they are guidance for the human author, and to a
      model they read as instructions with system-prompt authority.
    - Template placeholder markers trigger a WARNING — the agents would otherwise
      treat the template's fictional examples as real app facts.
    """
    if not path.exists():
        return _MISSING_PLACEHOLDER
    raw = path.read_text()
    marker_count = sum(raw.count(marker) for marker in _TEMPLATE_MARKERS)
    if marker_count:
        logger.warning(
            "%s still contains %d template placeholder marker(s) — agents are being "
            "prompted with template content, not your app's real conventions. "
            "Fill it in (see SETUP.md) before trusting any generated plan or test.",
            path.name,
            marker_count,
        )
    stripped = _HTML_COMMENT_RE.sub("", raw)
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", stripped)


def assemble_system_prompt(
    config: Config,
    base_prompt: str,
    *,
    include_map: bool = True,
) -> str:
    """Append ``project_context.md`` (and optionally ``project_map.md``) to a base prompt.

    ``project_context.md`` is always appended under a ``# Project Context`` header.
    ``project_map.md`` is appended under ``# Application Map`` only when
    ``include_map`` is true (Planner/Healer); the Generator passes
    ``include_map=False``.
    """
    parts = [
        base_prompt,
        "---",
        "# Project Context",
        _load_context_file(config.project_context_path),
    ]

    if include_map:
        parts.extend(
            ["---", "# Application Map", _load_context_file(config.project_map_path)]
        )

    return "\n\n".join(parts)


def agent_retries(default: int = 5) -> int:
    """Max retries for an agent's tool/output errors.

    Browser-driving agents (Planner, Healer) make many MCP tool calls and must recover from
    transient "element not found" errors while hunting for selectors; pydantic-ai's low default
    aborts the whole run after only a couple (e.g. "Tool 'browser_type' exceeded max retries").
    Override via the ``AGENT_MCP_RETRIES`` env var.
    """
    raw = os.environ.get("AGENT_MCP_RETRIES")
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def reasoning_effort(env_var: str) -> ReasoningEffort | None:
    """Validated reasoning-effort setting from ``env_var`` (None when unset).

    Only meaningful on reasoning models (e.g. gpt-oss). An invalid value fails fast —
    a typo'd effort that silently disappears would masquerade as a tuned pipeline.
    When set, a warning reminds that gateway support must be PROVEN: OpenAI-compatible
    gateways commonly accept unknown params and silently drop them, so the setting is
    only trustworthy after ``scripts/step0d_verify_reasoning_effort.py`` reports HONORED.
    """
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value not in _VALID_REASONING_EFFORTS:
        raise ValueError(
            f"{env_var}={raw!r} is not a valid reasoning effort; "
            f"use one of {_VALID_REASONING_EFFORTS} or unset it"
        )
    logger.warning(
        "%s=%s is set. Gateways may silently DROP unknown request params — only trust "
        "this setting if scripts/step0d_verify_reasoning_effort.py reported HONORED "
        "against your gateway/model.",
        env_var,
        value,
    )
    return cast(ReasoningEffort, value)


def agent_output_retries(default: int = 15) -> int:
    """Structured-output retry budget from ``AGENT_OUTPUT_RETRIES`` (default 15).

    Retries for the MODEL'S OWN responses — empty turns, unparsed tool calls, output-schema
    validation failures — as opposed to ``agent_retries`` (tool errors). pydantic-ai counts
    these CUMULATIVELY across a run (successful turns never reset the counter) and, when no
    separate budget is set, falls back to the tool budget (5): a gateway whose tool-call
    parser intermittently returns empty husk turns then kills a long exploration even though
    each bounce is individually recoverable. 15 rides that out; ``AGENT_REQUEST_LIMIT``
    remains the hard backstop on total run size.
    """
    raw = os.environ.get("AGENT_OUTPUT_RETRIES")
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def build_model_settings(effort_env: str) -> OpenAIChatModelSettings:
    """Model settings for a browser agent: always-sequential tool calls + optional effort.

    - **``parallel_tool_calls=False`` — always.** Browser tools mutate ONE shared page, and
      pydantic-ai executes a turn's tool calls CONCURRENTLY — so a model that batches two actions
      in one turn can click/navigate out of order (and, when vision is on, race an
      ``inspect_screen`` screenshot with the navigation it should observe). One tool call per turn
      makes every browser agent's actions strictly sequential, which is the only correct order for
      UI automation. (The gateway must honor the flag — OpenAI-compatible servers may silently
      drop it; confirm on a real run.)
    - **Reasoning effort** from ``effort_env`` (see ``reasoning_effort``) when that env var is set.
    - **``max_tokens``** from ``AGENT_MAX_OUTPUT_TOKENS`` when set: an explicit per-request
      completion budget. Overrides a gateway's small default, which can truncate a THINKING
      model's turn into a thinking-only response that pydantic-ai rejects and retries to
      exhaustion (see ``agent_max_output_tokens``).

    Shared by the Planner and the Healer; the Generator (no browser) never uses this.
    """
    settings_kwargs: dict[str, Any] = {"parallel_tool_calls": False}
    effort = reasoning_effort(effort_env)
    if effort:
        settings_kwargs["openai_reasoning_effort"] = effort
    max_tokens = agent_max_output_tokens()
    if max_tokens is not None:
        settings_kwargs["max_tokens"] = max_tokens
    return OpenAIChatModelSettings(**settings_kwargs)


def agent_max_output_tokens() -> int | None:
    """Per-request completion budget from ``AGENT_MAX_OUTPUT_TOKENS`` (unset = provider default).

    Some gateways apply a SMALL default ``max_tokens`` when the request carries none. A
    thinking model then spends the whole budget on its reasoning and the turn arrives
    thinking-only or cut mid-emission — rejected, retried (with a longer context that thinks
    even longer), and exhausted within a few turns. An explicit request-level budget overrides
    such defaults wherever the gateway honors the param. Unset, invalid, or non-positive
    values leave the request untouched.
    """
    raw = os.environ.get("AGENT_MAX_OUTPUT_TOKENS")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def agent_request_limit(default: int = 300) -> int:
    """Max model requests per agent run (pydantic-ai ``UsageLimits.request_limit``).

    A browser agent exploring a multi-step flow makes many tool round-trips; pydantic-ai's
    default of 50 aborts mid-exploration on a complex case (``UsageLimitExceeded``). Override
    via the ``AGENT_REQUEST_LIMIT`` env var.
    """
    raw = os.environ.get("AGENT_REQUEST_LIMIT")
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default
