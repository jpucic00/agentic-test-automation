"""Unit tests for the DOM Probe (agents/_dom_probe.py) — fully local (no network, no browser).

Covers the fixed-JS parameter embedding (model input is DATA, never code), the per-run budget,
the direct ``browser_evaluate`` dispatch (mocked), snapshot-stripping + size-capping of results,
error degradation, and the gating that registers ``probe_dom`` on the Planner/Healer only when
``AGENT_DOM_PROBE`` is on (disabled runs stay byte-identical). Coroutines run via ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from ai_test_gen import models
from ai_test_gen.agents import _dom_probe as dom_probe_mod
from ai_test_gen.agents import healer as healer_mod
from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents._dom_probe import (
    PROBE_TOOL,
    _clean,
    build_probe_js,
    register_probe_dom,
)


class _RecordingMcp:
    """A fake underlying MCP toolset exposing direct_call_tool, recording every call."""

    def __init__(self, result="### Result\n{\"matchCount\": 1}"):
        self.calls: list[tuple[str, dict]] = []
        self._result = result

    async def direct_call_tool(self, name, args):
        self.calls.append((name, args))
        return self._result


def _probe_cfg(cfg, max_calls=2):
    return dataclasses.replace(cfg, dom_probe_max_calls=max_calls)


def _agent():
    return Agent(model=TestModel(), output_type=models.TestPlan)


# --- fixed JS: model inputs are data, never code --------------------------------


def test_build_probe_js_embeds_query_json_escaped():
    hostile = 'say "hi" </script> \\ or (alert(1))'
    js = build_probe_js(hostile, None)
    assert "__QUERY__" not in js and "__SCOPE__" not in js  # placeholders replaced
    assert json.dumps(hostile) in js  # embedded as an escaped string literal…
    assert f'= {hostile}' not in js  # …never as raw syntax
    assert "const QUERY" in js and "const SCOPE = null;" in js  # scope None -> JS null


def test_build_probe_js_embeds_scope_when_given():
    js = build_probe_js("Save", 'div[role="dialog"]')
    assert json.dumps('div[role="dialog"]') in js


def test_probe_js_is_read_only():
    # The fixed function must never mutate the page: no clicks, writes, or event dispatch.
    for forbidden in (".click(", ".submit(", "dispatchEvent", "innerHTML =", ".value =",
                      "setAttribute", "removeChild", "appendChild"):
        assert forbidden not in dom_probe_mod._PROBE_JS_TEMPLATE, forbidden


# --- dispatch, budget, and degradation ------------------------------------------


def test_probe_dom_dispatches_browser_evaluate_with_fixed_js(cfg):
    mcp = _RecordingMcp()
    tool = register_probe_dom(_agent(), _probe_cfg(cfg), mcp)
    out = asyncio.run(tool("Speichern", 'div[role="dialog"]'))
    assert "matchCount" in out
    (name, args), = mcp.calls
    assert name == PROBE_TOOL
    assert json.dumps("Speichern") in args["function"]  # the model's text rode along as data
    assert "querySelectorAll" in args["function"]  # …inside the fixed probe function


def test_probe_dom_enforces_per_run_budget(cfg):
    mcp = _RecordingMcp()
    tool = register_probe_dom(_agent(), _probe_cfg(cfg, max_calls=1), mcp)
    assert "matchCount" in asyncio.run(tool("Save"))  # within budget
    out = asyncio.run(tool("Save"))  # exceeds budget of 1
    assert "budget" in out.lower()
    assert len(mcp.calls) == 1  # the second call never reached the browser


def test_probe_dom_degrades_when_evaluate_fails(cfg):
    class _Broken(_RecordingMcp):
        async def direct_call_tool(self, name, args):
            raise RuntimeError("evaluate not supported")

    tool = register_probe_dom(_agent(), _probe_cfg(cfg), _Broken())
    out = asyncio.run(tool("Save"))
    assert "probe_dom failed" in out  # degraded to a message, no exception escaped


def test_probe_dom_unavailable_without_direct_call_path(cfg):
    # A toolset with no direct_call_tool anywhere in its wrapper chain -> graceful message.
    tool = register_probe_dom(_agent(), _probe_cfg(cfg), object())
    out = asyncio.run(tool("Save"))
    assert "unavailable" in out.lower()


# --- result hygiene: snapshot stripped, size capped ------------------------------


def test_clean_strips_trailing_page_snapshot():
    raw = (
        '### Result\n{"matchCount": 2}\n\n'
        "### Page state\n- Page Snapshot\n- generic:\n  - text: x"
    )
    out = _clean(raw)
    assert '{"matchCount": 2}' in out
    assert "generic" not in out  # everything from the snapshot marker on is gone


def test_clean_caps_result_size():
    out = _clean("x" * 10_000)
    assert len(out) <= dom_probe_mod._RESULT_CHAR_CAP + 40
    assert out.endswith("…[probe result truncated]")


def test_clean_joins_content_item_lists():
    class _Item:
        def __init__(self, text):
            self.text = text

    assert _clean([_Item("part-a"), {"text": "part-b"}]) == "part-a\npart-b"


# --- gating: registered on both agents only when enabled -------------------------


def test_planner_registers_probe_only_when_enabled(cfg, monkeypatch):
    seen: list[int] = []
    real = planner_mod._register_probe_dom

    def spy(agent, config, toolset, agent_label="Planner"):
        seen.append(config.dom_probe_max_calls)
        return real(agent, config, toolset, agent_label)

    monkeypatch.setattr(planner_mod, "_register_probe_dom", spy)
    planner_mod.build_planner(cfg)  # probe off (dom_probe_max_calls == 0)
    assert seen == []
    planner_mod.build_planner(dataclasses.replace(cfg, dom_probe_max_calls=5))  # on
    assert seen == [5]


def test_healer_registers_probe_only_when_enabled(cfg, monkeypatch):
    seen: list[int] = []
    real = healer_mod.register_probe_dom

    def spy(agent, config, toolset, agent_label="Planner"):
        seen.append(config.dom_probe_max_calls)
        return real(agent, config, toolset, agent_label)

    monkeypatch.setattr(healer_mod, "register_probe_dom", spy)
    healer_mod.build_healer(cfg)  # off
    assert seen == []
    healer_mod.build_healer(dataclasses.replace(cfg, dom_probe_max_calls=5))  # on
    assert seen == [5]


def test_prompt_carries_probe_fragment_only_when_enabled(cfg, monkeypatch):
    captured: dict[str, str] = {}
    real = planner_mod.assemble_system_prompt

    def spy(config, base_prompt, *, include_map=True):
        captured["base"] = base_prompt
        return real(config, base_prompt, include_map=include_map)

    monkeypatch.setattr(planner_mod, "assemble_system_prompt", spy)
    planner_mod.build_planner(cfg)  # off
    assert "DOM probe" not in captured["base"]
    planner_mod.build_planner(dataclasses.replace(cfg, dom_probe_max_calls=5))  # on
    assert "DOM probe" in captured["base"]
    assert "probe_dom" in captured["base"]


def test_probe_fragment_demands_verification():
    # The gated fragment must keep "verify before trust": candidates are recon, and every one
    # is verified via browser_generate_locator before being recorded.
    fragment = (planner_mod.PROMPTS_DIR / "dom_probe.md").read_text()
    assert "browser_generate_locator" in fragment
    assert "RECONNAISSANCE" in fragment
    assert "NEVER record an unverified candidate" in fragment


def test_probe_tool_docstring_demands_verification(cfg):
    tool = register_probe_dom(_agent(), _probe_cfg(cfg), _RecordingMcp())
    doc = tool.__doc__ or ""
    assert "browser_generate_locator" in doc
    assert "UNVERIFIED" in doc
