"""Shared DOM Probe: read-only reconnaissance for elements the a11y snapshot can't name.

On an inaccessible (div-soup) app the browser agents are structurally blind: their only DOM
source is the accessibility snapshot, which renders unnamed ``generic`` nodes for non-semantic
controls — so resilience-ladder rungs 3–4 (CSS/XPath) have no data to author a candidate from,
and a visible element ("the modal's submit button") can be unfindable. The raw
``browser_evaluate`` tool is deliberately hidden from the agents (model-authored JS hallucinates
selectors and is a code-exec risk — see ``playwright_mcp._BLOCKED_TOOL_MARKERS``).

``probe_dom(text, scope?)`` fills the gap without re-opening that door: it executes ONE fixed,
pipeline-authored, READ-ONLY JS function via ``direct_call_tool("browser_evaluate", ...)`` — the
same agent-filter bypass the Vision Aid uses for self-capture screenshots. The model supplies
only DATA (a search text, an optional CSS scope), embedded JSON-escaped into the constant
function; it can never inject code (the parameterized-SQL principle). The probe returns, per
match: tag / id / classes / own text / relevant attributes / visibility / shadow-DOM & iframe
flags, plus a CANDIDATE CSS and XPath selector with match counts.

Candidates are reconnaissance, NOT locators of record: the agent must verify one through the
existing path (``browser_generate_locator`` with the candidate as ``target``, plus the
``browser_verify_*`` tools) before recording it — "verify before trust" stays the law; the probe
just replaces blind guessing with informed authoring. Budgeted per agent run via
``AGENT_DOM_PROBE`` (closure-local counter, same pattern as the Vision Aid).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from pydantic_ai import Agent

from ..config import Config
from ._vision_aid import _underlying_mcp

logger = logging.getLogger(__name__)

__all__ = ["PROBE_TOOL", "build_probe_js", "register_probe_dom"]

# The MCP tool the probe drives directly. Hidden from the agents' toolset (see
# playwright_mcp._BLOCKED_TOOL_MARKERS); reachable here only via direct_call_tool with the
# fixed function below — the model never authors JS.
PROBE_TOOL = "browser_evaluate"

# Hard cap on the text returned to the agent — recon must inform, not flood the context.
_RESULT_CHAR_CAP = 4000

# Some MCP tool results append the full page snapshot; the probe's JSON is self-contained, so
# anything from this marker on is dead weight and is stripped before returning.
_SNAPSHOT_MARKER = "Page Snapshot"

# The fixed, read-only probe function. __QUERY__ / __SCOPE__ are replaced with JSON-encoded
# values (see build_probe_js) — the model's inputs are data inside string literals, never code.
# It walks the scope (piercing open shadow roots and same-origin iframes), matches on own text +
# common attributes, and reports each match with candidate CSS/XPath + match counts. Any internal
# error returns {"error": ...} instead of throwing, so a bad scope degrades to a message.
_PROBE_JS_TEMPLATE = """\
() => {
  try {
    const QUERY = __QUERY__;
    const SCOPE = __SCOPE__;
    const MAX_VISITED = 20000;
    const MAX_MATCHES = 8;
    const norm = (s) => String(s || "").replace(/\\s+/g, " ").trim();
    const needle = norm(QUERY).toLowerCase();
    if (!needle) return JSON.stringify({ error: "empty search text" });

    let root = document;
    if (SCOPE) {
      let scoped = null;
      try { scoped = document.querySelector(SCOPE); }
      catch (e) { return JSON.stringify({ error: "invalid scope selector: " + e.message }); }
      if (!scoped) return JSON.stringify({ error: "scope selector matched no element: " + SCOPE });
      root = scoped;
    }

    const state = { visited: 0, truncated: false };
    const crossOriginIframes = [];
    const found = [];

    const ownText = (el) => {
      let t = "";
      for (const n of el.childNodes) if (n.nodeType === Node.TEXT_NODE) t += n.textContent;
      return norm(t);
    };
    const attr = (el, name) => (el.getAttribute && el.getAttribute(name)) || "";
    const haystack = (el) => [
      ownText(el), attr(el, "aria-label"), attr(el, "title"), attr(el, "placeholder"),
      attr(el, "name"), el.value ? String(el.value) : "", el.id || "",
    ];

    const collect = (node, inShadow, frameSrc) => {
      let all;
      try { all = node.querySelectorAll("*"); } catch (e) { return; }
      for (const el of all) {
        if (state.visited >= MAX_VISITED) { state.truncated = true; return; }
        state.visited += 1;
        if (found.length < MAX_MATCHES &&
            haystack(el).some((h) => norm(h).toLowerCase().includes(needle))) {
          found.push({ el, inShadow, frameSrc });
        }
        if (el.shadowRoot) collect(el.shadowRoot, true, frameSrc);
        if (el.tagName === "IFRAME" || el.tagName === "FRAME") {
          try {
            if (el.contentDocument)
              collect(el.contentDocument, inShadow, el.src || "(inline frame)");
            else crossOriginIframes.push(el.src || "(no src)");
          } catch (e) { crossOriginIframes.push(el.src || "(no src)"); }
        }
      }
    };
    collect(root, false, null);

    const describeAncestors = (el) => {
      const parts = [];
      let cur = el.parentElement;
      for (let i = 0; cur && i < 4; i++) {
        let d = cur.tagName.toLowerCase();
        if (cur.id) d += '[id="' + cur.id + '"]';
        else if (cur.classList && cur.classList.length)
          d += "." + Array.from(cur.classList).slice(0, 2).join(".");
        parts.push(d);
        cur = cur.parentElement;
      }
      return parts.join(" < ");
    };

    const cssCandidate = (el) => {
      if (el.id) return '[id="' + el.id.replace(/"/g, '\\\\"') + '"]';
      const tag = el.tagName.toLowerCase();
      const anchors = ["name", "data-testid", "data-qa", "aria-label", "placeholder", "title"];
      for (const a of anchors) {
        const v = attr(el, a);
        if (v) return tag + "[" + a + '="' + v.replace(/"/g, '\\\\"') + '"]';
      }
      let anc = el.parentElement, hop = 0;
      while (anc && hop < 6 && !anc.id) { anc = anc.parentElement; hop += 1; }
      const cls = el.classList && el.classList.length
        ? "." + Array.from(el.classList).slice(0, 2).join(".")
        : "";
      const tail = tag + cls;
      return anc && anc.id ? '[id="' + anc.id.replace(/"/g, '\\\\"') + '"] ' + tail : tail;
    };

    const xpathCandidate = (el) => {
      const tag = el.tagName.toLowerCase();
      const text = ownText(el);
      if (text && text.length <= 60 && !text.includes('"'))
        return '//' + tag + '[normalize-space()="' + text + '"]';
      const anchors = ["name", "aria-label", "placeholder", "title"];
      for (const a of anchors) {
        const v = attr(el, a);
        if (v && !v.includes('"')) return '//' + tag + '[@' + a + '="' + v + '"]';
      }
      return null;
    };

    const countCss = (el, css) => {
      try { return (el.getRootNode() || document).querySelectorAll(css).length; }
      catch (e) { return null; }
    };
    const countXPath = (el, xp) => {
      if (!xp) return null;
      const doc = el.ownerDocument;
      if (!doc || el.getRootNode() !== doc) return null; // XPath cannot see into shadow roots
      try {
        return doc.evaluate(xp, doc, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null)
          .snapshotLength;
      } catch (e) { return null; }
    };

    const matches = found.map(({ el, inShadow, frameSrc }) => {
      const view = el.ownerDocument && el.ownerDocument.defaultView;
      const style = view ? view.getComputedStyle(el) : null;
      const css = cssCandidate(el);
      const xp = xpathCandidate(el);
      const attrs = {};
      const wanted = ["name", "type", "role", "aria-label", "placeholder", "title",
                      "data-testid", "data-qa"];
      for (const a of wanted) {
        const v = attr(el, a);
        if (v) attrs[a] = v.slice(0, 80);
      }
      return {
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        classes: el.classList ? Array.from(el.classList).slice(0, 6).join(" ") : "",
        text: ownText(el).slice(0, 80),
        attrs: attrs,
        visible: !!(el.getClientRects && el.getClientRects().length) &&
          (!style || (style.visibility !== "hidden" && style.display !== "none")),
        disabled: el.disabled === true || attr(el, "aria-disabled") === "true",
        inShadowDom: inShadow,
        inIframe: frameSrc,
        ancestors: describeAncestors(el),
        candidateCss: css,
        candidateCssMatches: countCss(el, css),
        candidateXPath: xp,
        candidateXPathMatches: countXPath(el, xp),
      };
    });

    return JSON.stringify({
      query: QUERY,
      scope: SCOPE,
      matchCount: matches.length,
      matches: matches,
      elementsScanned: state.visited,
      truncated: state.truncated,
      crossOriginIframes: crossOriginIframes.slice(0, 5),
    });
  } catch (e) {
    return JSON.stringify({ error: "probe failed: " + (e && e.message ? e.message : String(e)) });
  }
}"""


def build_probe_js(text: str, scope: str | None) -> str:
    """The fixed probe function with ``text``/``scope`` embedded as JSON-encoded literals.

    ``json.dumps`` (ensure_ascii) yields a valid, fully-escaped JS string literal (or ``null``),
    so quotes, backslashes, and unicode in the model's inputs are data — never syntax.
    """
    return _PROBE_JS_TEMPLATE.replace("__QUERY__", json.dumps(text)).replace(
        "__SCOPE__", json.dumps(scope)
    )


def _result_text(result: Any) -> str:
    """Best-effort text of a ``direct_call_tool`` result (plain string or content-item list)."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        texts = []
        for item in result:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if text:
                texts.append(text)
        return "\n".join(texts)
    return str(result)


def _clean(raw: Any) -> str:
    """Strip any trailing page snapshot and cap the size of a probe result."""
    text = _result_text(raw)
    marker_at = text.find(_SNAPSHOT_MARKER)
    if marker_at >= 0:
        text = text[:marker_at].rstrip()
    if len(text) > _RESULT_CHAR_CAP:
        text = text[:_RESULT_CHAR_CAP] + " …[probe result truncated]"
    return text


def register_probe_dom(
    agent: Agent[None, Any],
    config: Config,
    toolset: Any,
    agent_label: str = "Planner",
) -> Callable[..., Coroutine[Any, Any, str]]:
    """Attach the optional ``probe_dom`` DOM-recon tool to a browser agent.

    ``toolset`` is the agent's live Playwright MCP toolset; the probe drives
    ``browser_evaluate`` on it directly (``direct_call_tool``), bypassing the agent-facing
    code-exec filter with a FIXED function — the model only ever supplies the search text and
    scope. Per-run call budget = ``config.dom_probe_max_calls``. ``agent_label`` tags the log
    lines. Also returns the tool function (the registration target), which unit tests call.
    """
    max_calls = config.dom_probe_max_calls
    target = _underlying_mcp(toolset)
    calls_made = 0

    async def probe_dom(text: str, scope: str | None = None) -> str:
        """Search the live DOM (read-only) for elements matching visible text or attributes.

        Use when the accessibility snapshot does NOT usefully show an element you can see or
        expect — non-semantic div/span controls, an unnamed button in a modal, a sparse/empty
        snapshot. Returns each match's real tag, id, classes, attributes, visibility, and a
        CANDIDATE css + xpath selector with match counts. Candidates are UNVERIFIED
        reconnaissance: before recording or using one, VERIFY it — pass the candidate as
        browser_generate_locator's `target` and/or confirm with browser_verify_element_visible.
        `scope` optionally restricts the search to a container's CSS selector (e.g. the open
        dialog). The snapshot + browser_generate_locator remain the primary path; calls here
        count against a per-run budget.
        """
        nonlocal calls_made
        if target is None:
            # Defensive: no direct tool-call path on this toolset (e.g. a bare test double).
            return "probe_dom is unavailable in this session — proceed with the snapshot."
        if calls_made >= max_calls:
            logger.info(
                "%s DOM probe: budget of %d call(s) reached — skipping", agent_label, max_calls
            )
            return (
                f"DOM-probe budget reached ({max_calls} calls this run). Proceed with the "
                "accessibility snapshot and browser_generate_locator."
            )
        calls_made += 1
        logger.info(
            "%s DOM probe %d/%d: text=%r scope=%r", agent_label, calls_made, max_calls, text, scope
        )
        try:
            raw = await target.direct_call_tool(
                PROBE_TOOL, {"function": build_probe_js(text, scope)}
            )
        except Exception as exc:  # noqa: BLE001 — recon must degrade, never abort the run
            logger.warning("%s DOM probe failed: %s", agent_label, exc)
            return f"probe_dom failed ({exc}). Proceed with the accessibility snapshot."
        out = _clean(raw)
        logger.info("%s DOM probe result: %s", agent_label, out[:200])
        return out

    agent.tool_plain(probe_dom)
    logger.info(
        "%s DOM probe ENABLED: up to %d probe_dom call(s)/run via direct %s",
        agent_label,
        max_calls,
        PROBE_TOOL,
    )
    return probe_dom
