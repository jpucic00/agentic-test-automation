"""``count_matches``: prove an authored CSS/XPath selector matches exactly ONE element.

The browser agents verify a CSS/XPath they author (resilience-ladder rungs 3–4) by passing it raw
as ``browser_generate_locator``'s ``target``. That call errors on 0 matches but NOT on duplicates —
for a non-ref target @playwright/mcp resolves via ``page.$(selector)``, which silently takes the
first match — and the ``browser_verify_*`` tools accept only role+name / text, never a selector.
So nothing in the MCP tool set proves uniqueness read-only, and a duplicate-matching selector
passes planning only to trip ``strict mode violation … resolved N elements`` at run time.

``count_matches(selector)`` closes that gap with the DOM probe's pattern (``dom_probe.py``): it
executes ONE fixed, pipeline-authored, READ-ONLY JS function via
``direct_call_tool("browser_evaluate", ...)``. The model supplies only DATA — the selector,
normalized in Python to an (engine, expression) pair and embedded JSON-escaped into the constant
function; it can never inject code. Always registered on the Planner and Healer (no env knob):
it is cheap, read-only, and every authored selector needs it. No per-run cap — a runaway loop
is still bounded by ``AGENT_REQUEST_LIMIT``.

Semantics mirror Playwright's page-level locators as closely as plain DOM APIs allow:

- ``xpath=…``, bare ``//…`` / ``(//…)`` / ``..`` → ``document.evaluate`` (element nodes only).
  Like Playwright's XPath engine, it does not pierce shadow roots.
- ``css=…`` or plain CSS → ``querySelectorAll`` on the document AND every open shadow root, the
  way Playwright's ``css=`` pierces open shadow DOM. Caveat: a combinator cannot cross a shadow
  boundary here (``#host .inner`` with ``.inner`` inside ``#host``'s shadow root counts 0), which
  Playwright's engine can.
- A ``locator('…')`` / ``page.locator("…")`` wrapper is unwrapped. Playwright-only syntax
  (``getBy*``, chained calls, ``>>``, ``internal:``/``text=``/other engines, ``:has-text()`` and
  friends) is reported as unsupported rather than guessed at.
- Same document only (like a page-level locator): iframe contents are not counted.

Hidden matches are reported separately but still count — Playwright's strict mode counts them too.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Any, Literal

from pydantic_ai import Agent

from .mcp_direct import EVALUATE_TOOL, clean_result, result_text, underlying_mcp

logger = logging.getLogger(__name__)

__all__ = ["UnsupportedSelector", "build_count_js", "parse_selector", "register_count_matches"]

Engine = Literal["css", "xpath"]

# page.locator('…') / locator("…") / locator(`…`) — the whole string, nothing chained after it.
_WRAPPER_RE = re.compile(r"^(?:page\.)?locator\(\s*(['\"`])(?P<body>.*)\1\s*\)$", re.DOTALL)
# A Playwright selector-engine prefix such as `xpath=`, `css=`, `text=`, `id=`, `data-testid=`.
_ENGINE_RE = re.compile(r"^(?P<engine>[A-Za-z_][\w-]*)=")
# Playwright auto-detects XPath for selectors starting with `//` or `..` (optionally parenthesized).
_XPATH_AUTO_RE = re.compile(r"^\(*(?://|\.\.)")
# Playwright-only CSS extensions that querySelectorAll cannot evaluate.
_PLAYWRIGHT_PSEUDOS = (
    ":has-text(", ":text(", ":text-is(", ":text-matches(", ":visible", ":nth-match(",
    ":left-of(", ":right-of(", ":above(", ":below(", ":near(",
)


class UnsupportedSelector(ValueError):
    """The selector uses syntax count_matches cannot evaluate with plain DOM APIs."""


def parse_selector(raw: str) -> tuple[Engine, str]:
    """Normalize a model-supplied selector to ``(engine, expression)``.

    Unwraps a ``locator('…')`` wrapper, strips an ``xpath=`` / ``css=`` prefix, auto-detects
    XPath like Playwright does, and raises ``UnsupportedSelector`` (with a model-readable reason)
    for Playwright-only syntax.
    """
    text = raw.strip()
    wrapped = _WRAPPER_RE.match(text)
    if wrapped:
        # Undo JS string-literal escaping (\' \" \\) inside the wrapper.
        text = re.sub(r"\\(.)", r"\1", wrapped.group("body")).strip()
    if not text:
        raise UnsupportedSelector("empty selector")
    if text.startswith(("getBy", "page.", "locator(")) or ").locator(" in text:
        raise UnsupportedSelector(
            "Playwright locator expressions (getBy*, chained calls like .first()/.filter()) are "
            "not supported — pass a raw CSS or XPath selector"
        )
    if text.startswith("internal:"):
        raise UnsupportedSelector("Playwright `internal:` selectors are not supported")
    engine: Engine
    engine_match = _ENGINE_RE.match(text)
    if engine_match:
        name = engine_match.group("engine").lower()
        if name not in ("css", "xpath"):
            raise UnsupportedSelector(
                f"the Playwright `{name}=` engine is not supported — pass a raw CSS or XPath "
                "selector"
            )
        engine = "xpath" if name == "xpath" else "css"
        text = text[engine_match.end():].strip()
    elif _XPATH_AUTO_RE.match(text):
        engine = "xpath"
    else:
        engine = "css"
    if not text:
        raise UnsupportedSelector("empty selector")
    if ">>" in text:
        raise UnsupportedSelector("Playwright `>>` selector chaining is not supported")
    if engine == "css":
        lowered = text.lower()
        for pseudo in _PLAYWRIGHT_PSEUDOS:
            if pseudo in lowered:
                raise UnsupportedSelector(
                    f"the Playwright-only pseudo-class `{pseudo.rstrip('(')}` is not supported"
                )
    return engine, text


# The fixed, read-only counting function. __ENGINE__ / __EXPR__ are replaced with JSON-encoded
# values (see build_count_js) — the model's selector is data inside a string literal, never code.
# Visibility uses the same rule as the DOM probe. Errors return {"error": ...}, never throw.
_COUNT_JS_TEMPLATE = """\
() => {
  try {
    const ENGINE = __ENGINE__;
    const EXPR = __EXPR__;
    const MAX_VISITED = 50000;
    const isVisible = (el) => {
      const view = el.ownerDocument && el.ownerDocument.defaultView;
      const style = view ? view.getComputedStyle(el) : null;
      return !!(el.getClientRects && el.getClientRects().length) &&
        (!style || (style.visibility !== "hidden" && style.display !== "none"));
    };
    const els = [];
    let shadowRoots = 0;
    let truncated = false;
    if (ENGINE === "xpath") {
      let snap;
      try {
        snap = document.evaluate(EXPR, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                                  null);
      } catch (e) { return JSON.stringify({ error: "invalid XPath: " + e.message }); }
      for (let i = 0; i < snap.snapshotLength; i++) {
        const n = snap.snapshotItem(i);
        if (n && n.nodeType === Node.ELEMENT_NODE) els.push(n);
      }
    } else {
      const roots = [document];
      let visited = 0;
      const walk = (root) => {
        for (const el of root.querySelectorAll("*")) {
          if (visited >= MAX_VISITED) { truncated = true; return; }
          visited += 1;
          if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); }
        }
      };
      walk(document);
      shadowRoots = roots.length - 1;
      for (const root of roots) {
        try { for (const el of root.querySelectorAll(EXPR)) els.push(el); }
        catch (e) { return JSON.stringify({ error: "invalid CSS selector: " + e.message }); }
      }
    }
    return JSON.stringify({
      engine: ENGINE,
      count: els.length,
      visible: els.filter(isVisible).length,
      shadowRoots: shadowRoots,
      truncated: truncated,
    });
  } catch (e) {
    return JSON.stringify({ error: "count failed: " + (e && e.message ? e.message : String(e)) });
  }
}"""


def build_count_js(engine: Engine, expr: str) -> str:
    """The fixed counting function with ``engine``/``expr`` embedded as JSON-encoded literals.

    ``json.dumps`` (ensure_ascii) yields a valid, fully-escaped JS string literal, so quotes,
    backslashes, and ``</script>`` in the selector are data — never syntax.
    """
    return _COUNT_JS_TEMPLATE.replace("__ENGINE__", json.dumps(engine)).replace(
        "__EXPR__", json.dumps(expr)
    )


_RESULT_RE = re.compile(r"### Result\s*\n(?P<body>.*?)(?:\n### |\Z)", re.DOTALL)


def parse_count_result(raw: Any) -> dict[str, Any] | None:
    """Extract the counting function's JSON payload from a ``browser_evaluate`` result.

    The MCP renders the returned string JSON-encoded under ``### Result`` (so it is decoded
    twice). Returns None when no payload can be recovered.
    """
    text = result_text(raw)
    found = _RESULT_RE.search(text)
    body = (found.group("body") if found else text).strip()
    try:
        value: Any = json.loads(body)
        if isinstance(value, str):
            value = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if "error" in value:
        return value
    # The payload comes from the page context, where scripts can patch JSON/Array
    # builtins; a non-integer count must read as unreadable, not crash format_count.
    if not all(_is_count(value.get(key)) for key in ("count", "visible")):
        return None
    return value


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def format_count(selector: str, payload: dict[str, Any]) -> str:
    """Model-facing verdict for one count: unique, none, or several (with the fix direction)."""
    if "error" in payload:
        return f"count_matches could not evaluate `{selector}`: {payload['error']}"
    count = int(payload.get("count", 0))
    visible = int(payload.get("visible", 0))
    suffix = " (DOM scan truncated — the count may be low)" if payload.get("truncated") else ""
    if count == 1:
        return f"`{selector}` matches exactly 1 element ({visible} visible) — unique.{suffix}"
    if count == 0:
        return (
            f"`{selector}` matches 0 elements — wrong selector, or the element is not on the "
            f"page in its current state.{suffix}"
        )
    return (
        f"`{selector}` matches {count} elements ({visible} visible) — NOT unique (Playwright's "
        "strict mode counts hidden matches too). Tighten it — anchor on a stable attribute, "
        f"ancestor, or text — until it matches exactly 1.{suffix}"
    )


def register_count_matches(
    agent: Agent[None, Any],
    toolset: Any,
    agent_label: str = "Planner",
) -> Callable[..., Coroutine[Any, Any, str]]:
    """Attach the always-on ``count_matches`` tool to a browser agent.

    ``toolset`` is the agent's live Playwright MCP toolset; the tool drives ``browser_evaluate``
    on it directly (``direct_call_tool``) with a FIXED function — the model only supplies the
    selector. ``agent_label`` tags the log lines. Returns the tool function (the registration
    target), which unit tests call.
    """
    target = underlying_mcp(toolset)

    async def count_matches(selector: str) -> str:
        """Count how many elements a raw CSS or XPath selector matches on the current page.

        Use it to confirm a CSS/XPath you authored is UNIQUE before recording it: it must report
        exactly 1 match (browser_generate_locator does not flag duplicates). Pass the raw
        selector — `xpath=//…`, `//…`, `css=…`, or plain CSS (a `locator('…')` wrapper is also
        accepted). Playwright-only syntax (getBy*, `>>`, `:has-text()`, `text=`) is not
        supported. Read-only; never returns a selector of its own.
        """
        try:
            engine, expr = parse_selector(selector)
        except UnsupportedSelector as exc:
            return f"count_matches cannot evaluate `{selector}`: {exc}."
        if target is None:
            # Defensive: no direct tool-call path on this toolset (e.g. a bare test double).
            return "count_matches is unavailable in this session."
        logger.info("%s count_matches: %s %r", agent_label, engine, expr)
        try:
            raw = await target.direct_call_tool(
                EVALUATE_TOOL, {"function": build_count_js(engine, expr)}
            )
        except Exception as exc:  # noqa: BLE001 — a count must degrade, never abort the run
            logger.warning("%s count_matches failed: %s", agent_label, exc)
            return f"count_matches failed ({exc}). Treat the selector as unverified."
        payload = parse_count_result(raw)
        if payload is None:
            out = f"count_matches returned an unreadable result: {clean_result(raw)}"
        else:
            out = format_count(selector, payload)
        logger.info("%s count_matches result: %s", agent_label, out[:200])
        return out

    agent.tool_plain(count_matches)
    logger.info("%s count_matches ENABLED via direct %s", agent_label, EVALUATE_TOOL)
    return count_matches
