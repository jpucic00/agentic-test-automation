# agentic-test-automation

AI-driven Playwright test generation scaffold — Planner / Generator / Healer agents that convert manual Jira/Xray test cases into Playwright tests and open GitLab merge requests, running entirely on internal AI infrastructure.

## Status — Phase 0 (first release)

This release covers **access, tooling, and gateway verification only**. There is no agent code yet; the pipeline itself is built in Phase 1+. Phase 0 ships:

- [`SETUP.md`](SETUP.md) — install instructions for the private PC (authoring) and the company laptop (runtime).
- [`.env.example`](.env.example) — comprehensive environment template, one section per dependency (LLM gateway, embed/rerank, Jira/Xray, staging, GitLab).
- [`pyproject.toml`](pyproject.toml) — minimal Python 3.12 project (`openai`, `python-dotenv`, `httpx`, `requests`) so the verification scripts run via `uv run`.
- [`scripts/step0_verify_tool_calling.py`](scripts/step0_verify_tool_calling.py) — confirms the gateway proxies `tools` / `tool_choice` correctly for the three candidate models.
- [`scripts/step0b_verify_embeddings.py`](scripts/step0b_verify_embeddings.py) — confirms `/embeddings` and a rerank endpoint respond (needed for Phase 2.5 RAG).
- [`scripts/step0c_xray_flavor.py`](scripts/step0c_xray_flavor.py) — detects Xray Cloud vs Server/DC and finds the "test steps" custom field ID.

### Not yet here (deferred to later phases)

- Planner / Generator / Healer agents
- Playwright MCP wiring, browser orchestration
- Xray client, GitLab MR opener, end-to-end orchestrator
- Filled-in `project_context.md` / `project_map.md`
- Dockerfile, GitLab CI, RAG indexing

> **Phase 1.A is now in progress.** The `src/ai_test_gen/` package skeleton has been scaffolded as
> stubs (each module names the guide section + the task that implements it), and `pyproject.toml` is
> now an installable package (`uv run python -c 'import ai_test_gen'` succeeds). The items above
> remain unimplemented — stubs are not behavior.

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
