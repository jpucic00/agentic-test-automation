# syntax=docker/dockerfile:1
#
# AI test-generation pipeline container. Usage: README.md, "Run in Docker".
#
# Base: the official Playwright image — it already bundles Chromium and every system
# library Playwright needs (a slim Python base hits missing-lib errors at browser
# launch). The tag MUST match the project's Playwright version (1.60.0) so
# @playwright/test finds the browser build the image ships (@playwright/mcp needs its
# own Chromium, installed below). The
# -noble variant (Ubuntu 24.04) provides Python 3.12 natively (no deadsnakes PPA).
#
# Reproducibility: pin an immutable digest on first build, e.g.
#   FROM mcr.microsoft.com/playwright:v1.60.0-noble@sha256:<digest>
# (the digest can't be known until the image is pulled; confirm the tag exists on the
# build host — it pulls ~2GB from mcr.microsoft.com over the public internet).
FROM mcr.microsoft.com/playwright:v1.60.0-noble

# The Playwright image is Node-focused and doesn't ship a Python we can rely on.
# Ubuntu 24.04 (noble) carries python3.12 in its default repos — no deadsnakes PPA.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.12 python3.12-venv \
    && rm -rf /var/lib/apt/lists/*

# uv, version-pinned to the project's toolchain (never :latest in production).
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

# Reproducibility + air-gap hygiene.
#  - UV_PYTHON_DOWNLOADS=never: uv MUST use the base image's system python3.12 and
#    never fetch a managed interpreter (matters once the build runs against an
#    internal package mirror only).
#  - PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1: the browsers come from the base image and are
#    driven by Node only; neither `npm ci` nor the Python playwright package should
#    pull a second browser set.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PATH=/app/.venv/bin:$PATH

# Non-root user. The base image already ships `pwuser` at uid 1001 (the Playwright
# user, with access to the browsers in /ms-playwright), so uid 1001 is taken — rename
# pwuser → appuser rather than creating a new user. Keeps uid 1001 + browser access.
RUN usermod --login appuser --home /home/appuser --move-home pwuser \
    && groupmod --new-name appuser pwuser
ENV HOME=/home/appuser \
    XDG_CACHE_HOME=/home/appuser/.cache \
    NPM_CONFIG_CACHE=/home/appuser/.npm
WORKDIR /app
RUN chown -R appuser:appuser /app
USER appuser

# 1) Python deps first (best cache layer — changes rarely). Two-step install: deps
#    without the project (src/ isn't here yet), then the project after the code copy.
#    --frozen pins to uv.lock (fail on drift); --no-dev skips pytest/ruff.
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) Node test-harness deps. Copy the lockfile too and use `npm ci` (deterministic
#    from package-lock.json). Do NOT pass --omit=dev: @playwright/test is a
#    devDependency the runner needs. This installs @playwright/mcp (pinned in output/package.json) LOCALLY so
#    the orchestrator's hardcoded output/node_modules/@playwright/mcp/cli.js resolves.
COPY --chown=appuser:appuser output/package.json output/package-lock.json ./output/
RUN cd output && npm ci

# @playwright/mcp bundles its OWN playwright-core, pinned to a different Chromium
# revision than @playwright/test — the base image only ships the runner's. Without
# this the Planner/Healer MCP browser fails to launch ("browser not installed") while
# the test runner works. Installed into the DEFAULT cache (~/.cache/ms-playwright), not
# the base image's /ms-playwright: the MCP SDK launches the server with only an
# allow-listed env (HOME, PATH, ...), so PLAYWRIGHT_BROWSERS_PATH never reaches it.
RUN env -u PLAYWRIGHT_BROWSERS_PATH PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0 \
    node output/node_modules/@playwright/mcp/node_modules/playwright-core/cli.js install chromium

# 3) App code + runtime config (changes most often → last layer).
#    project_context.md / project_map.md are intentionally NOT copied (untracked,
#    per-adopter, may carry dummy creds) — bind-mount them at runtime.
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser playwright-mcp-config.json ./
COPY --chown=appuser:appuser output/playwright.config.ts ./output/
COPY --chown=appuser:appuser scripts/ ./scripts/

# Install the project itself now that src/ exists (fast — deps already resolved).
RUN uv sync --frozen --no-dev

# Runtime artifact dirs. config.load_config() also mkdirs plans/tests/snapshots, but
# pre-creating with appuser ownership keeps mounted named volumes writable by uid 1001.
RUN mkdir -p output/plans output/tests output/snapshots output/runs

# The Jira/Xray issue key (and optional --verbose) is passed as the run command:
#   docker run --rm --env-file .env -e GITLAB_ENABLED=false IMAGE QA-1234 --verbose
# --no-sync: the env is already synced above; never touch the network at runtime.
ENTRYPOINT ["uv", "run", "--no-sync", "python", "-m", "ai_test_gen.orchestrator"]
