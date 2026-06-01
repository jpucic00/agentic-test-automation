"""The Healer agent: failed test + error trace -> fixed Playwright test.

The Healer is intentionally narrow: it only fixes a failing test, it never plans
or restructures. It gets the Playwright MCP toolset so it can inspect the live app
when an error indicates a selector issue. If it cannot fix the test within the
orchestrator's attempt budget, the failure is surfaced to humans.

Implements AI_TEST_GENERATION_GUIDE.md §3.10 (+ §3.5b context loading). The Healer
gets BOTH context files (project_context.md and project_map.md).
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from ..config import Config
from ..llm import build_openai_model
from ..models import GeneratedTest, HealedTest, TestRunResult
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


async def heal_test(
    config: Config,
    test: GeneratedTest,
    failure: TestRunResult,
    storage_state: Path | None = None,
) -> HealedTest:
    """Run the Healer on a failing test + its failure result and return the fix."""
    agent = build_healer(config, storage_state=storage_state)
    user_message = f"""Fix this failing Playwright test.

**File:** {test.file_name}

**Test code:**
```typescript
{test.code}
```

**Failure:**
- Status: {failure.status}
- Error: {failure.error_message}

**stderr:**
```
{failure.stderr[:2000]}
```

You may navigate the staging app to verify the correct selectors before fixing.
Only change what is necessary to make the test pass. Do not restructure or add new tests.
"""
    # MCP toolset → enter the agent as an async context manager around the run.
    async with agent:
        result = await agent.run(
            user_message, usage_limits=UsageLimits(request_limit=agent_request_limit())
        )
        return result.output
