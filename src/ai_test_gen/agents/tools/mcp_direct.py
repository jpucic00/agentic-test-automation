"""Shared plumbing for tools that drive the live Playwright MCP directly (``direct_call_tool``).

``inspect_screen``, ``probe_dom`` and ``count_matches`` each call an MCP tool the agents never see
(``browser_take_screenshot``, ``browser_evaluate``) on the SAME live browser the agent drives. They
share how to reach that browser beneath the agent-facing wrappers and how to read its results.
"""

from __future__ import annotations

from typing import Any

# The MCP tool the DOM Probe and count_matches drive with a FIXED, pipeline-authored function.
# Hidden from the agents' toolset (see browser.mcp._BLOCKED_TOOL_MARKERS); reachable only via
# direct_call_tool — the model never authors JS.
EVALUATE_TOOL = "browser_evaluate"

# Hard cap on the text returned to the agent — recon must inform, not flood the context.
RESULT_CHAR_CAP = 4000

# Some MCP tool results append the full page snapshot; a tool's own payload is self-contained,
# so anything from this marker on is dead weight and is stripped before returning.
_SNAPSHOT_MARKER = "Page Snapshot"


def underlying_mcp(toolset: Any) -> Any | None:
    """Walk a toolset's wrapper chain to the object exposing ``direct_call_tool``.

    ``build_playwright_mcp`` returns the live ``MCPToolset`` wrapped in a ``.filtered(...)`` layer;
    only the underlying ``MCPToolset`` exposes ``direct_call_tool``. Follow ``.wrapped`` until we
    find it (or run out), so a tool can act on the SAME live browser the agent drives. Returns
    None if no layer can call a tool directly (defensive — callers degrade).
    """
    seen = toolset
    while seen is not None and not hasattr(seen, "direct_call_tool"):
        seen = getattr(seen, "wrapped", None)
    return seen


def result_text(result: Any) -> str:
    """Best-effort text of a ``direct_call_tool`` result (plain string or content-item list)."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        texts = []
        for item in result:
            text = item if isinstance(item, str) else getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if text:
                texts.append(text)
        return "\n".join(texts)
    return str(result)


def clean_result(raw: Any) -> str:
    """Strip any trailing page snapshot and cap the size of a direct tool result."""
    text = result_text(raw)
    marker_at = text.find(_SNAPSHOT_MARKER)
    if marker_at >= 0:
        text = text[:marker_at].rstrip()
    if len(text) > RESULT_CHAR_CAP:
        text = text[:RESULT_CHAR_CAP] + " …[probe result truncated]"
    return text
