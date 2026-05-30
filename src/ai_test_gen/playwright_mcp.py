"""Builds the Playwright MCP toolset (subprocess over stdio).

Pydantic AI runs ``@playwright/mcp`` as a subprocess and talks to it over stdio,
exposing the browser tools to an agent as a toolset. Attach the returned toolset
via ``Agent(model, toolsets=[build_playwright_mcp(config, storage_state)])``.

The server itself is configured by ``playwright-mcp-config.json`` at the repo
root (browser / headless / ``imageResponses`` settings — see §3.7); this module
only assembles the launch command and wires in an optional pre-authenticated
session.

Implements AI_TEST_GENERATION_GUIDE.md §3.7 (Phase 1.B — Playwright MCP &
Authentication).

API note: pydantic-ai 1.104.0 deprecates ``MCPServerStdio`` (the form the guide
originally showed) in favour of ``MCPToolset`` + a transport, and removes it in
pydantic-ai v2. We use the current API so this scaffold survives a pydantic-ai
bump. ``StdioTransport`` is re-exported from ``pydantic_ai.mcp``, so no direct
``fastmcp`` import is required.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai.mcp import MCPToolset, StdioTransport

from .config import Config

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MCP_CONFIG_PATH = PROJECT_ROOT / "playwright-mcp-config.json"

# Pin the MCP server version from day one. The guide uses ``@latest`` in Phase 1
# and only pins in Phase 2 — but version drift during the PoC makes it impossible
# to tell whether a failure is ours or upstream's. Bump deliberately; verify the
# current stable release before changing.
PLAYWRIGHT_MCP_VERSION = "0.0.75"
PLAYWRIGHT_MCP_PACKAGE = f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"


def build_playwright_mcp(config: Config, storage_state: Path | None = None) -> MCPToolset:
    """Create an ``MCPToolset`` that runs Playwright MCP over stdio.

    Args:
        config: app config. Reserved for future extensibility (e.g. proxy
            settings); accepted now so the signature is stable across phases.
        storage_state: optional path to a Playwright ``storage_state.json`` so the
            browser starts pre-authenticated (see ``scripts/save_auth_state.py``).
            Avoids agents burning tokens re-logging-in on every run.

    Returns an ``MCPToolset`` to attach via ``Agent(model, toolsets=[...])``.
    """
    del config  # not used yet; kept in the signature for forward compatibility

    args = [
        PLAYWRIGHT_MCP_PACKAGE,
        "--config",
        str(MCP_CONFIG_PATH),
        "--isolated",
    ]
    if storage_state is not None:
        args.extend(["--storage-state", str(storage_state)])

    return MCPToolset(StdioTransport(command="npx", args=args))
