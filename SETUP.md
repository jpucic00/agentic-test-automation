# Setup

This guide takes you from a clean machine to generating your first test. Everything runs from one
machine that can reach your model gateway, Jira/Xray, staging app, and (optionally) GitLab. If those
services sit behind a proxy, mTLS, or a private CA, see [section 8](#8-optional-gateway-behind-a-proxy-mtls-or-private-ca).

---

## 1. Prerequisites

| Tool | Min version | Why |
|---|---|---|
| Python | 3.12 | Pinned in [`.python-version`](.python-version); `pydantic-ai` and modern type-hint features need it |
| Node.js | 20 | Required by `@playwright/mcp` and the `output/` Playwright test harness |
| `uv` | latest | Python project + dependency manager; replaces pip+venv+pip-tools |
| `git` | any recent | Source control |

### macOS install

```bash
# Homebrew (skip if already installed):
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python@3.12 node git
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL -l   # reload PATH so `uv` is visible
```

`brew install python@3.12` does **not** override the system `python3`. Use `python3.12` explicitly, or
rely on `uv` to find it (recommended — `uv` reads `.python-version`).

### Linux (Ubuntu / Debian) install

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL -l
```

If your distro doesn't ship Python 3.12, use `pyenv` or install from the deadsnakes PPA.

### Windows install

Use the official installers:

- Python 3.12 from <https://www.python.org/downloads/> (tick "Add to PATH")
- Node.js 20 LTS from <https://nodejs.org/>
- Git from <https://git-scm.com/>
- `uv`: `irm https://astral.sh/uv/install.ps1 | iex` (PowerShell)

Then reopen the terminal so PATH updates take effect.

### Verify

```bash
python3.12 --version    # 3.12.x
node --version          # v20.x or newer
uv --version            # any 0.x
git --version           # any
```

If any of these fail, fix before moving on.

---

## 2. Install

```bash
git clone https://github.com/<your-fork>/agentic-test-automation.git
cd agentic-test-automation
uv sync                                        # creates .venv, installs deps, editable-installs ai_test_gen
uv run python -c 'import ai_test_gen'          # smoke-check the package layout imports
uv run ruff check .                            # lint
uv run pyright                                 # type-check
uv run pytest -q                               # offline unit suite (config, models, agents) — no network

# Playwright runtime (needed to run generated tests against your app):
uv run playwright install chromium             # one-time: the Chromium binary the Python `playwright` pkg drives
(cd output && npm install)                     # the output/ Node harness → output/node_modules (gitignored); commit output/package-lock.json
# The @playwright/mcp server bundles its OWN playwright-core, pinned to a DIFFERENT Chromium
# build than the test runner above. Install that browser via its bundled core too, or the
# Planner refuses with "Chromium browser is not installed" (it's a revision mismatch, not a
# missing install — both builds end up side by side under ~/.cache/ms-playwright):
node output/node_modules/@playwright/mcp/node_modules/playwright-core/cli.js install chromium
```

If `uv sync` errors with "no interpreter found for Python 3.12", install Python 3.12 (see
[Prerequisites](#1-prerequisites)) — `uv` reads [`.python-version`](.python-version) and refuses to
substitute a different minor version.

---

## 3. Access prerequisites (file requests for missing ones now)

Confirm you have, or have filed access requests for, every item below. Access often takes longer than the code does.

- [ ] **LLM gateway base URL** (OpenAI-compatible, e.g. `https://your-gateway/v1`)
- [ ] **LLM gateway API key**
- [ ] **Model names available on the gateway** — one each for the Planner, Generator, and Healer
  (defaults: `openai/gpt-oss-120b`, `mistralai/devstral-small-2-2512`, `openai/gpt-oss-120b`)
- [ ] **Embedding + reranker model names** on the gateway (defaults: `mxbai-embed-large`, `bge-reranker-v2-m3`)
- [ ] **Jira/Xray credentials with read access** to your test project:
  - Cloud → Atlassian email + API token (from <https://id.atlassian.com/manage-profile/security/api-tokens>)
  - Server/DC → username + PAT (or password)
- [ ] **Jira base URL** (e.g. `https://your-org.atlassian.net` or self-hosted)
- [ ] **GitLab personal access token** with `api` scope (User Settings → Access Tokens), OR a project access
  token with `Developer` role + `write_repository` — only if you want the pipeline to open MRs
- [ ] **Target GitLab repo** that will receive MRs (its path or numeric ID)
- [ ] **Staging app URL** under test + working credentials there

---

## 4. Configure your `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Each section in [`.env.example`](.env.example) names the script that consumes it, so you can fill one
section at a time. Watch out for:

- `LLM_BASE_URL` — include the trailing `/v1` if your gateway uses it.
- `XRAY_IS_CLOUD` — `true` for `*.atlassian.net`, `false` for self-hosted.
- `JIRA_TOKEN` — for Cloud this is an **API token**, not your Jira password.
- `GITLAB_PROJECT_ID` — `group/subgroup/project` path or the numeric ID. URL-encoded path is also accepted
  (`group%2Fproject`).
- `STAGING_BASE_URL` — must point at a non-production host; `load_config()` hard-fails otherwise (see the
  guardrail note in `.env.example`).

`.env` is in [`.gitignore`](.gitignore) — it will never be staged.

---

## 5. Verify access (Step 0)

In order:

```bash
uv run python scripts/step0_verify_tool_calling.py
uv run python scripts/step0b_verify_embeddings.py
uv run python scripts/step0c_xray_flavor.py --issue-key <one-real-QA-key>
```

> **Gateways with strict structured-output backends** (e.g. vLLM's `xgrammar`) compile every
> advertised tool schema and fail the whole request on a single unsupported JSON-Schema construct
> (`… features not supported by the xgrammar`). The pipeline advertises only grammar-clean
> schemas: two Playwright MCP tools whose generated schemas use such constructs (`browser_drop`,
> `browser_network_request`) are excluded from the agents' toolset, and a schema-scan test in
> `tests/test_playwright_mcp.py` guards the rest — re-run it after bumping `@playwright/mcp`.

Plus two manual smokes:

```bash
# GitLab PAT reaches the target project (expect HTTP 200 + JSON):
curl -sS -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_BASE_URL/api/v4/projects/$(printf '%s' "$GITLAB_PROJECT_ID" | sed 's|/|%2F|g')" \
  | head -c 200

# Staging login works (open in browser):
open "$STAGING_BASE_URL"   # macOS; use xdg-open on Linux or start on Windows
```

Detailed expected output and a failure-mode table are in [`scripts/README.md`](scripts/README.md).

### 5.1 Xray client check

`step0c` only *detects* the steps field; this exercises the actual client (`XrayClient.fetch()` →
`ManualTestCase`). For Server/DC, needs `XRAY_IS_CLOUD=false` and `JIRA_TOKEN` set to your PAT (sent as Bearer):

```bash
uv run python scripts/test_xray.py --issue-key <one-real-QA-key>
```

Expect a `ManualTestCase` JSON with **non-empty `steps` and `expected_results`**. If your tenant's steps
field isn't the default, set `XRAY_STEPS_FIELD_ID` to the ID that `step0c_xray_flavor.py --issue-key` reports.

---

## 6. Describe your app (`project_context.md` + `project_map.md`)

The agents read two human-authored context files. Copy the templates and fill them in for your app:

```bash
cp project_context.example.md project_context.md   # conventions, test users (disposable creds), quirks
cp project_map.example.md   project_map.md          # routes, auth/login flow, key screens
```

`project_context.md` goes to every agent; `project_map.md` goes only to the browser-driving agents
(Planner, Healer). Each template is a guided questionnaire: every section has a short purpose
comment, bullets phrased as `<questions to answer in place>`, and tables with a `<…>` format row to
copy per entry — work through it top to bottom, replacing each placeholder. **Don't list element
ids/selectors** in either file: the agents capture locators live from the running app (the
resilience ladder — id → accessible → CSS → XPath), so you describe routes, flows, roles, and
conventions, not how to find each element.

The loader strips HTML comments (`<!-- … -->`) before injecting these files — write guidance for
humans in comments freely; it never reaches the model. If a file still contains template
placeholders (the `<e.g. …>` / `<APP NAME>` style, or legacy `[REPLACE …]` markers), every run logs
a **warning** naming the file: the agents are then being prompted with the template's fictional
examples instead of your app's real conventions, and plan quality will reflect that. Fill the files
in until the warning disappears.

**Authentication is context-driven — no saved session.** Each agent, and each generated test, logs in
live as the role the scenario needs, using the disposable credentials and login flow you record in
`project_context.md` (test users) and `project_map.md` (auth flow — e.g. a Keycloak SSO form). There is no
stored `storage_state` (sessions expire between runs, and most cases need a different role or must register
first).

---

## 7. Run the pipeline

Generate a test for one Jira/Xray key:

```bash
uv run python -m ai_test_gen.orchestrator QA-1234 --verbose
```

The Orchestrator fetches the case, plans it against your staging app, generates the `.spec.ts`, runs it,
heals on failure (up to the configured cap), and — unless `GITLAB_ENABLED=false` — opens a merge request.
The generated test and its plan are written to `output/`. To run the whole thing in a container instead, see
the [Docker section in the README](README.md#run-in-docker).

### 7.1 Or: try it against the bundled demo app (no Jira/staging needed)

To watch the pipeline run without a Jira/Xray tenant or a staging environment — only a model gateway is
required — use the bundled demo app and its ready-made test cases. This is the fastest end-to-end check of
your gateway and lets you skip the Jira access (§3), the Xray client check (§5.1), and "describe your app"
(§6) steps.

```bash
# 1. Start the demo app (separate shell; serves http://localhost:3000):
cd packages/demo-notes-app && npm install && npm run dev

# 2. In your .env, enable the "Demo profile" block from .env.example:
#      TESTCASE_SOURCE=local
#      LOCAL_TESTCASE_DIR=packages/demo-notes-app/test-cases
#      PROJECT_CONTEXT_PATH=packages/demo-notes-app/project_context.md
#      PROJECT_MAP_PATH=packages/demo-notes-app/project_map.md
#      STAGING_BASE_URL=http://localhost:3000
#      GITLAB_ENABLED=false

# 3. Generate a test for a bundled case (NOTE-1 .. NOTE-4):
uv run python scripts/run_one.py NOTE-2 --verbose
```

`TESTCASE_SOURCE=local` reads test cases from `packages/demo-notes-app/test-cases/` (raw-Xray-shaped JSON —
the same shape the live Xray client produces, just read from disk), and the `PROJECT_*_PATH` overrides point
the agents at the demo's own committed `project_context.md` / `project_map.md` without touching your app's
root context files. See [`packages/demo-notes-app/README.md`](packages/demo-notes-app/README.md).

---

## 8. Optional: gateway behind a proxy, mTLS, or private CA

Skip this section unless your gateway / Jira / GitLab require one of the following. All three are off by default.

### Proxy

By default the pipeline (and the Step 0 scripts) connect **directly** and ignore the environment's
`HTTP(S)_PROXY` / `NO_PROXY`. If your gateway or Jira is reachable **only** through a proxy, set:

```
USE_HTTP_PROXY=true
```

httpx (the gateway client) and requests (the Xray + GitLab clients) then honor the standard proxy variables.

### mTLS client certificate

If the gateway requires mutual TLS, you'll have a `.pfx`/`.p12` bundle plus a password. Uncomment the mTLS
block at the top of `.env`:

```
MTLS_PKCS12_FILE=/absolute/path/to/client.pfx
MTLS_PKCS12_PASSWORD=<password>
```

`.pfx` and `.p12` are the same format — `MTLS_PKCS12_FILE` accepts either. The path must be **absolute**
(`.env` does not expand `~`). Sanity-check the bundle outside Python with:

```bash
openssl pkcs12 -in /absolute/path/to/client.pfx -info -noout -passin pass:"$MTLS_PKCS12_PASSWORD"
```

### Private CA

If your gateway uses a private CA not in certifi's default bundle, point both `SSL_CERT_FILE` (httpx) and
`REQUESTS_CA_BUNDLE` (requests) at the same PEM bundle in the mTLS block. Most cloud-hosted gateways don't need this.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `uv sync` says "no Python 3.12 interpreter found" | Install Python 3.12 via the platform installer in section 1; `uv` will not substitute another minor version |
| Scripts fail with "server disconnected without sending a response" (the TLS/mTLS handshake succeeds first) | Routing through an environment-configured proxy is dropping the request. The scripts connect directly by default — verify you have **not** set `USE_HTTP_PROXY=true`. A direct `curl` should return 200 while the same `curl` through the proxy fails identically. If the endpoint is reachable **only** through a proxy, set `USE_HTTP_PROXY=true`. If even a direct Python call drops, the gateway may fingerprint Python's TLS ClientHello — fall back to a libcurl-backed client (`curl_cffi`) |
| Step 0 reports `Model did not call any tool` | The gateway isn't forwarding the `tools` parameter; some gateways need a custom header like `X-Use-Tools: true` — check with whoever operates it |
| Step 0c lists every `customfield_*` for the issue | The Xray steps field has a non-standard human name; record the right ID and set `XRAY_STEPS_FIELD_ID` |
| `git check-ignore .env` exits non-zero | `.gitignore` was edited; restore the `.env` line so secrets stay untracked |

If you hit something that isn't in the table, add a row before you forget — this project is meant to be
shared, and the next adopter saves the hour you just spent.
