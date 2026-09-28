"""Unit tests for the navigation allow-list (allowlist.py) and its MCP hook (agents/_nav_guard.py).

Fully local: the hook is driven with fake ``call_tool`` coroutines via ``asyncio.run`` (no
pytest-asyncio, no browser). Covers origin normalization, the refusal of off-list navigation
WITHOUT calling through, the off-list ``- Page URL:`` warning, composition with the
``LocatorFailureGuard``, the always-on wiring in ``build_playwright_mcp``, and the static
pre-run check of spec gotos + plan URLs.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic_ai.exceptions import ModelRetry

from ai_test_gen import allowlist, models
from ai_test_gen import playwright_mcp as pm
from ai_test_gen.agents._locator_steer import LOCATOR_TOOL, LocatorFailureGuard
from ai_test_gen.agents._nav_guard import NavigationGuard

STAGING = "https://staging.example.internal"
SSO = "https://sso.example.com"
ALLOWED = (STAGING, SSO)


class _Recorder:
    """A fake ``call_tool`` that records calls and returns a canned result."""

    def __init__(self, result: object = "ok") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result

    async def __call__(self, name, tool_args, *, metadata=None):
        self.calls.append((name, tool_args))
        return self.result


def _page(url: str) -> str:
    return (
        "### Ran Playwright code\n```js\nawait page.click();\n```\n"
        f"### Page\n- Page URL: {url}\n- Page Title: T"
    )


# --- origin normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://Staging.Example.internal/notes?x=1", STAGING),
        ("https://staging.example.internal:443/", STAGING),
        ("http://localhost:3000/login", "http://localhost:3000"),
        ("http://[::1]:8080/", "http://[::1]:8080"),
        ("data:text/html,hi", None),
        ("about:blank", None),
        ("https://evil.com\\@staging.example.internal", None),  # parser-confusion guard
        ("https://staging.example.internal:99999", None),
        ("/notes", None),
    ],
)
def test_url_origin_normalizes(url, origin):
    assert allowlist.url_origin(url) == origin


def test_userinfo_cannot_smuggle_an_allowed_host():
    assert allowlist.navigation_refusal("https://staging.example.internal@evil.com/", ALLOWED)


def test_relative_to_strips_only_app_origins():
    origins = (STAGING,)
    assert allowlist.relative_to(f"{STAGING}/notes?a=1#top", origins) == "/notes?a=1#top"
    assert allowlist.relative_to(STAGING, origins) == "/"
    assert allowlist.relative_to(f"{SSO}/login", origins) == f"{SSO}/login"


# --- the hook: refuse off-list navigation ------------------------------------------


def test_refuses_offlist_navigate_without_calling_the_tool(caplog):
    guard = NavigationGuard(ALLOWED)
    call = _Recorder()
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(guard(None, call, "browser_navigate", {"url": "https://www.google.com"}))
    assert call.calls == []  # never reached the browser
    assert isinstance(out, str)
    assert "NAVIGATION REFUSED" in out and "Do NOT retry" in out
    assert STAGING in out and SSO in out  # names the allowed hosts
    assert "refused browser_navigate" in caplog.text


@pytest.mark.parametrize(
    "url",
    [f"{STAGING}/notes", f"{SSO}/realms/x", "staging.example.internal/notes", "about:blank"],
)
def test_allows_listed_urls_and_about_blank(url):
    call = _Recorder()
    out = asyncio.run(NavigationGuard(ALLOWED)(None, call, "browser_navigate", {"url": url}))
    assert out == "ok"
    assert call.calls == [("browser_navigate", {"url": url})]


@pytest.mark.parametrize("url", ["data:text/html,<h1>x</h1>", "file:///etc/passwd", "/notes"])
def test_refuses_non_http_and_relative_targets(url):
    call = _Recorder()
    out = asyncio.run(NavigationGuard(ALLOWED)(None, call, "browser_navigate", {"url": url}))
    assert call.calls == []
    assert "NAVIGATION REFUSED" in str(out)


def test_browser_tabs_new_with_offlist_url_is_refused_but_other_actions_pass():
    guard = NavigationGuard(ALLOWED)
    call = _Recorder()
    out = asyncio.run(guard(None, call, "browser_tabs", {"action": "new", "url": "https://x.io"}))
    assert call.calls == [] and "NAVIGATION REFUSED" in str(out)
    asyncio.run(guard(None, call, "browser_tabs", {"action": "new"}))  # blank tab
    asyncio.run(guard(None, call, "browser_tabs", {"action": "select", "index": 1}))
    assert [c[1]["action"] for c in call.calls] == ["new", "select"]


# --- the hook: flag an off-list current page ---------------------------------------


def test_warns_when_a_click_lands_offlist(caplog):
    call = _Recorder(_page("https://accounts.google.com/signin"))
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(NavigationGuard(ALLOWED)(None, call, "browser_click", {"ref": "e3"}))
    assert isinstance(out, str)
    assert out.startswith(_page("https://accounts.google.com/signin"))  # original kept
    assert "OUTSIDE the allowed hosts" in out and "navigate back" in out
    assert "off-list page https://accounts.google.com/signin" in caplog.text


def test_warning_appended_to_list_results():
    call = _Recorder([_page("https://x.io/"), {"type": "image"}])
    out = asyncio.run(NavigationGuard(ALLOWED)(None, call, "browser_click", {}))
    assert isinstance(out, list) and len(out) == 3
    assert "OUTSIDE the allowed hosts" in out[-1]


@pytest.mark.parametrize(
    "url", [f"{STAGING}/notes", "about:blank", "chrome-error://chromewebdata/"]
)
def test_no_warning_on_allowed_or_inert_pages(url):
    call = _Recorder(_page(url))
    out = asyncio.run(NavigationGuard(ALLOWED)(None, call, "browser_click", {}))
    assert out == _page(url)


# --- composition with the locator guard + always-on wiring --------------------------


async def _fail(name, tool_args, *, metadata=None):
    raise ModelRetry("ref e7 not found")


def test_composes_with_locator_guard():
    inner = LocatorFailureGuard(ceiling=1)
    guard = NavigationGuard(ALLOWED, inner=inner)
    # The inner guard still sees (and soft-lands) the locator tool's failure …
    out = asyncio.run(guard(None, _fail, LOCATOR_TOOL, {"element": "x"}))
    assert "STOP calling it" in str(out)
    # … while an off-list navigation is refused before the inner hook or the tool run.
    call = _Recorder()
    out = asyncio.run(guard(None, call, "browser_navigate", {"url": "https://x.io"}))
    assert call.calls == [] and "NAVIGATION REFUSED" in str(out)


def test_build_playwright_mcp_always_wraps_the_callers_hook(cfg, monkeypatch, tmp_path):
    cli = tmp_path / "cli.js"
    cli.write_text("// fake cli")
    monkeypatch.setattr(pm, "MCP_CLI_PATH", cli)
    captured: dict = {}
    real = pm.MCPToolset

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pm, "MCPToolset", spy)
    inner = LocatorFailureGuard(ceiling=5)
    pm.build_playwright_mcp(cfg, process_tool_call=inner)
    hook = captured["process_tool_call"]
    assert isinstance(hook, NavigationGuard)
    assert hook.inner is inner
    assert hook.allowed_origins == cfg.allowed_origins

    pm.build_playwright_mcp(cfg)  # no caller hook → the guard alone, still installed
    assert isinstance(captured["process_tool_call"], NavigationGuard)


# --- static pre-run check -----------------------------------------------------------


def _check(code, plan=None, base_url=STAGING, envs=(STAGING, "https://qa2.example.internal")):
    return allowlist.preflight_violations(
        code, plan, base_url=base_url, env_origins=envs, extra_origins=(SSO,)
    )


def test_relative_and_listed_gotos_pass():
    code = (
        "await page.goto('/');\nawait page.goto(\"/notes\");\n"
        f"await page.goto('{STAGING}/login');\nawait page.goto(`{SSO}/auth`);\n"
        "await page.goto(`${BASE}/x`);\nawait page.goto('about:blank');"
    )
    assert _check(code) == []


def test_offlist_absolute_goto_is_a_violation():
    violations = _check("await page.goto('https://www.example.com/');")
    assert len(violations) == 1 and "https://www.example.com" in violations[0]


def test_protocol_relative_and_non_http_gotos_are_violations():
    assert _check("await page.goto('//evil.com/x');")
    assert _check("await page.goto('data:text/html,hi');")


@pytest.mark.parametrize(
    "goto",
    [
        r"await page.goto('/\\evil.com');",  # JS value /\evil.com — WHATWG reads //evil.com
        r"await page.goto('\/\/evil.com');",  # JS value //evil.com
        r"await page.goto('\x68ttps://evil.com');",  # JS value https://evil.com
        "await page.goto('/\t/evil.com');",  # URL parsing drops the tab
    ],
)
def test_escaped_or_control_char_goto_is_a_violation(goto):
    # The check reads source text, the browser reads the decoded string: anything that
    # makes those differ must be refused, or a "relative" path lands on another host.
    assert _check(goto)


def test_whitespace_before_goto_paren_is_still_checked():
    assert _check("await page.goto ('https://evil.com/');")


def test_primary_origin_hard_coded_is_blocked_on_a_secondary_environment():
    code = f"await page.goto('{STAGING}/');"
    assert _check(code) == []  # fine on the primary
    violations = _check(code, base_url="https://qa2.example.internal")
    assert len(violations) == 1 and "another configured environment" in violations[0]


def test_plan_urls_are_checked_against_the_full_allow_list():
    plan = models.TestPlan(
        test_case_key="QA-1", title="t", target_url=f"{STAGING}/",
        steps=[
            models.PlanStep(action="a", page_url=f"{SSO}/login"),
            models.PlanStep(action="b", page_url="https://tracker.io/p"),
        ],
    )
    # Recorded on the primary, so a secondary run still accepts the primary origin.
    violations = _check("", plan, base_url="https://qa2.example.internal")
    assert violations == [
        "plan steps[1].page_url 'https://tracker.io/p' is on https://tracker.io, "
        "which is not an allowed host"
    ]
