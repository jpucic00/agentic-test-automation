# Setup

This project is developed in two places: a **private PC** (authoring — write code, run static checks, commit) and a **company laptop** (runtime — execute scripts that hit the corporate LLM gateway, Jira/Xray, GitLab, and staging). The split exists because the corporate network is not reachable from the private PC.

Phase 0 (the current release) covers everything you need to bring both machines online and verify access.

---

## 1. Prerequisites (both machines)

| Tool | Min version | Why |
|---|---|---|
| Python | 3.12 | Pinned in [`.python-version`](.python-version); `pydantic-ai` and modern type-hint features need it |
| Node.js | 20 | Required by `@playwright/mcp` and the `output/` Playwright test harness (Phase 1.B) |
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

`brew install python@3.12` does **not** override the system `python3`. Use `python3.12` explicitly, or rely on `uv` to find it (recommended — `uv` reads `.python-version`).

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

## 2. Private PC (authoring) setup

Goal: clone the repo, set up the venv, run static checks. **Do not** run any `scripts/step0*` or `scripts/test_xray.py` here — they all need the corporate network.

```bash
git clone https://github.com/<your-fork>/agentic-test-automation.git
cd agentic-test-automation
uv sync                                        # creates .venv, installs deps, editable-installs ai_test_gen
uv run python -c 'import ai_test_gen'          # smoke-check the package layout imports
uv run ruff check src tests scripts            # lint
uvx pyright src scripts                        # type-check
uv run pytest                                  # offline unit suite (config, models, xray client) — no network

# Phase 1.B Node harness (optional here; lets `npx playwright test` compile generated tests):
(cd output && npm install)                      # → output/node_modules (gitignored); commit output/package-lock.json
```

If `uv sync` errors with "no interpreter found for Python 3.12", install Python 3.12 (see [Prerequisites](#1-prerequisites-both-machines)) — `uv` reads [`.python-version`](.python-version) and refuses to substitute a different minor version.

### 2.1 Dependency guardrail (Claude Code hook)

If you drive this repo with Claude Code, a `PreToolUse` hook ([`.claude/hooks/guard-deps.py`](.claude/hooks/guard-deps.py), wired in [`.claude/settings.json`](.claude/settings.json)) pauses for your explicit approval whenever a tool call would add or change a dependency — any `uv add` / `uv remove` / `uv lock` / `pip install` / `poetry add` / `conda install`, or a direct edit to `pyproject.toml` or `uv.lock`. It never blocks; it forces an "ask" prompt so a human vets the package (real name, actually needed, no typosquat) **before** it reaches the hash-pinned `uv.lock` and gets installed — including on the company laptop. `uv sync` (install-from-lock) is intentionally not gated.

- **Activate:** the first time you open this repo in Claude Code, approve the project hook when prompted, or run `/hooks` once to load it. Pure stdlib — it needs only `python3` on PATH.
- **Extend / disable:** edit the `DEP_COMMAND_MARKERS` and `DEP_FILES` lists at the top of the script (e.g. add `requirements.txt`), or remove the `hooks` block from `.claude/settings.json` (toggle via `/hooks`).

---

## 3. Company laptop (runtime) setup

Goal: clone the repo, fill in `.env`, run the three Step 0 verification scripts.

### 3.1 Toolchain

Install Python 3.12, Node 20, `uv`, and `git` exactly as in [section 1](#1-prerequisites-both-machines). Run the same `--version` checks.

### 3.2 Clone and sync

```bash
git clone https://github.com/<your-fork>/agentic-test-automation.git
cd agentic-test-automation
uv sync
```

### 3.3 Access prerequisites (file requests for missing ones now)

Confirm you have, or have filed access requests for, every item below. They typically take longer than the code does.

- [ ] **LLM gateway base URL** (OpenAI-compatible, e.g. `https://gw.internal/v1`)
- [ ] **LLM gateway API key**
- [ ] **Model names available on the gateway** — `openai/gpt-oss-120b`, `mistralai/devstral-small-2-2512`, `meta-llama/llama-3.3-70b-instruct` (or replacements)
- [ ] **Embedding + reranker model names** on the gateway (defaults: `mxbai-embed-large`, `bge-reranker-v2-m3`)
- [ ] **Jira/Xray credentials with read access** to your test project:
  - Cloud → Atlassian email + API token (from <https://id.atlassian.com/manage-profile/security/api-tokens>)
  - Server/DC → username + PAT (or password)
- [ ] **Jira base URL** (e.g. `https://yourcompany.atlassian.net` or self-hosted)
- [ ] **GitLab personal access token** with `api` scope (User Settings → Access Tokens), OR a project access token with `Developer` role + `write_repository`
- [ ] **Target GitLab repo** that will receive MRs (its path or numeric ID)
- [ ] **Staging app URL** under test + working credentials there

### 3.4 Create your `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Each section in [`.env.example`](.env.example) names the script that consumes it, so you can fill one section at a time. Watch out for:

- `LLM_BASE_URL` — include the trailing `/v1` if your gateway uses it.
- `XRAY_IS_CLOUD` — `true` for `*.atlassian.net`, `false` for self-hosted.
- `JIRA_TOKEN` — for Cloud this is an **API token**, not your Jira password.
- `GITLAB_PROJECT_ID` — `group/subgroup/project` path or the numeric ID. URL-encoded path is also accepted (`group%2Fproject`).
- `USE_HTTP_PROXY` — leave **unset** (the default) to connect **directly** and ignore the environment's `HTTP(S)_PROXY` (the gateway is reached over the VPN; this does not bypass the VPN). Set to `true` only if your gateway/Jira is reachable solely through a proxy. Routing through the env proxy is a common cause of "server disconnected without sending a response" even when the handshake succeeds.

`.env` is in [`.gitignore`](.gitignore) — it will never be staged.

#### Optional: mTLS client certificate

If the corporate gateway requires mutual TLS (the platform team will have given you a `.pfx` or `.p12` bundle plus a password), uncomment the mTLS block at the top of `.env`:

```
MTLS_PKCS12_FILE=/absolute/path/to/client.pfx
MTLS_PKCS12_PASSWORD=<password from IT>
```

`.pfx` and `.p12` are the same file format — `MTLS_PKCS12_FILE` accepts either extension. The path must be **absolute** (`.env` does not expand `~`). Sanity-check the bundle outside Python with:

```bash
openssl pkcs12 -in /absolute/path/to/client.pfx -info -noout -passin pass:"$MTLS_PKCS12_PASSWORD"
```

If your gateway also uses a private CA (rare for cloud-hosted gateways), point `SSL_CERT_FILE` at the corp root CA PEM bundle in the same block.

### 3.5 Run Step 0 verification

In order:

```bash
uv run python scripts/step0_verify_tool_calling.py
uv run python scripts/step0b_verify_embeddings.py
uv run python scripts/step0c_xray_flavor.py --issue-key <one-real-QA-key>
```

Plus two manual smokes:

```bash
# GitLab PAT reaches the target project (expect HTTP 200 + JSON):
curl -sS -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_BASE_URL/api/v4/projects/$(printf '%s' "$GITLAB_PROJECT_ID" | sed 's|/|%2F|g')" \
  | head -c 200

# Staging login works (open in browser):
open "$STAGING_BASE_URL"   # macOS; use xdg-open on Linux or start on Windows
```

Detailed expected output and failure-mode table is in [`scripts/README.md`](scripts/README.md).

### 3.6 Phase 1.A runtime check — Xray client

Phase 1.A adds one runtime check beyond Step 0. `step0c` only *detects* the steps
field; this exercises the actual client (`XrayClient.fetch()` → `ManualTestCase`).
Needs `XRAY_IS_CLOUD=false` and `JIRA_TOKEN` set to your PAT (sent as Bearer):

```bash
uv run python scripts/test_xray.py --issue-key <one-real-QA-key>
```

Expect a `ManualTestCase` JSON with **non-empty `steps` and `expected_results`**. If
your tenant's steps field isn't `customfield_11006`, set `XRAY_STEPS_FIELD_ID` to the
ID that `step0c_xray_flavor.py --issue-key` reports.

### 3.7 Phase 1.B runtime check — Playwright MCP & auth state

Phase 1.B adds the browser layer. These steps need the live staging app and a real
browser, so they run on the **company laptop** only.

```bash
# One-time: install the Chromium binary the Python `playwright` package drives.
uv run playwright install chromium

# Node harness for executing generated tests:
(cd output && npm install)

# Capture a logged-in session (a headed browser opens — watch it log in), then verify it:
uv run python scripts/save_auth_state.py
uv run python scripts/verify_auth_state.py        # exits 0 only if the saved state authenticates
```

Before the first run, adjust the login selectors in
[`scripts/save_auth_state.py`](scripts/save_auth_state.py) (`#username`, `#password`,
`#login-submit`, `/login`) and the post-login check route in
[`scripts/verify_auth_state.py`](scripts/verify_auth_state.py) to the real staging
app, and record them in [`project_map.md`](project_map.md) (auth flow). The session
is written to `output/storage_state.json` (gitignored); `build_playwright_mcp(...)`
passes it to Playwright MCP via `--storage-state` so agents start pre-authenticated.

---

## 4. Troubleshooting

| Symptom | Where | Fix |
|---|---|---|
| `uv sync` says "no Python 3.12 interpreter found" | Either machine | Install Python 3.12 via the platform installer in section 1; `uv` will not substitute another minor version |
| `step0*` scripts fail with "server disconnected without sending a response" (the TLS/mTLS handshake succeeds first) | Company laptop | Routing through the environment-configured proxy drops the request. The scripts ignore the env proxy **by default** and connect directly — verify you have **not** set `USE_HTTP_PROXY=true`, and that you are on the VPN (which provides the route; this does not bypass the VPN). A direct `curl` should return 200; the same `curl` through the proxy fails identically. If the endpoint is reachable **only** through a proxy, set `USE_HTTP_PROXY=true`. If even a direct Python call still drops, the gateway may fingerprint Python's TLS ClientHello — use a libcurl-backed client (`curl_cffi`) |
| Step 0 reports `Model did not call any tool` | Company laptop | Gateway is not forwarding `tools` parameter; some gateways need a custom header like `X-Use-Tools: true` — ask platform team |
| Step 0c lists every `customfield_*` for the issue | Company laptop | The Xray steps field has a non-standard human name; record the right ID manually and feed it into Phase 1.A config |
| `git check-ignore .env` exits non-zero | Either machine | `.gitignore` was edited; restore `.env` line so secrets stay untracked |

If you hit something that isn't in the table, add a row before you forget — the project is meant to be shared, and the next adopter saves the hour you just spent (see ["Documentation and shareability"](CLAUDE.md) in `CLAUDE.md`).
