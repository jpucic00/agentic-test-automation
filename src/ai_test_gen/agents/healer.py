"""The Healer agent: failed test + error trace -> fixed Playwright test.

The Healer is intentionally narrow: it only fixes a failing test, it never plans
or restructures. It gets the Playwright MCP toolset so it can inspect the live app
when an error indicates a selector issue. If it cannot fix the test within the
orchestrator's attempt budget, the failure is surfaced to humans.

Implements AI_TEST_GENERATION_GUIDE.md §3.10 (+ §3.5b context loading). The Healer
gets BOTH context files (project_context.md and project_map.md) in its system prompt,
and at heal time also receives the original ManualTestCase (intent) and the TestPlan —
including the Planner's notes and verified selectors — so it can diagnose the failure
against what the test is meant to do, not just the error text.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from ..config import Config
from ..llm import build_openai_model
from ..models import GeneratedTest, HealedTest, ManualTestCase, TestPlan, TestRunResult
from ..playwright_mcp import build_playwright_mcp
from ._context import agent_request_limit, agent_retries, assemble_system_prompt

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def build_healer(config: Config, storage_state: Path | None = None) -> Agent[None, HealedTest]:
    """Build the Healer agent (Playwright MCP toolset attached, output_type=HealedTest)."""
    model = build_openai_model(config, config.healer_model)

    base_prompt = (PROMPTS_DIR / "healer.md").read_text()
    system_prompt = assemble_system_prompt(config, base_prompt, include_map=True)

    mcp = build_playwright_mcp(config, storage_state=storage_state)

    return Agent(
        model=model,
        output_type=HealedTest,
        toolsets=[mcp],
        system_prompt=system_prompt,
        retries=agent_retries(),  # room to recover from transient MCP tool errors
    )


def _format_case_steps(test_case: ManualTestCase) -> str:
    """Render the manual test case's steps paired with their expected results (the intent)."""
    if not test_case.steps:
        return "(no steps recorded)"
    lines: list[str] = []
    for i, step in enumerate(test_case.steps):
        expected = test_case.expected_results[i] if i < len(test_case.expected_results) else ""
        line = f"{i + 1}. {step}"
        if expected:
            line += f"  -> expect: {expected}"
        lines.append(line)
    return "\n".join(lines)


def _format_plan_steps(plan: TestPlan) -> str:
    """Render the plan's steps with the Planner's verified selectors and expectations."""
    if not plan.steps:
        return "(no steps)"
    lines: list[str] = []
    for i, step in enumerate(plan.steps):
        lines.append(f"{i + 1}. {step.action}")
        if step.target_selector:
            lines.append(f"   verified selector: {step.target_selector}")
        if step.expected:
            lines.append(f"   expect: {step.expected}")
    return "\n".join(lines)


def _build_heal_message(
    test: GeneratedTest,
    failure: TestRunResult,
    plan: TestPlan,
    test_case: ManualTestCase,
    heal_history: list[str] | None = None,
) -> str:
    """Assemble the Healer's user message: intent + plan + failing code + failure.

    The original ``ManualTestCase`` (intent) and the ``TestPlan`` — especially the Planner's
    ``notes`` and verified selectors — are included so the Healer can reconcile the failing code
    against what the test is meant to do (add a skipped step, drop a hallucinated one), not just
    react to the error text.

    ``heal_history`` carries the ``changes_summary`` of every earlier heal attempt in this
    run. The Healer rewrites the whole file, so without that history attempt 2 can silently
    undo attempt 1's fix and ping-pong between two wrong versions.
    """
    planner_notes = plan.notes.strip() or "(none)"

    history_block = ""
    if heal_history:
        attempts = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(heal_history))
        history_block = f"""
## Previous heal attempts on this test (oldest first)
{attempts}

The failing code above ALREADY CONTAINS these changes, and the test STILL fails with the
error below. Do NOT undo a previous attempt's change unless the current error shows that
change itself was wrong — build on it or fix something else.
"""
    return f"""Fix this failing Playwright test.

Diagnose first: compare the ORIGINAL INTENT and the PLAN below against the failing code and the
error. The Planner/Generator may have SKIPPED a step the test case requires, or INVENTED a step
that isn't in it — reconcile the code with the intent. Keep the test faithful to the intent: never
make it green by dropping a real check or testing something the case didn't ask for.

## Original test case (the intent — {test_case.key})
{test_case.title}

Steps:
{_format_case_steps(test_case)}

## Plan it was generated from
- Target URL: {plan.target_url}
- Planner notes (flaky behavior / auth quirks / alternative selectors observed live):
{planner_notes}

Planned steps (selectors here were verified live by the Planner):
{_format_plan_steps(plan)}

## Failing test
**File:** {test.file_name}

```typescript
{test.code}
```
{history_block}
**Failure:**
- Status: {failure.status}
- Error: {failure.error_message}

**stderr:**
```
{failure.stderr[:2000]}
```

You may navigate the staging app and call browser_generate_locator on an element's ref to capture a
VERIFIED locator — for any selector you fix AND any step you add, don't hand-write it. Prefer a
Planner-verified selector (above) over the one in the failing code, and honor the Planner's notes.
Make the change needed to reconcile the test with the intent and make it pass; prefer the smallest
such change. Do not add unrelated test cases or assertions the test case didn't ask for.
"""


async def heal_test(
    config: Config,
    test: GeneratedTest,
    failure: TestRunResult,
    plan: TestPlan,
    test_case: ManualTestCase,
    storage_state: Path | None = None,
    heal_history: list[str] | None = None,
) -> HealedTest:
    """Run the Healer on a failing test + its failure result and return the fix.

    ``heal_history`` is the list of earlier attempts' ``changes_summary`` for this
    run, so a later attempt builds on (rather than undoes) the previous fix.
    """
    agent = build_healer(config, storage_state=storage_state)
    user_message = _build_heal_message(test, failure, plan, test_case, heal_history)
    # MCP toolset → enter the agent as an async context manager around the run.
    async with agent:
        result = await agent.run(
            user_message, usage_limits=UsageLimits(request_limit=agent_request_limit())
        )
        return result.output
