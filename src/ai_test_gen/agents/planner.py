"""The Planner agent: ManualTestCase + live staging app (MCP) -> TestPlan.

The Planner reads a manual test case and, using the Playwright MCP toolset to
navigate the live staging app, produces a structured ``TestPlan`` with verified
selectors. Verifying selectors against the real app before committing them is the
key advantage over generating tests blind.

Implements AI_TEST_GENERATION_GUIDE.md §3.8 (+ §3.5b context loading). The Planner
gets BOTH context files (project_context.md and project_map.md).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

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
from .vision import ask_vision

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# A screenshot older than this (seconds) is treated as stale by inspect_screen: the model must
# take a fresh browser_take_screenshot before the vision sensor will describe "the current page".
_STALE_AFTER_S = 5.0


def _latest_png(directory: Path) -> Path | None:
    """Newest ``*.png`` under ``directory`` by mtime, or None if there are none."""
    pngs = list(directory.rglob("*.png"))
    if not pngs:
        return None
    return max(pngs, key=lambda p: p.stat().st_mtime)


def _register_inspect_screen(
    agent: Agent[None, TestPlan], config: Config
) -> Callable[[str], Coroutine[Any, Any, str]]:
    """Attach the optional Devstral ``inspect_screen`` vision tool to a Planner agent.

    gpt-oss stays the MCP driver; this tool just turns the latest screenshot into a short text
    answer (via ``ask_vision``) so the text-only Planner can "see". Per-run call budget =
    ``config.vision_max_calls``. Advisory only — it never returns a selector. Also returns the
    tool function (the registration target), which unit tests call directly.
    """
    max_calls = config.vision_max_calls
    calls_made = 0

    async def inspect_screen(question: str) -> str:
        """Look at the CURRENT page screenshot and answer a question about what is visible.

        Use when the accessibility snapshot is ambiguous or silent — to confirm a dropdown
        opened, a modal/overlay is covering the page, a success/error toast appeared, or whether
        an element is actually visible. FIRST call browser_take_screenshot to capture the current
        page, THEN call this with a specific question, e.g. "Is a modal dialog covering the page?"
        or "Did a success toast appear?". Returns a short text description. It NEVER returns a
        selector — keep using browser_generate_locator for targeting.
        """
        nonlocal calls_made
        if calls_made >= max_calls:
            return (
                f"Vision budget reached ({max_calls} calls this run). Proceed using the "
                "accessibility snapshot."
            )
        png = _latest_png(config.snapshots_dir)
        if png is None:
            return (
                "No screenshot is available yet. Call browser_take_screenshot first, then retry "
                "inspect_screen."
            )
        if time.time() - png.stat().st_mtime > _STALE_AFTER_S:
            return (
                f"The latest screenshot is stale (older than {_STALE_AFTER_S:.0f}s). Call "
                "browser_take_screenshot first, then retry inspect_screen."
            )
        calls_made += 1
        return await ask_vision(config, question, png.read_bytes())

    agent.tool_plain(inspect_screen)
    return inspect_screen


def build_planner(config: Config, storage_state: Path | None = None) -> Agent[None, TestPlan]:
    """Build the Planner agent (Playwright MCP toolset attached, output_type=TestPlan)."""
    model = build_openai_model(config, config.planner_model)

    base_prompt = (PROMPTS_DIR / "planner.md").read_text()
    if config.vision_max_calls > 0:
        # Gated so a disabled run's system prompt is byte-identical to before.
        base_prompt += "\n\n" + (PROMPTS_DIR / "planner_vision.md").read_text()
    system_prompt = assemble_system_prompt(config, base_prompt, include_map=True)

    mcp = build_playwright_mcp(config, storage_state=storage_state)

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
    # Optional Devstral vision sensor (PLANNER_VISION). Registered only when enabled so a
    # disabled run's toolset — and behaviour — is identical to before.
    if config.vision_max_calls > 0:
        _register_inspect_screen(agent, config)
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

Now build a TestPlan. Navigate the staging app and, for each element you act on, call
browser_generate_locator on its ref to capture a VERIFIED locator — never hand-write one.
The app's manual id= attributes surface as getByTestId('...'); elements without an id come
back as getByRole/getByLabel. Record each locator verbatim in target_selector.
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
