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

from ..config import Config

logger = logging.getLogger(__name__)

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
