"""Steer the Planner to vision when ``browser_generate_locator`` keeps failing.

The Planner drives the live staging app over Playwright MCP and captures every selector with
``browser_generate_locator``. When that tool errors, pydantic-ai retries it (ceiling =
``AGENT_MCP_RETRIES``, default 5) and, after enough *consecutive* failing run-steps, aborts
the whole planning run with "browser_generate_locator exceeded max retries". The repeated
failures are almost never a missing-id problem — they are page-STATE problems the a11y
snapshot hides: a stale ``ref``, the wrong page (a login that didn't land, a redirect, an
error screen), a modal or overlay over the target, an element not rendered yet, or one that
simply isn't on this page. A screenshot reveals every one of those.

This installs an ``MCPToolset.process_tool_call`` hook that counts CONSECUTIVE
``browser_generate_locator`` failures and, at a threshold (default 3, kept below the retry
ceiling), replaces the bland MCP error with a ``ModelRetry`` that STEERS the (text-only)
Planner to ``browser_take_screenshot`` + ``inspect_screen`` and re-orient before retrying. It
diagnoses WHERE the Planner is stuck; ``browser_generate_locator`` stays the sole selector
source — the steer never yields a selector (the vision sensor is forbidden from producing
one; see ``vision.py``). A single successful locator resets the count — and pydantic-ai
resets its own per-tool retry counter on any step that doesn't fail the tool
(``ToolManager.for_run_step``), so the screenshot/inspect detour the steer induces also
refills the budget. A recovered run never trips the ceiling.

Planner-only: the steer points at ``inspect_screen``, registered only on the Planner and only
when ``PLANNER_VISION`` is on. With vision off no hook is attached and the run is
byte-identical to before. Stateful (counts consecutive failures) — one instance per run.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import CallToolFunc, ToolResult

logger = logging.getLogger(__name__)

# The one MCP tool whose repeated failure triggers the steer — the Planner's sole selector source.
LOCATOR_TOOL = "browser_generate_locator"

_STEER_AFTER_ENV = "PLANNER_LOCATOR_STEER_AFTER"
_DEFAULT_STEER_AFTER = 3

# Delivered IN PLACE OF the bland MCP error once the threshold is hit — the ModelRetry prompt
# the Planner sees. It names the exact tools to call, and forbids vision as a selector source.
_STEER_MESSAGE = (
    "browser_generate_locator has now failed {n} times in a row on this target. STOP "
    "retrying the same locator — the page is almost certainly not in the state you assume. "
    "LOOK before retrying: call browser_take_screenshot, then inspect_screen with a specific "
    'question such as "Is a modal or overlay covering the page?", "Am I on the expected page, '
    'or on a login/error screen?", or "Is the element I am targeting actually visible?". Then '
    "RE-ORIENT — dismiss the overlay, navigate to the right page, or wait for the element — "
    "take a fresh snapshot, and only THEN call browser_generate_locator again. If vision shows "
    "the element genuinely is not on this page, record that in notes and move on — do NOT keep "
    "retrying. inspect_screen NEVER returns a selector; the locator still comes from "
    "browser_generate_locator."
)


def _steer_after(ceiling: int) -> int:
    """Consecutive-failure threshold for the steer (``PLANNER_LOCATOR_STEER_AFTER``, default 3).

    Clamped to ``[1, ceiling - 1]`` so at least one retry remains AFTER the steer fires for the
    Planner to act on — a steer that can only fire on the final allowed attempt is useless.
    ``ceiling`` is the per-tool retry budget (``agent_retries()``). Invalid values fall back to
    the default; this is a tuning knob, not a correctness gate.
    """
    raw = os.environ.get(_STEER_AFTER_ENV)
    value = _DEFAULT_STEER_AFTER
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = _DEFAULT_STEER_AFTER
    upper = max(1, ceiling - 1)
    return min(max(1, value), upper)


class LocatorVisionSteer:
    """``process_tool_call`` hook: steer the Planner to vision on repeated locator failure.

    Counts CONSECUTIVE ``browser_generate_locator`` failures (any clean return resets the
    count). At ``steer_after`` consecutive failures it raises a ``ModelRetry`` carrying the
    steer message in place of the raw MCP error; below the threshold it re-raises the original
    error unchanged. Every other tool passes straight through, untouched and uncounted.
    """

    def __init__(self, ceiling: int) -> None:
        self.steer_after = _steer_after(ceiling)
        self._consecutive = 0

    async def __call__(
        self,
        ctx: RunContext[Any],
        call_tool: CallToolFunc,
        name: str,
        tool_args: dict[str, Any],
    ) -> ToolResult:
        del ctx  # the steer keys on tool name + consecutive failures, not run context
        if name != LOCATOR_TOOL:
            return await call_tool(name, tool_args)
        try:
            result = await call_tool(name, tool_args)
        except ModelRetry as exc:
            self._consecutive += 1
            if self._consecutive >= self.steer_after:
                logger.info(
                    "Planner locator steer: %s failed %d×in a row (>=%d) — steering to "
                    "browser_take_screenshot + inspect_screen",
                    LOCATOR_TOOL,
                    self._consecutive,
                    self.steer_after,
                )
                raise ModelRetry(_STEER_MESSAGE.format(n=self._consecutive)) from exc
            logger.info(
                "Planner locator steer: %s failure %d/%d before steering to vision",
                LOCATOR_TOOL,
                self._consecutive,
                self.steer_after,
            )
            raise
        self._consecutive = 0
        return result
