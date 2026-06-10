"""The Planner agent: ManualTestCase + live staging app (MCP) -> TestPlan.

The Planner reads a manual test case and, using the Playwright MCP toolset to
navigate the live staging app, produces a structured ``TestPlan`` with verified
selectors. Verifying selectors against the real app before committing them is the
key advantage over generating tests blind.

Implements AI_TEST_GENERATION_GUIDE.md §3.8 (+ §3.5b context loading). The Planner
gets BOTH context files (project_context.md and project_map.md).
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.usage import UsageLimits

from ..config import Config
from ..llm import build_openai_model
from ..models import ManualTestCase, TestPlan
from ..playwright_mcp import build_playwright_mcp
from ._context import agent_request_limit, agent_retries, assemble_system_prompt
from ._history import trim_stale_snapshots

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def build_planner(config: Config, storage_state: Path | None = None) -> Agent[None, TestPlan]:
    """Build the Planner agent (Playwright MCP toolset attached, output_type=TestPlan)."""
    model = build_openai_model(config, config.planner_model)

    base_prompt = (PROMPTS_DIR / "planner.md").read_text()
    system_prompt = assemble_system_prompt(config, base_prompt, include_map=True)

    mcp = build_playwright_mcp(config, storage_state=storage_state)

    return Agent(
        model=model,
        output_type=TestPlan,
        toolsets=[mcp],
        system_prompt=system_prompt,
        retries=agent_retries(),  # room to recover from transient MCP tool errors
        # Long explorations accumulate dozens of stale page snapshots; keep only the
        # newest few so the model stays out of its long-context degradation zone.
        capabilities=[ProcessHistory(trim_stale_snapshots)],
    )


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
