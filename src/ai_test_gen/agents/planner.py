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

from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.usage import UsageLimits

from ..config import Config
from ..llm import build_openai_model
from ..models import ManualTestCase, TestPlan
from ..playwright_mcp import build_playwright_mcp
from ._context import (
    agent_request_limit,
    agent_retries,
    assemble_system_prompt,
    reasoning_effort,
)
from ._history import trim_stale_snapshots
from ._locator_steer import LOCATOR_TOOL, LocatorVisionSteer

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

# Module-level alias so build_planner calls the name a test can monkeypatch
# (test_planner_registers_inspect_screen_only_when_enabled patches this attribute).
_register_inspect_screen = register_inspect_screen

# Public surface + the Vision Aid helpers re-exported for back-compat (consumed by tests).
__all__ = [
    "build_planner",
    "plan_test_case",
    "_register_inspect_screen",
    "register_inspect_screen",
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
    system_prompt = assemble_system_prompt(config, base_prompt, include_map=True)

    # Optional locator→vision steer: after N consecutive browser_generate_locator failures it
    # swaps the bland MCP error for a ModelRetry pushing the Planner to screenshot +
    # inspect_screen and re-orient. Gated on the same flag as inspect_screen — vision off ⇒
    # hook is None, toolset unchanged.
    steer: LocatorVisionSteer | None = None
    if config.vision_max_calls > 0:
        steer = LocatorVisionSteer(agent_retries())
        logger.info(
            "Planner locator-failure vision steer ENABLED: after %d consecutive %s failure(s) → "
            "browser_take_screenshot + inspect_screen",
            steer.steer_after,
            LOCATOR_TOOL,
        )
    mcp = build_playwright_mcp(config, storage_state=storage_state, process_tool_call=steer)

    # Optional reasoning effort (PLANNER_REASONING_EFFORT) — sent only when set, and
    # only trustworthy after step0d proved the gateway honors it (see _context helper).
    effort = reasoning_effort("PLANNER_REASONING_EFFORT")
    model_settings = (
        OpenAIChatModelSettings(openai_reasoning_effort=effort) if effort else None
    )

    agent = Agent(
        model=model,
        output_type=TestPlan,
        toolsets=[mcp],
        system_prompt=system_prompt,
        model_settings=model_settings,
        retries=agent_retries(),  # room to recover from transient MCP tool errors
        # Long explorations accumulate dozens of stale page snapshots; keep only the
        # newest few so the model stays out of its long-context degradation zone.
        capabilities=[ProcessHistory(trim_stale_snapshots)],
    )
    # Optional Vision Aid sensor (VISION_MAX_CALLS / PLANNER_VISION). Registered only when enabled
    # so a disabled run's toolset — and behaviour — is identical to before. The capture handle
    # drives browser_take_screenshot on this same live MCP so inspect_screen sees the current page.
    if config.vision_max_calls > 0:
        _register_inspect_screen(agent, config, capture=_make_screenshot_capture(mcp))
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

    # MCP toolset → the agent must be entered as an async context manager so the
    # Playwright MCP subprocess is started (and cleanly stopped) around the run.
    async with agent:
        result = await agent.run(
            user_message, usage_limits=UsageLimits(request_limit=agent_request_limit())
        )
        return result.output


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
