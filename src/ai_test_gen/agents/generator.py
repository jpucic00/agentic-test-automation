"""The Generator agent: TestPlan -> Playwright TypeScript test file.

The Generator transforms a structured ``TestPlan`` into a complete, runnable
``.spec.ts`` file. It does NOT use Playwright MCP — the smaller, well-scoped task
yields better output from the code-optimized model.

Implements AI_TEST_GENERATION_GUIDE.md §3.9 (+ §3.5b context loading). The
Generator gets ONLY project_context.md (no application map) to keep its context
lean — it needs code conventions, not the route map.

The plan it sees has every URL on a configured environment rewritten to a baseURL-relative
path (``https://staging.example.com/notes`` → ``/notes``), so the generated spec navigates
with ``page.goto('/notes')`` and runs unchanged on every ``STAGING_BASE_URL`` environment;
URLs on other hosts (``STAGING_EXTRA_URLS``) stay absolute.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic_ai import Agent, AgentRetries

from ..allowlist import relative_to
from ..config import Config
from ..llm import build_openai_model
from ..models import GeneratedTest, TestPlan
from ._context import agent_output_retries, assemble_system_prompt
from ._run_failure import run_agent_logged

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
        retries=AgentRetries(tools=2, output=agent_output_retries()),  # output: serving husks
    )


def _relative_plan(plan: TestPlan, app_origins: Sequence[str]) -> TestPlan:
    """A copy of ``plan`` whose URLs on ``app_origins`` are baseURL-relative paths."""
    steps = [
        step.model_copy(update={"page_url": relative_to(step.page_url, app_origins)})
        if step.page_url
        else step
        for step in plan.steps
    ]
    return plan.model_copy(
        update={"target_url": relative_to(plan.target_url, app_origins), "steps": steps}
    )


def _build_generation_message(
    plan: TestPlan,
    previous_code: str | None = None,
    error_text: str | None = None,
    app_origins: Sequence[str] = (),
) -> str:
    """Assemble the Generator's user message; optionally with a compile-retry section.

    ``previous_code``/``error_text`` are set when a generated file failed to even
    compile/collect (the run produced no report) — the Generator gets its own output
    back with the error so it can fix the code without involving a browser agent.
    ``app_origins`` (the configured environments) are stripped from the plan's URLs so
    the spec's navigation and URL assertions are baseURL-relative.
    """
    message = f"""Generate a Playwright TypeScript test from this plan.

```json
{_relative_plan(plan, app_origins).model_dump_json(indent=2)}
```

Requirements:
- Use Playwright's @playwright/test framework
- Use `test.describe` and `test()` blocks
- File should be a complete, runnable .spec.ts file
- Use each step's `target_selector` locator AS-IS — prepend `page.` (e.g.
  `page.getByTestId('login-submit')`); never rewrite `getByTestId` to a `#id`/`data-testid`
- Assert each state-changing step's outcome via its `assert_selector` (verified) or
  `page.waitForURL(page_url)` — never invent visible text from the `expected` prose
- Use the plan's URLs exactly as given: app pages are baseURL-relative paths (`page.goto('/')`,
  `page.waitForURL('/notes')`); only other hosts are absolute
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
    user_message = _build_generation_message(
        plan, previous_code, error_text, app_origins=config.environment_origins
    )
    # run_agent_logged captures the run's messages so retry exhaustion (e.g. the model
    # answering in prose instead of emitting GeneratedTest) logs its evidence like the
    # browser agents do; entering the toolset-less agent is a no-op context.
    return await run_agent_logged(agent, user_message, agent_label="Generator")
