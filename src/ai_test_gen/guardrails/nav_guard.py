"""Keep the browser agents on the allowed hosts: refuse off-list navigation, flag off-list pages.

The config guard only proves the CONFIGURED URLs are non-prod; once the Planner or Healer
drives a browser it can type any URL or follow any link. ``NavigationGuard`` is an
``MCPToolset.process_tool_call`` hook that ``build_playwright_mcp`` installs on every Playwright
MCP toolset, wrapping (not replacing) any caller hook such as the ``LocatorFailureGuard``:

- **Refuse**: ``browser_navigate`` — and ``browser_tabs`` ``action="new"`` with a ``url`` — to a
  URL whose origin is not on the allow-list (every ``STAGING_BASE_URL`` environment plus every
  ``STAGING_EXTRA_URLS`` host) is NOT sent to the browser. The agent gets a tool result naming
  the allowed hosts and telling it not to retry. ``about:blank`` is allowed; ``data:``,
  ``file:`` and every other non-http(s) scheme are refused.
- **Flag**: after any other tool call, when the result's ``- Page URL:`` line shows the browser
  on an off-list origin (a link click or redirect took it there), a warning is appended telling
  the agent to navigate back to an allowed URL, and the event is logged at WARNING.

Playwright MCP's own ``--allowed-origins`` is deliberately NOT used: it aborts EVERY browser
request to an unlisted origin — fonts, CDNs, third-party APIs — which breaks real apps, and it
is documented as not a security boundary. This guard constrains where the agent points the
page, not what the page loads.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.mcp import CallToolFunc, ProcessToolCallback, ToolResult

from .allowlist import navigation_refusal, offlist_page_url

logger = logging.getLogger(__name__)

NAVIGATE_TOOL = "browser_navigate"
TABS_TOOL = "browser_tabs"


def _requested_url(name: str, tool_args: dict[str, Any]) -> str | None:
    """The URL a navigating tool call would open, or ``None`` for non-navigating calls."""
    if name == TABS_TOOL and tool_args.get("action") != "new":
        return None
    if name not in (NAVIGATE_TOOL, TABS_TOOL):
        return None
    url = tool_args.get("url")
    return url if isinstance(url, str) and url.strip() else None


def _result_text(result: ToolResult) -> str:
    """The text parts of an MCP tool result (images and structured parts are skipped)."""
    if isinstance(result, str):
        return result
    if isinstance(result, Sequence) and not isinstance(result, dict):
        return "\n".join(part for part in result if isinstance(part, str))
    return ""


def _append(result: ToolResult, note: str) -> ToolResult:
    if isinstance(result, str):
        return f"{result}\n\n{note}"
    if isinstance(result, list):
        return [*result, note]
    return [result, note]


class NavigationGuard:
    """``process_tool_call`` hook enforcing the navigation allow-list; wraps an optional hook."""

    def __init__(
        self, allowed_origins: Sequence[str], inner: ProcessToolCallback | None = None
    ) -> None:
        self.allowed_origins = tuple(allowed_origins)
        self.inner = inner

    def _hosts(self) -> str:
        return ", ".join(self.allowed_origins) or "(none configured)"

    async def __call__(
        self,
        ctx: RunContext[Any],
        call_tool: CallToolFunc,
        name: str,
        tool_args: dict[str, Any],
    ) -> ToolResult:
        target = _requested_url(name, tool_args)
        if target is not None:
            reason = navigation_refusal(target, self.allowed_origins)
            if reason is not None:
                logger.warning("Navigation guard: refused %s to %r — %s", name, target, reason)
                return (
                    f"NAVIGATION REFUSED — not performed: {reason}. The only hosts this run may "
                    f"open are: {self._hosts()}. Do NOT retry this URL or any other host outside "
                    "that list; continue on an allowed host (a host the app needs but that is not "
                    "listed is a configuration gap — record it in your notes)."
                )
        if self.inner is not None:
            result = await self.inner(ctx, call_tool, name, tool_args)
        else:
            result = await call_tool(name, tool_args)
        off_list = offlist_page_url(_result_text(result), self.allowed_origins)
        if off_list is None:
            return result
        logger.warning("Navigation guard: %s left the browser on off-list page %s", name, off_list)
        return _append(
            result,
            f"WARNING: the browser is now on {off_list}, which is OUTSIDE the allowed hosts "
            f"({self._hosts()}). Do not interact with this page — navigate back to an allowed "
            "URL now.",
        )
