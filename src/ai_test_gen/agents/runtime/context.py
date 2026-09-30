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

``build_model_settings`` turns the browser agents' ``Config`` knobs into pydantic-ai model
settings; every budget and knob itself lives in ``core/config.py``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from pydantic_ai.models.openai import OpenAIChatModelSettings

from ...core.config import Config, ReasoningEffort

logger = logging.getLogger(__name__)

_MISSING_PLACEHOLDER = "(no project context provided)"

_COMMENT_TOKEN_RE = re.compile(r"<!--|-->")
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


def _strip_html_comments(text: str) -> str:
    """Remove HTML comments, counting nesting depth so an outer comment that quotes a
    literal ``<!-- … -->`` example is removed whole (a non-greedy regex stops at the
    inner ``-->`` and leaks the rest). A stray ``-->`` outside any comment is kept;
    an unterminated comment strips to end-of-text (guidance must never leak)."""
    kept: list[str] = []
    depth = 0
    pos = 0
    for token in _COMMENT_TOKEN_RE.finditer(text):
        if token.group() == "<!--":
            if depth == 0:
                kept.append(text[pos : token.start()])
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                pos = token.end()
    if depth == 0:
        kept.append(text[pos:])
    else:
        logger.warning(
            "Context file has an unterminated '<!--' comment — everything after it was "
            "dropped from the agent prompt. Close the comment with '-->'."
        )
    return "".join(kept)


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
    stripped = _strip_html_comments(raw)
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", stripped)


_ACTIVATION_WORDS = ("activation", "aktivierung")


def declares_activation_flow(config: Config) -> bool:
    """True when the project context or map mentions an activation flow.

    Gates the ``activation.md`` prompt fragment, so an app without one never carries those
    rules. A plain word match on the comment-stripped files: a false positive only adds the
    fragment, while missing a real flow would leave the agents without it.
    """
    text = "\n".join(
        _load_context_file(path)
        for path in (config.project_context_path, config.project_map_path)
    ).lower()
    return any(word in text for word in _ACTIVATION_WORDS)


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


def build_model_settings(
    config: Config, reasoning_effort: ReasoningEffort | None
) -> OpenAIChatModelSettings:
    """Model settings for a browser agent: always-sequential tool calls + optional effort.

    - **``parallel_tool_calls=False`` — always.** Browser tools mutate ONE shared page, and
      pydantic-ai executes a turn's tool calls CONCURRENTLY — so a model that batches two actions
      in one turn can click/navigate out of order (and, when vision is on, race an
      ``inspect_screen`` screenshot with the navigation it should observe). One tool call per turn
      makes every browser agent's actions strictly sequential, which is the only correct order for
      UI automation. (The gateway must honor the flag — OpenAI-compatible servers may silently
      drop it; confirm on a real run.)
    - **Reasoning effort** — the agent's own field (``config.planner_reasoning_effort`` /
      ``config.healer_reasoning_effort``) when set.
    - **``max_tokens``** from ``config.agent_max_output_tokens`` when set: an explicit
      per-request completion budget. Overrides a gateway's small default, which can truncate a
      THINKING model's turn into a thinking-only response that pydantic-ai rejects and retries
      to exhaustion.

    Shared by the Planner and the Healer; the Generator (no browser) never uses this.
    """
    settings_kwargs: dict[str, Any] = {"parallel_tool_calls": False}
    if reasoning_effort:
        settings_kwargs["openai_reasoning_effort"] = reasoning_effort
    if config.agent_max_output_tokens is not None:
        settings_kwargs["max_tokens"] = config.agent_max_output_tokens
    return OpenAIChatModelSettings(**settings_kwargs)
