"""Helpers for assembling agent system prompts with project context.

A single place that injects the two human-authored context files into an agent's
system prompt (AI_TEST_GENERATION_GUIDE.md §3.5b):

- ``project_context.md`` — conventions/quirks; loaded into EVERY agent.
- ``project_map.md`` — routes/flows; loaded only into the agents that drive the
  browser (Planner, Healer), via ``include_map=True``.

Keeping the Generator's context lean (no map) matters: mid-tier models degrade
past ~30K tokens, so every token saved makes structured output more reliable.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..config import Config

_MISSING_PLACEHOLDER = "(no project context provided)"


def _safe_read(path: Path) -> str:
    """Return ``path``'s text, or a placeholder if the file is missing.

    Agents must still build and run when the context files have not been filled
    in yet (the repo ships templates), so a missing file is not an error.
    """
    if not path.exists():
        return _MISSING_PLACEHOLDER
    return path.read_text()


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
    parts = [base_prompt, "---", "# Project Context", _safe_read(config.project_context_path)]

    if include_map:
        parts.extend(["---", "# Application Map", _safe_read(config.project_map_path)])

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


def agent_request_limit(default: int = 150) -> int:
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
