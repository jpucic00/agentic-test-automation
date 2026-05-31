"""The Generator agent: TestPlan -> Playwright TypeScript test file.

The Generator transforms a structured ``TestPlan`` into a complete, runnable
``.spec.ts`` file. It does NOT use Playwright MCP — the smaller, well-scoped task
yields better output from the code-optimized model.

Implements AI_TEST_GENERATION_GUIDE.md §3.9 (+ §3.5b context loading). The
Generator gets ONLY project_context.md (no application map) to keep its context
lean — it needs code conventions, not the route map.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent

from ..config import Config
from ..llm import build_openai_model
from ..models import GeneratedTest, TestPlan
from ._context import assemble_system_prompt

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def build_generator(config: Config) -> Agent[None, GeneratedTest]:
    """Build the Generator agent (no MCP toolset, output_type=GeneratedTest)."""
    model = build_openai_model(config, config.generator_model)

    base_prompt = (PROMPTS_DIR / "generator.md").read_text()
    system_prompt = assemble_system_prompt(config, base_prompt, include_map=False)

    return Agent(
        model=model,
        output_type=GeneratedTest,
        system_prompt=system_prompt,
        retries=2,
    )


async def generate_test(config: Config, plan: TestPlan) -> GeneratedTest:
    """Run the Generator on a TestPlan and return the generated Playwright test."""
    agent = build_generator(config)
    user_message = f"""Generate a Playwright TypeScript test from this plan.

```json
{plan.model_dump_json(indent=2)}
```

Requirements:
- Use Playwright's @playwright/test framework
- Use `test.describe` and `test()` blocks
- File should be a complete, runnable .spec.ts file
- Prefer ID selectors (`#login-button`) — this app uses them manually
- Add `await expect(...)` assertions for each step's expected outcome
- Use `await page.goto()` with the full staging URL from the plan
"""
    # No toolset → no async context manager needed; run the agent directly.
    result = await agent.run(user_message)
    return result.output
