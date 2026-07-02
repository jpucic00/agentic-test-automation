"""Shared Vision Aid sensor: turn a live screenshot into a short text observation.

Both browser-driving agents (Planner and Healer) are text-only on the gateway's reasoning model,
so they cannot ingest images. This module gives either of them an ``inspect_screen`` tool that
screenshots the CURRENT page and hands it to the **Vision Aid Agent** (``vision.ask_vision`` — a
vision-capable model) for a one-or-two-sentence description of *visual state*. The browser agent
stays the brain and the MCP driver; the Vision Aid Agent is only an eye. It NEVER yields a selector
(element targeting stays on ``browser_generate_locator``).

Extracted from ``planner.py`` so the Planner and the Healer register the exact same sensor. The
budget is a closure-local counter seeded from ``config.vision_max_calls``, so each agent lifecycle
(one Planner run; each Healer heal attempt) gets its own ``N`` — a single shared *knob*, per-run
counters, intentionally not one global pool (that would need an orchestrator-threaded counter).

``planner.py`` re-exports these names for back-compat with existing imports/monkeypatch targets.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from pydantic_ai import Agent

from ..config import Config
from .vision import ask_vision

logger = logging.getLogger(__name__)

# Shared API consumed by the Planner and Healer builders (and unit tests). Declared so the helpers
# that are only USED from sibling modules aren't flagged as unused-within-module.
__all__ = [
    "_DEFAULT_STALE_AFTER_S",
    "SCREENSHOT_TOOL",
    "_stale_after_s",
    "_latest_png",
    "_underlying_mcp",
    "_make_screenshot_capture",
    "register_inspect_screen",
]

# A screenshot older than this (seconds) is treated as stale by inspect_screen: the model must
# take a fresh browser_take_screenshot before the vision sensor will describe "the current page".
# The default is generous on purpose: two gateway round-trips (browser_take_screenshot, then a
# SEPARATE inspect_screen turn) sit between capture and use, so a tight window made every real call
# bounce as "stale" — invisibly. Override per-run with PLANNER_VISION_STALE_S.
_DEFAULT_STALE_AFTER_S = 45.0

# The Playwright MCP tool inspect_screen drives itself (via direct_call_tool) to capture the live
# page before describing it — see _make_screenshot_capture and inspect_screen.
SCREENSHOT_TOOL = "browser_take_screenshot"


def _stale_after_s() -> float:
    """Staleness window (seconds) for inspect_screen, from ``PLANNER_VISION_STALE_S`` (default 45).

    Invalid or non-positive values fall back to the default rather than failing the run — this is a
    latency-tuning knob, not a correctness gate.
    """
    raw = os.environ.get("PLANNER_VISION_STALE_S")
    if raw is None:
        return _DEFAULT_STALE_AFTER_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_STALE_AFTER_S
    return value if value > 0 else _DEFAULT_STALE_AFTER_S


def _latest_png(directory: Path) -> Path | None:
    """Newest ``*.png`` under ``directory`` by mtime, or None if there are none."""
    pngs = list(directory.rglob("*.png"))
    if not pngs:
        return None
    return max(pngs, key=lambda p: p.stat().st_mtime)


def _underlying_mcp(toolset: Any) -> Any | None:
    """Walk a toolset's wrapper chain to the object exposing ``direct_call_tool``.

    ``build_playwright_mcp`` returns the live ``MCPToolset`` wrapped in a ``.filtered(...)`` layer;
    only the underlying ``MCPToolset`` exposes ``direct_call_tool``. Follow ``.wrapped`` until we
    find it (or run out), so inspect_screen can take a screenshot on the SAME live browser the
    agent drives. Returns None if no layer can call a tool directly (defensive — callers degrade).
    """
    seen = toolset
    while seen is not None and not hasattr(seen, "direct_call_tool"):
        seen = getattr(seen, "wrapped", None)
    return seen


def _make_screenshot_capture(
    toolset: Any,
) -> Callable[[], Coroutine[Any, Any, None]] | None:
    """Build the async fn inspect_screen calls to capture the CURRENT page, or None if impossible.

    Drives ``browser_take_screenshot`` directly on the live MCP toolset (not through the model), so
    the PNG inspect_screen reads always reflects the page as it is NOW — removing the reliance on
    the model remembering to screenshot first (the cause of stale, previous-page vision answers).
    """
    target = _underlying_mcp(toolset)
    if target is None:
        return None

    async def capture() -> None:
        await target.direct_call_tool(SCREENSHOT_TOOL, {})

    return capture


def register_inspect_screen(
    agent: Agent[None, Any],
    config: Config,
    capture: Callable[[], Coroutine[Any, Any, None]] | None = None,
    agent_label: str = "Planner",
) -> Callable[[str], Coroutine[Any, Any, str]]:
    """Attach the optional ``inspect_screen`` Vision Aid tool to a browser agent.

    The reasoning model stays the MCP driver; this tool just turns a fresh screenshot into a short
    text answer (via ``ask_vision``) so the text-only agent can "see". When ``capture`` is supplied
    (the live-browser screenshot fn from ``_make_screenshot_capture``), inspect_screen takes the
    screenshot ITSELF immediately before describing it, so the image always reflects the current
    page — not a stale, previous-page shot the model forgot to refresh. ``capture=None`` keeps the
    old passive behaviour (read whatever PNG is newest on disk) for unit tests. Per-run call budget
    = ``config.vision_max_calls``. Advisory only — it never returns a selector. ``agent_label``
    (e.g. ``"Planner"`` / ``"Healer"``) only tags the log lines so a run shows which agent is
    looking. Also returns the tool function (the registration target), which unit tests call.
    """
    max_calls = config.vision_max_calls
    calls_made = 0

    async def inspect_screen(question: str) -> str:
        """Look at the CURRENT page and answer a question about what is visible.

        Captures a fresh screenshot of the live page ITSELF, then shows it to a vision model — you
        do NOT need to call browser_take_screenshot first; just call this with a specific question,
        e.g. "Is a modal dialog covering the page?" or "Did a success toast appear?". Use when the
        accessibility snapshot is ambiguous or silent — to confirm a dropdown opened, a
        modal/overlay is covering the page, a success/error toast appeared, or whether an element is
        actually visible. The answer has TWO labeled parts: "Answer:" (your question — it flags a
        wrong premise explicitly) and "On screen:" (what the page actually shows) — read
        "On screen:" to confirm you are where you think you are, and re-orient first if not.
        Ask ONLY about visible state.
        NEVER ask it for an id, data-testid, selector, or locator (it cannot read those from
        pixels); capture every selector with browser_generate_locator instead.
        """
        nonlocal calls_made
        if calls_made >= max_calls:
            logger.info(
                "%s vision: budget of %d call(s) reached — skipping", agent_label, max_calls
            )
            return (
                f"Vision budget reached ({max_calls} calls this run). Proceed using the "
                "accessibility snapshot."
            )
        # Capture the page as it is NOW so the description can't be of a page the agent has already
        # navigated away from. On failure, degrade to the newest existing PNG (the staleness guard
        # below still protects against describing an ancient leftover as current).
        if capture is not None:
            try:
                await capture()
            except Exception as exc:  # noqa: BLE001 — any capture failure must degrade, not abort
                logger.warning(
                    "%s vision: self-capture via %s failed (%s) — falling back to the newest "
                    "existing screenshot under the staleness guard",
                    agent_label,
                    SCREENSHOT_TOOL,
                    exc,
                )
        png = _latest_png(config.snapshots_dir)
        if png is None:
            logger.info(
                "%s vision: inspect_screen called but no screenshot in %s yet — asked the model to "
                "browser_take_screenshot first",
                agent_label,
                config.snapshots_dir,
            )
            return (
                "No screenshot is available yet. Call browser_take_screenshot first, then retry "
                "inspect_screen."
            )
        stale_after = _stale_after_s()
        age = time.time() - png.stat().st_mtime
        if age > stale_after:
            logger.info(
                "%s vision: inspect_screen called but the latest screenshot is %.0fs old "
                "(> %.0fs) — asked the model to recapture; raise PLANNER_VISION_STALE_S if the "
                "gateway round-trip is the cause",
                agent_label,
                age,
                stale_after,
            )
            return (
                f"The latest screenshot is stale (older than {stale_after:.0f}s). Call "
                "browser_take_screenshot first, then retry inspect_screen."
            )
        calls_made += 1
        logger.info("%s vision check %d/%d: %s", agent_label, calls_made, max_calls, question)
        answer = await ask_vision(config, question, png.read_bytes())
        logger.info("%s vision answer: %s", agent_label, answer)
        return answer

    agent.tool_plain(inspect_screen)
    logger.info(
        "%s vision sensor ENABLED: up to %d inspect_screen call(s)/run via %s "
        "(staleness window %.0fs); reads the newest *.png from %s",
        agent_label,
        max_calls,
        config.vision_model,
        _stale_after_s(),
        config.snapshots_dir,
    )
    return inspect_screen
