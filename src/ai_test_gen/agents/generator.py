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


def _build_generation_message(
    plan: TestPlan,
    previous_code: str | None = None,
    error_text: str | None = None,
) -> str:
    """Assemble the Generator's user message; optionally with a compile-retry section.

    ``previous_code``/``error_text`` are set when a generated file failed to even
    compile/collect (the run produced no report) — the Generator gets its own output
    back with the error so it can fix the code without involving a browser agent.
    """
    message = f"""Generate a Playwright TypeScript test from this plan.

```json
{plan.model_dump_json(indent=2)}
```

Requirements:
- Use Playwright's @playwright/test framework
- Use `test.describe` and `test()` blocks
- File should be a complete, runnable .spec.ts file
- Use each step's `target_selector` locator AS-IS — prepend `page.` (e.g.
  `page.getByTestId('login-submit')`); never rewrite `getByTestId` to a `#id`/`data-testid`
- Assert each state-changing step's outcome via its `assert_selector` (verified) or
  `page.waitForURL(page_url)` — never invent visible text from the `expected` prose
- Use `await page.goto()` with the full staging URL from the plan
"""
    if previous_code is not None:
        message += f"""
## Previous attempt failed to run
A previous file generated from this plan never executed — Playwright could not
compile/collect it. Fix the code so it runs; keep the plan's steps, selectors,
and assertions unchanged.

### Previous code
```typescript
{previous_code}
```

### Compile/collection error
```
{(error_text or "(no error output captured)")[:2000]}
```
"""
    return message


async def generate_test(
    config: Config,
    plan: TestPlan,
    *,
    previous_code: str | None = None,
    error_text: str | None = None,
) -> GeneratedTest:
    """Run the Generator on a TestPlan and return the generated Playwright test.

    Pass ``previous_code`` + ``error_text`` to retry after a compile/collection
    failure (a run with ``did_run=False``); the plan itself is unchanged.
    """
    agent = build_generator(config)
    user_message = _build_generation_message(plan, previous_code, error_text)
    # No toolset → no async context manager needed; run the agent directly.
    result = await agent.run(user_message)
    return result.output
