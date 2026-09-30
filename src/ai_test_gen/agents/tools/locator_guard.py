"""Guard ``browser_generate_locator`` failures: steer to vision, never let them kill a run.

The browser agents (Planner, Healer) capture every selector with ``browser_generate_locator``.
When that tool errors, pydantic-ai retries it and — after ``AGENT_MCP_RETRIES`` *consecutive*
failing run-steps — aborts the WHOLE agent run with "browser_generate_locator exceeded max
retries". On an inaccessible app that abort is routine, not exceptional: the element exists but
carries nothing the accessibility snapshot can name, so the hunt fails repeatedly and one stuck
element used to cost the entire planning run or heal attempt.

This module installs an ``MCPToolset.process_tool_call`` hook on BOTH browser agents, ALWAYS
(``LocatorFailureGuard``). It counts CONSECUTIVE ``browser_generate_locator`` failures (any clean
return — including its own give-up result — resets the streak) and reacts in two stages:

- **Steer to vision** (only while ``AGENT_VISION`` is on AND the run's vision budget is not
  spent): at ``steer_after`` consecutive failures (default 3, ``PLANNER_LOCATOR_STEER_AFTER``,
  clamped below the retry ceiling) the bland MCP error is replaced with a ``ModelRetry`` that
  pushes the agent to ``inspect_screen`` (which self-captures the page) and re-orient. Repeated
  locator failures are usually a page-STATE problem the a11y snapshot hides — a stale ``ref``,
  the wrong page, an overlay — and a screenshot reveals every one of those. The steer never
  yields a selector.
- **Soft-land exhaustion** (always, vision on or off): at ``exhaust_after`` consecutive failures
  (= the retry ceiling) the hook stops re-raising and RETURNS an informative tool result telling
  the agent to stop hunting this element by this method — descend the resilience ladder (author
  + verify a CSS/XPath; use ``probe_dom`` when available to see the element's real attributes),
  or record the gap in notes and move on. Returning a result instead of raising means
  pydantic-ai never sees the fatal Nth retry, so a locator hunt can never abort the run;
  ``AGENT_REQUEST_LIMIT`` remains the overall backstop.

pydantic-ai resets its own per-tool retry counter on any run-step that doesn't fail the tool
(``ToolManager.for_run_step``), so the inspect detour the steer induces also refills the budget;
a recovered run never even reaches the exhaustion stage. The give-up result is a clean return too
(pydantic-ai's counter resets), so the guard resets its streak there as well — otherwise the NEXT
element's first failure would get the give-up text with no retries at all. Stateful (counts
consecutive failures) — one instance per agent run.
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import CallToolFunc, ToolResult

logger = logging.getLogger(__name__)

# The one MCP tool whose repeated failure triggers the guard — the agents' sole selector source.
LOCATOR_TOOL = "browser_generate_locator"

# Delivered IN PLACE OF the bland MCP error once the steer threshold is hit — the ModelRetry
# prompt the agent sees. It names the exact tools to call, and forbids vision as a selector source.
_STEER_MESSAGE = (
    "browser_generate_locator has failed {n} times in a row. STOP "
    "retrying the same locator — the page is likely not in the state you assume. "
    "LOOK first: call inspect_screen with a specific "
    'question such as "Is a modal or overlay covering the page?", "Am I on the expected page, '
    'or on a login/error screen?", or "Is the element I am targeting actually visible?". Then '
    "RE-ORIENT — dismiss the overlay, navigate to the right page, or wait for the element — "
    "take a fresh snapshot, and only THEN call browser_generate_locator again. If vision shows "
    "the element genuinely is not on this page, record that in notes and move on — do NOT keep "
    "retrying. inspect_screen NEVER returns a selector; the locator still comes from "
    "browser_generate_locator."
)


def _steer_after(ceiling: int, requested: int) -> int:
    """Consecutive-failure threshold for the steer: ``requested``, clamped to ``[1, ceiling - 1]``.

    ``requested`` is ``config.locator_steer_after`` (``PLANNER_LOCATOR_STEER_AFTER``, default 3).
    The clamp leaves at least one retry AFTER the steer fires for the agent to act on — a steer
    that can only fire on the final allowed attempt is useless. ``ceiling`` is the per-tool retry
    budget (``config.agent_mcp_retries``).
    """
    upper = max(1, ceiling - 1)
    return min(max(1, requested), upper)


class LocatorFailureGuard:
    """``process_tool_call`` hook: steer mid-streak (vision), soft-land at the retry ceiling.

    Counts CONSECUTIVE ``browser_generate_locator`` failures (any clean return resets the
    count). At ``exhaust_after`` (= the retry ceiling) it RETURNS a give-up-this-element tool
    result instead of raising, so the run can never die of locator-retry exhaustion, and resets
    the streak so the next element starts with its full retry allowance. Between
    ``steer_after`` and the ceiling — and only when vision is on — it raises a ``ModelRetry``
    carrying the steer message in place of the raw MCP error. Below the threshold the original
    error passes through unchanged. Every other tool passes straight through, untouched and
    uncounted.

    ``vision_on`` / ``probe_on`` shape the guidance: the steer stage exists only with vision,
    and the exhaustion message mentions ``inspect_screen`` / ``probe_dom`` only when the
    corresponding tool is actually registered on the agent. ``disable_vision`` (wired as
    ``register_inspect_screen``'s ``on_spent`` callback) drops the vision guidance once the run's
    vision budget is spent or its backend failed, so the agent isn't sent to a dead tool.
    """

    def __init__(
        self,
        ceiling: int,
        *,
        steer_after: int = 3,
        vision_on: bool = False,
        probe_on: bool = False,
    ) -> None:
        self.exhaust_after = max(1, ceiling)
        self.steer_after = _steer_after(ceiling, steer_after)
        self._vision_on = vision_on
        self._probe_on = probe_on
        self._consecutive = 0

    def disable_vision(self) -> None:
        """Stop pointing the agent at ``inspect_screen`` for the rest of this run."""
        if self._vision_on:
            logger.info("Locator guard: vision budget spent — dropping the inspect_screen steer")
        self._vision_on = False

    def _exhaust_message(self, n: int) -> str:
        """The give-up-this-element guidance returned (not raised) at the retry ceiling."""
        hints: list[str] = []
        if self._probe_on:
            hints.append(
                "call probe_dom with the element's visible text (scope it to the open "
                "dialog/container if any) to see its real tag and attributes plus candidate "
                "CSS/XPath selectors, then VERIFY the best candidate"
            )
        hints.append(
            "descend the resilience ladder: AUTHOR a candidate CSS or XPath anchored on the "
            "element's stable text/attributes and VERIFY it before recording it: pass the RAW "
            "selector (prefixed xpath= or css=) as browser_generate_locator's `target` (it "
            "errors on 0 matches), AND count_matches must report exactly 1 (generate_locator "
            "does not flag duplicates)"
        )
        if self._vision_on:
            hints.append(
                "if you may be on the wrong page or something is covering it, use "
                "inspect_screen to re-orient first"
            )
        numbered = "; ".join(f"({i + 1}) {hint}" for i, hint in enumerate(hints))
        return (
            f"{LOCATOR_TOOL} has failed {n} times in a row on this target — STOP calling it with "
            "this target; that retry budget is exhausted. This does NOT abort your task: "
            f"{numbered}. If NO locator can be verified at all, leave this element's selector "
            "empty, record exactly what you observed in notes (or changes_summary), and MOVE ON "
            "with the rest of the task. Do not repeat the call that just failed."
        )

    async def __call__(
        self,
        ctx: RunContext[Any],
        call_tool: CallToolFunc,
        name: str,
        tool_args: dict[str, Any],
    ) -> ToolResult:
        del ctx  # the guard keys on tool name + consecutive failures, not run context
        if name != LOCATOR_TOOL:
            return await call_tool(name, tool_args)
        try:
            result = await call_tool(name, tool_args)
        except ModelRetry as exc:
            self._consecutive += 1
            if self._consecutive >= self.exhaust_after:
                # Return (don't raise): pydantic-ai treats this as a clean tool result, so the
                # fatal "exceeded max retries" can never fire for the locator tool.
                logger.info(
                    "Locator guard: %s failed %d× in a row (>= ceiling %d) — returning "
                    "give-up-this-element guidance instead of aborting the run",
                    LOCATOR_TOOL,
                    self._consecutive,
                    self.exhaust_after,
                )
                message = self._exhaust_message(self._consecutive)
                # The clean return also resets pydantic-ai's retry counter; mirror it so the next
                # element's first failure gets normal retry treatment, not this give-up text.
                self._consecutive = 0
                return message
            if self._vision_on and self._consecutive >= self.steer_after:
                logger.info(
                    "Locator guard: %s failed %d× in a row (>=%d) — steering to inspect_screen",
                    LOCATOR_TOOL,
                    self._consecutive,
                    self.steer_after,
                )
                raise ModelRetry(_STEER_MESSAGE.format(n=self._consecutive)) from exc
            logger.info(
                "Locator guard: %s failure %d/%d before intervening",
                LOCATOR_TOOL,
                self._consecutive,
                self.steer_after if self._vision_on else self.exhaust_after,
            )
            raise
        self._consecutive = 0
        return result
