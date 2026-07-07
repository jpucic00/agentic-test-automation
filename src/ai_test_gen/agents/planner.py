"""The Planner agent: ManualTestCase + live staging app (MCP) -> TestPlan.

The Planner reads a manual test case and, using the Playwright MCP toolset to
navigate the live staging app, produces a structured ``TestPlan`` with verified
selectors. It does not merely verify selectors — it DRIVES the scenario (happy and
failure paths) and records what the app actually does, which is the key advantage
over generating tests blind.

Implements AI_TEST_GENERATION_GUIDE.md §3.8 (+ §3.5b context loading). The Planner
gets BOTH context files (project_context.md and project_map.md).

The optional Vision Aid sensor (``inspect_screen``) is shared with the Healer and lives in
``_vision_aid``; the helpers below are re-exported from here for back-compat with existing imports.
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic_ai import Agent, AgentRetries
from pydantic_ai.capabilities import ProcessHistory

from ..config import Config
from ..llm import build_openai_model
from ..models import ManualTestCase, TestPlan
from ..playwright_mcp import build_playwright_mcp
from ._context import (
    agent_output_retries,
    agent_retries,
    assemble_system_prompt,
    build_model_settings,
)
from ._dom_probe import register_probe_dom
from ._history import trim_stale_snapshots
from ._locator_steer import LOCATOR_TOOL, LocatorFailureGuard
from ._run_failure import run_agent_logged

# Vision Aid sensor (shared with the Healer). Re-exported here so existing imports and monkeypatch
# targets (tests/test_vision.py) keep resolving from this module. noqa: these are deliberate
# re-exports; _make_screenshot_capture and register_inspect_screen are also used directly below.
from ._vision_aid import (  # noqa: F401
    _DEFAULT_STALE_AFTER_S,
    SCREENSHOT_TOOL,
    _latest_png,
    _make_screenshot_capture,
    _stale_after_s,
    _underlying_mcp,
    register_inspect_screen,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Module-level aliases so build_planner calls names a test can monkeypatch
# (test_planner_registers_inspect_screen_only_when_enabled patches these attributes).
_register_inspect_screen = register_inspect_screen
_register_probe_dom = register_probe_dom

# Public surface + the Vision Aid helpers re-exported for back-compat (consumed by tests).
__all__ = [
    "build_planner",
    "plan_test_case",
    "_register_inspect_screen",
    "register_inspect_screen",
    "_register_probe_dom",
    "register_probe_dom",
    "_make_screenshot_capture",
    "_underlying_mcp",
    "_latest_png",
    "_stale_after_s",
    "_DEFAULT_STALE_AFTER_S",
    "SCREENSHOT_TOOL",
]


def build_planner(config: Config, storage_state: Path | None = None) -> Agent[None, TestPlan]:
    """Build the Planner agent (Playwright MCP toolset attached, output_type=TestPlan)."""
    model = build_openai_model(config, config.planner_model)

    base_prompt = (PROMPTS_DIR / "planner.md").read_text()
    if config.vision_max_calls > 0:
        # Gated so a disabled run's system prompt is byte-identical to before.
        base_prompt += "\n\n" + (PROMPTS_DIR / "vision_aid.md").read_text()
    if config.dom_probe_max_calls > 0:
        # Same gating for the DOM probe fragment: probe off ⇒ prompt byte-identical.
        base_prompt += "\n\n" + (PROMPTS_DIR / "dom_probe.md").read_text()
    system_prompt = assemble_system_prompt(config, base_prompt, include_map=True)

    # Locator-failure guard — ALWAYS attached: at the retry ceiling it returns give-up-this-
    # element guidance instead of letting "browser_generate_locator exceeded max retries" abort
    # the whole planning run. Its mid-streak steer to vision stays gated on AGENT_VISION, and
    # its guidance mentions probe_dom only when the probe is registered.
    guard = LocatorFailureGuard(
        agent_retries(),
        vision_on=config.vision_max_calls > 0,
        probe_on=config.dom_probe_max_calls > 0,
    )
    logger.info(
        "Planner locator guard ENABLED: give-up guidance after %d consecutive %s failure(s)%s",
        guard.exhaust_after,
        LOCATOR_TOOL,
        f"; vision steer at {guard.steer_after}" if config.vision_max_calls > 0 else "",
    )
    mcp = build_playwright_mcp(config, storage_state=storage_state, process_tool_call=guard)

    # Reasoning effort (PLANNER_REASONING_EFFORT) + parallel_tool_calls=False ALWAYS — browser
    # tool calls mutate one shared page and must run strictly in order (see build_model_settings).
    model_settings = build_model_settings("PLANNER_REASONING_EFFORT")

    agent = Agent(
        model=model,
        output_type=TestPlan,
        toolsets=[mcp],
        system_prompt=system_prompt,
        model_settings=model_settings,
        # tool: room to recover from transient MCP tool errors. output: the model's own bad
        # responses (empty/unparsed turns) accumulate ACROSS the run — separate, larger budget.
        retries=AgentRetries(tools=agent_retries(), output=agent_output_retries()),
        # Long explorations accumulate dozens of stale page snapshots; keep only the
        # newest few so the model stays out of its long-context degradation zone.
        capabilities=[ProcessHistory(trim_stale_snapshots)],
    )
    # Optional Vision Aid sensor (AGENT_VISION). Registered only when enabled
    # so a disabled run's toolset — and behaviour — is identical to before. The capture handle
    # drives browser_take_screenshot on this same live MCP so inspect_screen sees the current page.
    if config.vision_max_calls > 0:
        _register_inspect_screen(agent, config, capture=_make_screenshot_capture(mcp))
    # Optional DOM Probe (AGENT_DOM_PROBE) — same gating; drives browser_evaluate on this same
    # live MCP with a FIXED read-only function (see agents/_dom_probe.py).
    if config.dom_probe_max_calls > 0:
        _register_probe_dom(agent, config, mcp)
    return agent


async def plan_test_case(
    config: Config,
    test_case: ManualTestCase,
    storage_state: Path | None = None,
) -> TestPlan:
    """Run the Planner on a single test case and return its TestPlan."""
    agent = build_planner(config, storage_state=storage_state)

    user_message = f"""# Manual Test Case to Plan

**Issue Key:** {test_case.key}
**Title:** {test_case.title}
**Staging URL:** {config.staging_base_url}

**Description:**
{test_case.description}

**Preconditions:**
{chr(10).join('- ' + p for p in test_case.preconditions) or '(none)'}

**Steps and Expected Results:**
{_format_steps(test_case)}

Now build a TestPlan. Navigate the staging app and DRIVE the scenario — perform each outcome-bearing
step, happy AND failure paths (submit forms, create data, trigger validation), and OBSERVE what the
app actually does. For each element you act on, capture a VERIFIED locator — the most robust kind
that element supports (resilience ladder: id > accessible > CSS > XPath). Author-written id=
attributes surface as getByTestId('...') via browser_generate_locator; accessible elements come back
as getByRole/getByLabel; inaccessible ones need a verified locator('css=...') or
locator('xpath=...'). Never hand-write an unverified locator — confirm it resolves to the intended
element first. Record each locator verbatim in target_selector.

For every step that asserts an outcome (a "verify …" step, or the after-state of a navigate/submit/
open-modal step), also record HOW to prove it: set page_url for page loads (assert the URL), or
capture a VERIFIED locator for the proof element into assert_selector — never leave the Generator to
guess assertion text. Keep each step's `expected` faithful to the manual case; if the live app
contradicts it, still keep the spec's expected and record the divergence in `notes`. If performing a
step changes earlier state (a failed login clears the password), make the recovery you had to do its
own ordered step.
"""

    # run_agent_logged enters the agent (Playwright MCP subprocess start/stop around the
    # run) and logs the captured failure evidence on retry exhaustion before re-raising.
    return await run_agent_logged(agent, user_message, agent_label="Planner")


def _format_steps(tc: ManualTestCase) -> str:
    """Render steps paired with their expected results (matched by index)."""
    if not tc.steps:
        return "(no steps provided)"
    lines: list[str] = []
    for i, step in enumerate(tc.steps, 1):
        expected = tc.expected_results[i - 1] if i - 1 < len(tc.expected_results) else ""
        lines.append(f"{i}. {step}")
        if expected:
            lines.append(f"   Expected: {expected}")
    return "\n".join(lines)
