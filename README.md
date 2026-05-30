# agentic-test-automation

AI-driven Playwright test generation scaffold — Planner / Generator / Healer agents that convert manual Jira/Xray test cases into Playwright tests and open GitLab merge requests, running entirely on internal AI infrastructure.

## Status — Phase 0 (first release)

This release covers **access, tooling, and gateway verification only**. There is no agent code yet; the pipeline itself is built in Phase 1+. Phase 0 ships:

- [`SETUP.md`](SETUP.md) — install instructions for the private PC (authoring) and the company laptop (runtime).
- [`.env.example`](.env.example) — comprehensive environment template, one section per dependency (LLM gateway, embed/rerank, Jira/Xray, staging, GitLab).
- [`pyproject.toml`](pyproject.toml) — Python 3.12 project; every runtime + dev dependency is pinned to an exact version (`==`) so `uv sync --frozen` is reproducible. Phase 0 itself needs only `openai` / `python-dotenv` / `httpx` / `requests` / `cryptography` to run the verification scripts via `uv run`.
- [`scripts/step0_verify_tool_calling.py`](scripts/step0_verify_tool_calling.py) — confirms the gateway proxies `tools` / `tool_choice` correctly for the three candidate models.
- [`scripts/step0b_verify_embeddings.py`](scripts/step0b_verify_embeddings.py) — confirms `/embeddings` and a rerank endpoint respond (needed for Phase 2.5 RAG).
- [`scripts/step0c_xray_flavor.py`](scripts/step0c_xray_flavor.py) — detects Xray Cloud vs Server/DC and finds the "test steps" custom field ID.

### Not yet here (deferred to later phases)

- Planner / Generator / Healer agents
- Agents that actually drive the browser (the Playwright MCP toolset + auth-state capture landed in Phase 1.B; the agents that consume them are 1.C/1.D)
- GitLab MR opener, end-to-end orchestrator
- Filled-in `project_context.md` / `project_map.md`
- Dockerfile, GitLab CI, RAG indexing

> **Phase 1.A is now in progress.** The `src/ai_test_gen/` package skeleton has been scaffolded as
> stubs (each module names the guide section + the task that implements it), `pyproject.toml` is
> now an installable package (`uv run python -c 'import ai_test_gen'` succeeds), and the full pipeline
> dependency set is pinned to exact versions (`pydantic-ai`, `python-gitlab`, `atlassian-python-api`,
> dev `pytest`). Modules implemented beyond stubs so far:
>
> - [`config.py`](src/ai_test_gen/config.py) — centralized config (`load_config()`) with a fail-closed
>   **prod-URL guardrail** (refuses to start unless `STAGING_BASE_URL`'s host carries a non-prod marker;
>   extend via `NON_PROD_URL_MARKERS`). Covered by [`tests/test_config.py`](tests/test_config.py).
> - [`models.py`](src/ai_test_gen/models.py) — the six Pydantic models that flow between agents (§3.5);
>   every field carries a `description` so they serialize into Pydantic AI structured-output schemas.
>   Covered by [`tests/test_models.py`](tests/test_models.py).
> - [`xray_client.py`](src/ai_test_gen/xray_client.py) — fetches a manual test case from Jira/Xray into a
>   `ManualTestCase`. This tenant is **Server/DC**: Bearer PAT, steps read from `customfield_11006` via
>   `/rest/api/2/issue?expand=names`. Offline tests in [`tests/test_xray_client.py`](tests/test_xray_client.py);
>   a live fetch is a company-laptop check (`uv run python scripts/test_xray.py --issue-key <KEY>`).
>
> Run the local suite with `uv run pytest`. The remaining items above are still unimplemented — stubs are not behavior.

> **Phase 1.B (Playwright MCP & authentication) — core landed.** The browser layer and the auth-wall fix:
>
> - [`playwright_mcp.py`](src/ai_test_gen/playwright_mcp.py) — `build_playwright_mcp()` returns an
>   `MCPToolset` (pydantic-ai 1.104.0; `MCPServerStdio` is deprecated) running a **pinned**
>   `@playwright/mcp@0.0.75`, configured by [`playwright-mcp-config.json`](playwright-mcp-config.json)
>   (accessibility-tree only — `imageResponses: omit`). Attach via `Agent(model, toolsets=[...])`.
> - [`scripts/save_auth_state.py`](scripts/save_auth_state.py) + [`scripts/verify_auth_state.py`](scripts/verify_auth_state.py)
>   — capture a logged-in `storage_state.json` once and confirm it authenticates, so agents don't
>   re-login on every run. **Company-laptop runtime** (needs staging + the real login selectors).
> - [`output/`](output/) Playwright harness — `playwright.config.ts` + a pinned `package.json`
>   (`@playwright/test==1.60.0`, matching the Python `playwright==1.60.0`); `npx playwright test --list`
>   compiles clean. Run `cd output && npm install` once.
>
> Adds one Python dependency: `playwright==1.60.0`. Multi-role storage state was deferred to Phase 1.D
> (its acceptance test needs the orchestrator that consumes it).

The full roadmap lives in [`AI_TEST_GENERATION_GUIDE.md`](AI_TEST_GENERATION_GUIDE.md). Each future phase is also a Flux epic on the [project board](http://localhost:4242).

## Where to look next

- **Setting up your machine:** [`SETUP.md`](SETUP.md)
- **Running the Step 0 scripts:** [`scripts/README.md`](scripts/README.md)
- **Build guide for everything past Phase 0:** [`AI_TEST_GENERATION_GUIDE.md`](AI_TEST_GENERATION_GUIDE.md)
- **Project conventions, agent task tracking, release rules:** [`CLAUDE.md`](CLAUDE.md)

## Running the release on the company laptop

The private PC writes the code; the company laptop runs it (the corporate gateway, Jira/Xray, GitLab, and staging are not reachable from outside the network). After this release lands, the workflow on the company laptop is:

1. `git pull` on the company laptop.
2. Follow [`SETUP.md` §3](SETUP.md#3-company-laptop-runtime-setup) — install toolchain, `cp .env.example .env`, fill in values.
3. Run the three Step 0 scripts (`uv run python scripts/step0_*.py`).
4. Run the two manual smokes (GitLab PAT curl, staging browser login).
5. Report results back to whoever owns the Flux board so the Phase 0 tasks can move to `done`.

The exact ordered checklist is surfaced inline in chat at release time (per the "Release mode" rules in [`CLAUDE.md`](CLAUDE.md)) — it is not committed as a file because the items decay quickly and we don't want a stale `RELEASE_CHECKLIST.md` rotting in the repo.

## Adopting this for another team

This scaffold is intended to be shared. To adopt it for a different gateway / Jira tenant / GitLab instance:

1. Fork the repo.
2. Adjust [`.env.example`](.env.example) defaults that mention `yourcompany` and `your-internal-gateway` to your real URLs (still placeholders, just relevant ones).
3. Walk through [`SETUP.md`](SETUP.md) on a fresh machine and add a row to the troubleshooting table for anything that didn't work first try.
4. When you reach Phase 1, fill in [`project_context.md`](project_context.md) and [`project_map.md`](project_map.md) for your app — the templates spell out what level of detail to provide.
