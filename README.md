# agentic-test-automation

AI-driven Playwright test generation. Three narrow LLM agents — **Planner → Generator → Healer** —
turn a manual Jira/Xray test case into a reviewed Playwright test and open a merge request for a human
to approve. The agents drive a real staging browser through Playwright MCP and verify their work against
the live app. Nothing is ever auto-merged, and the pipeline only ever runs against **staging, never production**.

## How it works

```
Jira/Xray key → fetch → Plan → Generate → Run → (Heal ↺) → open MR → human review
```

- **Planner** opens the staging app via Playwright MCP, walks the scenario, and captures *verified*
  selectors (read-only `browser_generate_locator`) into a typed `TestPlan`.
- **Generator** turns the plan into a runnable `.spec.ts` — no browser, because a focused code model
  writes better code from a precise plan.
- **Test Runner** executes the test against staging and reports pass/fail plus a trace.
- **Healer** inspects any failure in the live app and makes a *minimal* fix, retrying up to a
  configurable cap. If the failure is a genuine app bug, it leaves the test alone so the regression
  surfaces honestly instead of being "fixed" away.
- **GitLab Client** opens a merge request labeled `ai-generated` + `qa-review-needed` (optional — can be
  skipped for local runs).

A structured `TestPlan` is the contract between stages, so each step consumes a precise schema instead of
free text. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the design and
[`docs/WORKFLOW.md`](docs/WORKFLOW.md) for the run-time flow.

## Quick start

1. **Set up your machine** — Python 3.12, Node 20, `uv`. Full instructions in [`SETUP.md`](SETUP.md).
2. **Configure** — `cp .env.example .env` and fill in your model gateway, Jira/Xray, staging app, and
   (optionally) GitLab values. Every variable is documented inline.
3. **Verify access** — run the Step 0 scripts to confirm your gateway, Jira/Xray, and embedding
   endpoints respond (see [`scripts/README.md`](scripts/README.md)).
4. **Describe your app** — copy the two context templates and fill them in:
   ```bash
   cp project_context.example.md project_context.md   # conventions, test users, quirks
   cp project_map.example.md   project_map.md          # routes, auth flow, key screens
   ```
5. **Generate a test:**
   ```bash
   uv run python -m ai_test_gen.orchestrator QA-1234 --verbose
   ```

## Run in Docker

The pipeline ships as a container (official Playwright base image, non-root user, pinned dependencies).
To generate a test without GitLab — the agents call the gateway + Xray, run the test, and write the
result to a volume for review:

```bash
cp .env.example .env          # fill LLM_*, JIRA_*, STAGING_* (GitLab not needed)
docker compose build
docker compose run --rm pipeline QA-1234 --verbose
```

`GITLAB_ENABLED=false` (set in [`docker-compose.yml`](docker-compose.yml)) skips the MR step. To open an
MR instead, set `GITLAB_ENABLED=true` and provide the `GITLAB_*` vars. If your gateway uses a private CA,
mount it and set `SSL_CERT_FILE` + `REQUESTS_CA_BUNDLE` (see the compose file).

## Configuration

All configuration is environment variables, documented section-by-section in [`.env.example`](.env.example):

- **Model gateway** — any OpenAI-compatible endpoint; one model each for the Planner, Generator, and Healer.
- **Jira/Xray** — Cloud (API token) or Server/Data Center (PAT); the source of manual test cases.
- **Staging app** — the URL under test plus credentials. A fail-closed guardrail refuses to start unless
  the host looks non-production.
- **GitLab** — optional merge-request destination.
- **mTLS / proxy / private CA** — optional, for gateways that sit behind them.

## Repository layout

- [`SETUP.md`](SETUP.md) — install, configure, verify, run.
- [`scripts/README.md`](scripts/README.md) — the Step 0 access-verification scripts.
- [`docs/`](docs/) — architecture and run-time workflow.
- [`AI_TEST_GENERATION_GUIDE.md`](AI_TEST_GENERATION_GUIDE.md) — the deep, code-level build guide.
- [`project_context.example.md`](project_context.example.md) / [`project_map.example.md`](project_map.example.md)
  — templates for the per-app context the agents consume.

## Adopting this for your team

This scaffold is meant to be forked and adapted:

1. Point the [`.env.example`](.env.example) values at your own gateway / Jira tenant / GitLab instance.
2. Walk through [`SETUP.md`](SETUP.md) on a clean machine and add a row to its troubleshooting table for
   anything that didn't work first try.
3. Fill in [`project_context.md`](project_context.md) and [`project_map.md`](project_map.md) for your app —
   the templates spell out the level of detail to provide.
