"""Unit tests for build_playwright_mcp — offline (no MCP subprocess is started)."""
from __future__ import annotations

from typing import cast

import pytest

from ai_test_gen import playwright_mcp as pm
from ai_test_gen.config import Config


def test_mcp_version_pinned_not_latest():
    assert pm.PLAYWRIGHT_MCP_VERSION == "0.0.75"
    assert "latest" not in pm.PLAYWRIGHT_MCP_PACKAGE


def test_mcp_cli_path_points_at_local_node_install():
    parts = pm.MCP_CLI_PATH.parts
    assert parts[-3:] == ("@playwright", "mcp", "cli.js")
    assert "output" in parts and "node_modules" in parts


def test_build_playwright_mcp_errors_clearly_when_cli_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "MCP_CLI_PATH", tmp_path / "missing" / "cli.js")
    with pytest.raises(RuntimeError, match="npm install"):
        pm.build_playwright_mcp(cast(Config, object()))


def test_build_playwright_mcp_constructs_node_toolset_when_cli_present(monkeypatch, tmp_path):
    cli = tmp_path / "cli.js"
    cli.write_text("// fake cli")
    monkeypatch.setattr(pm, "MCP_CLI_PATH", cli)
    toolset = pm.build_playwright_mcp(cast(Config, object()), storage_state=tmp_path / "state.json")
    assert isinstance(toolset, pm.MCPToolset)
