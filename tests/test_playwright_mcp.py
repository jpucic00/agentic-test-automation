"""Unit tests for build_playwright_mcp — offline (no MCP subprocess is started)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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


def test_mcp_output_dir_is_gitignored_output_runs():
    # output/runs/ is gitignored — keeps MCP artifacts out of the repo root and out of git.
    assert pm.MCP_OUTPUT_DIR.parts[-2:] == ("output", "runs")


def test_build_playwright_mcp_errors_clearly_when_cli_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "MCP_CLI_PATH", tmp_path / "missing" / "cli.js")
    with pytest.raises(RuntimeError, match="npm install"):
        pm.build_playwright_mcp(cast(Config, object()))


def test_build_playwright_mcp_constructs_node_toolset_when_cli_present(monkeypatch, tmp_path):
    cli = tmp_path / "cli.js"
    cli.write_text("// fake cli")
    monkeypatch.setattr(pm, "MCP_CLI_PATH", cli)
    toolset = pm.build_playwright_mcp(cast(Config, object()), storage_state=tmp_path / "state.json")
    assert isinstance(toolset, pm.AbstractToolset)


def test_resolve_config_headless_by_default(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_MCP_HEADED", raising=False)
    assert pm._resolve_config_path() == str(pm.MCP_CONFIG_PATH)


def test_resolve_config_headed_writes_temp_with_headless_false(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_MCP_HEADED", "1")
    path = pm._resolve_config_path()
    assert path != str(pm.MCP_CONFIG_PATH)
    data = json.loads(Path(path).read_text())
    assert data["browser"]["launchOptions"]["headless"] is False
    assert data["imageResponses"] == "omit"  # rest of the committed config preserved


def test_code_exec_tools_are_filtered_out():
    def keep(name: str) -> bool:
        return pm._agent_safe_tool(cast(Any, None), cast(Any, SimpleNamespace(name=name)))

    assert keep("browser_evaluate") is False
    assert keep("browser_run_code_unsafe") is False
    assert keep("browser_click") is True
    assert keep("browser_snapshot") is True
