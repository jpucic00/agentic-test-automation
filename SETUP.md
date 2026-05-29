# Setup

This project is developed in two places: a **private PC** (authoring — write code, run static checks, commit) and a **company laptop** (runtime — execute scripts that hit the corporate LLM gateway, Jira/Xray, GitLab, and staging). The split exists because the corporate network is not reachable from the private PC.

Phase 0 (the current release) covers everything you need to bring both machines online and verify access.

---

## 1. Prerequisites (both machines)

| Tool | Min version | Why |
|---|---|---|
| Python | 3.12 | Pinned in [`.python-version`](.python-version); `pydantic-ai` and modern type-hint features need it |
| Node.js | 20 | Required by `@playwright/mcp` (Phase 1.B) |
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

Goal: clone the repo, set up the venv, run static checks. **Do not** attempt to run any `scripts/step0*` script here — they all need the corporate network.

```bash
git clone https://github.com/<your-fork>/agentic-test-automation.git
cd agentic-test-automation
uv sync                                        # creates .venv, installs Phase 0 deps
uv run python -m py_compile scripts/*.py       # syntax check
uv run ruff check scripts/                     # lint
```

If `uv sync` errors with "no interpreter found for Python 3.12", install Python 3.12 (see [Prerequisites](#1-prerequisites-both-machines)) — `uv` reads [`.python-version`](.python-version) and refuses to substitute a different minor version.

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

`.env` is in [`.gitignore`](.gitignore) — it will never be staged.

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

---

## 4. Troubleshooting

| Symptom | Where | Fix |
|---|---|---|
| `uv sync` says "no Python 3.12 interpreter found" | Either machine | Install Python 3.12 via the platform installer in section 1; `uv` will not substitute another minor version |
| `uv run python …` works on private PC but `step0*` scripts time out | Company laptop | Corporate proxy may be blocking the gateway — check with networking team |
| Step 0 reports `Model did not call any tool` | Company laptop | Gateway is not forwarding `tools` parameter; some gateways need a custom header like `X-Use-Tools: true` — ask platform team |
| Step 0c lists every `customfield_*` for the issue | Company laptop | The Xray steps field has a non-standard human name; record the right ID manually and feed it into Phase 1.A config |
| `git check-ignore .env` exits non-zero | Either machine | `.gitignore` was edited; restore `.env` line so secrets stay untracked |

If you hit something that isn't in the table, add a row before you forget — the project is meant to be shared, and the next adopter saves the hour you just spent (see ["Documentation and shareability"](CLAUDE.md) in `CLAUDE.md`).
