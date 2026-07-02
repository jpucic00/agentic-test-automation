"""Unit tests for build_playwright_mcp.

Offline (no network). One exception to "no subprocess": the live schema-scan test starts
the LOCAL node MCP server to list its tool schemas; it skips when output/node_modules is
not installed.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ai_test_gen import models
from ai_test_gen import playwright_mcp as pm
from ai_test_gen.config import Config


def test_mcp_version_pinned_not_latest():
    assert pm.PLAYWRIGHT_MCP_VERSION == "0.0.75"
    assert "latest" not in pm.PLAYWRIGHT_MCP_PACKAGE


def test_mcp_cli_path_points_at_local_node_install():
    parts = pm.MCP_CLI_PATH.parts
    assert parts[-3:] == ("@playwright", "mcp", "cli.js")
    assert "output" in parts and "node_modules" in parts


def test_mcp_output_dir_is_output_snapshots():
    # output/snapshots/ holds MCP artifacts; its contents are gitignored and wiped each run.
    assert pm.MCP_OUTPUT_DIR.parts[-2:] == ("output", "snapshots")


def test_build_playwright_mcp_runs_subprocess_in_snapshots_dir(monkeypatch, tmp_path):
    # The MCP subprocess cwd is pinned to the snapshots dir so cwd-relative artifacts
    # (screenshots/pngs the server writes outside --output-dir) don't escape to the repo root.
    cli = tmp_path / "cli.js"
    cli.write_text("// fake cli")
    out = tmp_path / "snapshots"
    monkeypatch.setattr(pm, "MCP_CLI_PATH", cli)
    monkeypatch.setattr(pm, "MCP_OUTPUT_DIR", out)
    captured: dict = {}
    real = pm.StdioTransport

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(pm, "StdioTransport", spy)
    pm.build_playwright_mcp(cast(Config, object()))
    assert captured["cwd"] == str(out)


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


def test_testing_capability_tools_pass_the_filter():
    # The `testing` capability exposes browser_generate_locator (the Planner's verified-selector
    # tool) + browser_verify_* assertions. None are code-exec, so the safety filter must KEEP them.
    def keep(name: str) -> bool:
        return pm._agent_safe_tool(cast(Any, None), cast(Any, SimpleNamespace(name=name)))

    assert keep("browser_generate_locator") is True
    assert keep("browser_verify_element_visible") is True
    assert keep("browser_verify_text_visible") is True
    assert keep("browser_verify_value") is True


def test_committed_config_enables_testing_caps_and_id_testid():
    # browser_generate_locator lives in the `testing` capability; testIdAttribute='id' makes it
    # emit getByTestId('x') sourced from the app's manually-written id="x".
    data = json.loads(pm.MCP_CONFIG_PATH.read_text())
    assert data["capabilities"] == ["testing"]
    assert data["testIdAttribute"] == "id"


def test_generated_runner_maps_testid_to_id():
    # The generated .spec.ts runs under output/playwright.config.ts; getByTestId('x') resolves to
    # [id="x"] ONLY because that runner also sets testIdAttribute:'id'. Keep it in sync with the
    # MCP config (test above) or every id-based locator fails at runtime.
    runner = pm.PROJECT_ROOT / "output" / "playwright.config.ts"
    assert "testIdAttribute: 'id'" in runner.read_text()


def test_testid_attribute_coupling_holds():
    # Load-bearing invariant: the MCP read side (playwright-mcp-config.json) and the runner side
    # (output/playwright.config.ts) must map the app's id= to the SAME test id, or every
    # getByTestId locator silently fails at runtime. The two single-sided tests above each assert
    # their own value is 'id' but can't catch DRIFT — this asserts the two are equal to each other.
    mcp_testid = json.loads(pm.MCP_CONFIG_PATH.read_text())["testIdAttribute"]
    runner_src = (pm.PROJECT_ROOT / "output" / "playwright.config.ts").read_text()
    match = re.search(r"testIdAttribute:\s*'([^']+)'", runner_src)
    assert match is not None, "runner config is missing testIdAttribute"
    assert match.group(1) == mcp_testid


def test_grammar_unsafe_tools_are_filtered_out():
    # Exact-name drops for strict structured-output gateways (vLLM+xgrammar): browser_drop
    # (propertyNames) and browser_network_request (minimum/maximum). Near-names must survive —
    # the ban is exact, not a substring like the code-exec markers.
    def keep(name: str) -> bool:
        return pm._agent_safe_tool(cast(Any, None), cast(Any, SimpleNamespace(name=name)))

    assert keep("browser_drop") is False
    assert keep("browser_network_request") is False
    assert keep("browser_drag") is True
    assert keep("browser_network_requests") is True
    assert keep("browser_click") is True


# JSON-Schema constructs vLLM's xgrammar backend cannot compile (curated from its supported
# subset; matches the constructs that broke the corporate gateway and, earlier, llama.cpp).
XGRAMMAR_UNSUPPORTED_KEYS = frozenset({
    "propertyNames", "patternProperties", "unevaluatedProperties",
    "if", "then", "else", "not", "allOf", "oneOf",
    "uniqueItems", "contains", "multipleOf",
    "dependentRequired", "dependentSchemas",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
})


def _unsupported_paths(
    schema: object, path: str = "", *, keys_are_names: bool = False
) -> list[str]:
    """Paths of xgrammar-unsupported constructs in a JSON schema (empty = grammar-clean).

    ``keys_are_names`` suppresses matching one level under ``properties``/``$defs``, where dict
    keys are user-chosen names (a property literally called "minimum" is not a constraint).
    """
    hits: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            sub = f"{path}.{key}" if path else str(key)
            if not keys_are_names and key in XGRAMMAR_UNSUPPORTED_KEYS:
                hits.append(sub)
            hits += _unsupported_paths(
                value, sub, keys_are_names=key in {"properties", "$defs", "definitions"}
            )
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            hits += _unsupported_paths(value, f"{path}[{i}]")
    return hits


def test_testplan_output_schema_is_grammar_clean():
    # The Planner's structured-output tool (TestPlan) is advertised to the gateway alongside the
    # MCP tools — it must stay compilable by strict backends too.
    assert _unsupported_paths(models.TestPlan.model_json_schema()) == []


@pytest.mark.skipif(
    not pm.MCP_CLI_PATH.exists(), reason="MCP server not installed (cd output && npm install)"
)
def test_advertised_mcp_tool_schemas_are_grammar_clean():
    # Regression net for @playwright/mcp bumps: list the LIVE tool schemas, apply the agent
    # filter, and require every schema the model would actually be offered to be free of
    # xgrammar-unsupported constructs. Also require the known offenders to still exist upstream —
    # if a bump renames/removes them, _GRAMMAR_UNSAFE_TOOLS needs updating, not silence.
    async def live_tools():
        toolset = pm.MCPToolset(
            pm.StdioTransport(
                command="node",
                args=[str(pm.MCP_CLI_PATH), "--config", str(pm.MCP_CONFIG_PATH)],
                keep_alive=False,
            ),
            init_timeout=pm.MCP_INIT_TIMEOUT_S,
        )
        async with toolset:
            for attr in ("direct_list_tools", "list_tools"):
                lister = getattr(toolset, attr, None)
                if lister is not None:
                    return await lister()
            raise AssertionError("no tool-listing method on MCPToolset")

    tools = asyncio.run(live_tools())
    names = {tool.name for tool in tools}
    assert pm._GRAMMAR_UNSAFE_TOOLS <= names, (
        f"filter list out of date: {sorted(pm._GRAMMAR_UNSAFE_TOOLS - names)} no longer "
        "advertised by @playwright/mcp"
    )
    kept = [
        tool
        for tool in tools
        if pm._agent_safe_tool(cast(Any, None), cast(Any, SimpleNamespace(name=tool.name)))
    ]
    offending = {
        tool.name: paths for tool in kept if (paths := _unsupported_paths(tool.inputSchema))
    }
    assert offending == {}, f"grammar-unsafe schemas advertised to the model: {offending}"
