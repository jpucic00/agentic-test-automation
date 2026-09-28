"""Unit tests for count_matches (agents/_match_count.py) — fully local (no network, no browser).

Covers selector normalization (engine prefixes, XPath auto-detection, ``locator('…')``
unwrapping, Playwright-only syntax rejected), the fixed-JS embedding (the selector is DATA, never
code), result parsing + verdict wording, the direct ``browser_evaluate`` dispatch (mocked), error
degradation, and the always-on registration on the Planner and Healer. Coroutines run via
``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from ai_test_gen import models
from ai_test_gen.agents import _match_count as match_mod
from ai_test_gen.agents import healer as healer_mod
from ai_test_gen.agents import planner as planner_mod
from ai_test_gen.agents._match_count import (
    UnsupportedSelector,
    build_count_js,
    format_count,
    parse_count_result,
    parse_selector,
    register_count_matches,
)


def _evaluate_result(payload: dict) -> str:
    # What @playwright/mcp's browser_evaluate renders: the returned string, JSON-encoded, under
    # "### Result", followed by the code it ran.
    return f"### Result\n{json.dumps(json.dumps(payload))}\n### Ran Playwright code\n```js\n…\n```"


class _RecordingMcp:
    """A fake underlying MCP toolset exposing direct_call_tool, recording every call."""

    def __init__(self, payload: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._result = _evaluate_result(payload or {"engine": "css", "count": 1, "visible": 1})

    async def direct_call_tool(self, name, args):
        self.calls.append((name, args))
        return self._result


def _agent():
    return Agent(model=TestModel(), output_type=models.TestPlan)


# --- selector normalization -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("xpath=//div[@id='a']", ("xpath", "//div[@id='a']")),
        ("//button[normalize-space()='Save']", ("xpath", "//button[normalize-space()='Save']")),
        ("(//span)[2]", ("xpath", "(//span)[2]")),
        ("css=div.logout", ("css", "div.logout")),
        ("[name=\"email\"]", ("css", "[name=\"email\"]")),
        ("#menu > .item", ("css", "#menu > .item")),
        ("  css=  a.b  ", ("css", "a.b")),
    ],
)
def test_parse_selector_engines(raw, expected):
    assert parse_selector(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("locator('xpath=//div[@id=\\'menu\\']')", ("xpath", "//div[@id='menu']")),
        ('page.locator("css=[name=\\"q\\"]")', ("css", '[name="q"]')),
        ("locator('//li[text()=\"Log out\"]')", ("xpath", '//li[text()="Log out"]')),
        ("locator(`div.x`)", ("css", "div.x")),
    ],
)
def test_parse_selector_unwraps_locator_wrapper(raw, expected):
    assert parse_selector(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "getByRole('button', { name: 'Save', exact: true })",
        "locator('div.x').first()",
        "page.getByTestId('login')",
        "text=Save",
        "id=login",
        "internal:role=button",
        "div >> text=Save",
        "button:has-text('Save')",
        "li:visible",
        "",
        "locator('')",
    ],
)
def test_parse_selector_rejects_playwright_only_syntax(raw):
    with pytest.raises(UnsupportedSelector):
        parse_selector(raw)


# --- fixed JS: the selector is data, never code ---------------------------------


def test_build_count_js_embeds_selector_json_escaped():
    hostile = 'a[title="</script>"] \\ \'x\' ${alert(1)}'
    js = build_count_js("css", hostile)
    assert "__ENGINE__" not in js and "__EXPR__" not in js  # placeholders replaced
    assert f"const EXPR = {json.dumps(hostile)};" in js  # an escaped string literal…
    assert f"= {hostile}" not in js  # …never raw syntax
    assert 'const ENGINE = "css";' in js


def test_count_js_is_read_only():
    for forbidden in (".click(", ".submit(", "dispatchEvent", "innerHTML =", ".value =",
                      "setAttribute", "removeChild", "appendChild", "focus("):
        assert forbidden not in match_mod._COUNT_JS_TEMPLATE, forbidden


# --- result parsing + verdicts ---------------------------------------------------


def test_parse_count_result_decodes_evaluate_output():
    raw = _evaluate_result({"engine": "xpath", "count": 3, "visible": 2})
    assert parse_count_result(raw) == {"engine": "xpath", "count": 3, "visible": 2}


def test_parse_count_result_accepts_content_item_lists():
    raw = [{"text": _evaluate_result({"count": 1, "visible": 1})}]
    assert parse_count_result(raw) == {"count": 1, "visible": 1}


def test_parse_count_result_returns_none_on_garbage():
    assert parse_count_result("### Error\nsomething broke") is None


@pytest.mark.parametrize(
    "payload",
    [{"count": None, "visible": 0}, {"count": "3", "visible": 1}, {"count": 1}, {"count": True}],
)
def test_parse_count_result_rejects_non_integer_counts(payload):
    # A page script can patch the builtins the count uses; a bad payload must degrade to
    # "unreadable", never raise out of the tool and abort the agent run.
    assert parse_count_result(_evaluate_result(payload)) is None


def test_format_count_verdicts():
    assert "exactly 1 element" in format_count("css=a", {"count": 1, "visible": 1})
    assert "unique" in format_count("css=a", {"count": 1, "visible": 0})
    none = format_count("css=a", {"count": 0, "visible": 0})
    assert "0 elements" in none
    many = format_count("css=a", {"count": 3, "visible": 1})
    assert "3 elements" in many and "NOT unique" in many and "hidden" in many
    assert "truncated" in format_count("css=a", {"count": 1, "visible": 1, "truncated": True})
    err = format_count("div[", {"error": "invalid CSS selector: bad"})
    assert "could not evaluate" in err and "invalid CSS selector" in err


# --- dispatch + degradation ------------------------------------------------------


def test_count_matches_dispatches_browser_evaluate_with_fixed_js():
    mcp = _RecordingMcp({"engine": "xpath", "count": 1, "visible": 1})
    tool = register_count_matches(_agent(), mcp)
    out = asyncio.run(tool("locator('xpath=//div[@id=\"menu\"]')"))
    assert "exactly 1 element" in out
    (name, args), = mcp.calls
    assert name == "browser_evaluate"
    assert 'const ENGINE = "xpath";' in args["function"]
    assert json.dumps('//div[@id="menu"]') in args["function"]  # unwrapped, then embedded


def test_count_matches_rejects_unsupported_without_calling_browser():
    mcp = _RecordingMcp()
    tool = register_count_matches(_agent(), mcp)
    out = asyncio.run(tool("getByRole('button', { name: 'Save' })"))
    assert "cannot evaluate" in out and "raw CSS or XPath" in out
    assert mcp.calls == []


def test_count_matches_degrades_when_evaluate_fails():
    class _Broken(_RecordingMcp):
        async def direct_call_tool(self, name, args):
            raise RuntimeError("evaluate not supported")

    tool = register_count_matches(_agent(), _Broken())
    out = asyncio.run(tool("css=a"))
    assert "count_matches failed" in out  # degraded to a message, no exception escaped


def test_count_matches_reports_unreadable_result():
    class _Odd(_RecordingMcp):
        async def direct_call_tool(self, name, args):
            return "### Error\nPage closed"

    out = asyncio.run(register_count_matches(_agent(), _Odd())("css=a"))
    assert "unreadable" in out and "Page closed" in out


def test_count_matches_unavailable_without_direct_call_path():
    tool = register_count_matches(_agent(), object())
    assert "unavailable" in asyncio.run(tool("css=a")).lower()


# --- always registered on both browser agents ------------------------------------


@pytest.mark.parametrize(
    ("mod", "build", "label"),
    [(planner_mod, "build_planner", "Planner"), (healer_mod, "build_healer", "Healer")],
)
def test_browser_agents_always_register_count_matches(cfg, monkeypatch, mod, build, label):
    seen: list[str] = []
    real = mod.register_count_matches

    def spy(agent, toolset, agent_label="Planner"):
        seen.append(agent_label)
        return real(agent, toolset, agent_label)

    monkeypatch.setattr(mod, "register_count_matches", spy)
    getattr(mod, build)(cfg)  # default config: vision + probe off — still registered
    assert seen == [label]
