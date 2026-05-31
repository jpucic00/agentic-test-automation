"""Builds the Playwright MCP toolset (subprocess over stdio).

Pydantic AI runs the ``@playwright/mcp`` server as a subprocess and talks to it
over stdio, exposing the browser tools to an agent as a toolset. Attach the
returned toolset via
``Agent(model, toolsets=[build_playwright_mcp(config, storage_state)])``.

The server itself is configured by ``playwright-mcp-config.json`` at the repo
root (browser / headless / ``imageResponses`` settings — see §3.7); this module
assembles the launch command and wires in an optional pre-authenticated session.

Launch via ``node <cli.js>`` directly, NOT ``npx``: ``npx`` is a resolver/wrapper
that spawns the real server as a grandchild and does not reliably forward the
stdio pipe to it on every environment — the MCP ``initialize`` handshake then
hangs and pydantic-ai reports "failed to initialize server session" (observed on
the company laptop; npx worked on the dev PC, so it is environment-specific).
Running ``node`` on the installed CLI removes that layer and is deterministic for
the Phase 2 Docker image. The server is pinned in ``output/package.json`` and
installed by ``cd output && npm install``.

Implements AI_TEST_GENERATION_GUIDE.md §3.7 (Phase 1.B — Playwright MCP &
Authentication).

API note: pydantic-ai 1.104.0 deprecates ``MCPServerStdio`` in favour of
``MCPToolset`` + a transport (removed in v2); ``StdioTransport`` is re-exported
from ``pydantic_ai.mcp``.
"""
from __future__ import annotations

import atexit
import json
import os
import tempfile
from pathlib import Path

from pydantic_ai.mcp import MCPToolset, StdioTransport

from .config import Config

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MCP_CONFIG_PATH = PROJECT_ROOT / "playwright-mcp-config.json"

# Pin the MCP server version from day one (declared in output/package.json). The
# guide uses ``@latest`` in Phase 1 and only pins in Phase 2 — but version drift
# during the PoC makes it impossible to tell whether a failure is ours or
# upstream's. Bump deliberately; verify the current stable release before changing.
PLAYWRIGHT_MCP_VERSION = "0.0.75"
PLAYWRIGHT_MCP_PACKAGE = f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"

# The server CLI, installed locally under output/node_modules by
# ``cd output && npm install`` (pinned in output/package.json). Launched with
# ``node <cli.js>`` — see the module docstring for why we avoid npx.
MCP_CLI_PATH = PROJECT_ROOT / "output" / "node_modules" / "@playwright" / "mcp" / "cli.js"

# pydantic-ai's MCP init timeout defaults to 5s; a cold Node start can exceed that.
MCP_INIT_TIMEOUT_S = 60.0

# Set this truthy to watch the browser drive (debugging the Planner/Healer). The
# committed config stays headless for CI / Docker / other consumers.
HEADED_ENV_VAR = "PLAYWRIGHT_MCP_HEADED"


def _resolve_config_path() -> str:
    """Return the MCP config path, honoring ``PLAYWRIGHT_MCP_HEADED``.

    Default is the committed (headless) ``playwright-mcp-config.json``. When the env
    var is truthy, write a one-off headed copy to a temp file so you can watch the
    agent drive the browser — without mutating the committed config.
    """
    if os.environ.get(HEADED_ENV_VAR, "").strip().lower() not in {"1", "true", "yes", "on"}:
        return str(MCP_CONFIG_PATH)
    config = json.loads(MCP_CONFIG_PATH.read_text())
    config.setdefault("browser", {}).setdefault("launchOptions", {})["headless"] = False
    fd, path = tempfile.mkstemp(prefix="pwmcp-headed-", suffix=".json")
    with os.fdopen(fd, "w") as handle:
        json.dump(config, handle)
    atexit.register(_safe_unlink, path)
    return path


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def build_playwright_mcp(config: Config, storage_state: Path | None = None) -> MCPToolset:
    """Create an ``MCPToolset`` that runs Playwright MCP over stdio.

    Args:
        config: app config. Reserved for future extensibility (e.g. proxy
            settings); accepted now so the signature is stable across phases.
        storage_state: optional path to a Playwright ``storage_state.json`` so the
            browser starts pre-authenticated (see ``scripts/save_auth_state.py``).
            Avoids agents burning tokens re-logging-in on every run.

    Returns an ``MCPToolset`` to attach via ``Agent(model, toolsets=[...])``.

    Raises:
        RuntimeError: if the server CLI is not installed yet — run
            ``cd output && npm install``.
    """
    del config  # not used yet; kept in the signature for forward compatibility

    if not MCP_CLI_PATH.exists():
        raise RuntimeError(
            f"Playwright MCP server not found at {MCP_CLI_PATH}. "
            "Install it with `cd output && npm install` "
            "(@playwright/mcp is pinned in output/package.json)."
        )

    args = [
        str(MCP_CLI_PATH),
        "--config",
        _resolve_config_path(),
        "--isolated",
    ]
    if storage_state is not None:
        args.extend(["--storage-state", str(storage_state)])

    return MCPToolset(
        StdioTransport(command="node", args=args),
        init_timeout=MCP_INIT_TIMEOUT_S,
    )
