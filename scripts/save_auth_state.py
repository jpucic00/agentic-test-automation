"""Save a Playwright ``storage_state.json`` for the staging app (run once).

Launches a *headed* Chromium, logs into the staging app with the configured
credentials, and writes ``output/storage_state.json``. Pass that file to
Playwright MCP via ``--storage-state`` (see
``src/ai_test_gen/playwright_mcp.py``) so agents start pre-authenticated instead
of burning tokens logging in on every run — the single biggest failure mode for
agentic browser testing.

Re-run whenever the session expires (weekly is fine for most apps)::

    uv run python scripts/save_auth_state.py                 # default output path
    uv run python scripts/save_auth_state.py --output /tmp/state.json

COMPANY LAPTOP ONLY: the login selectors below are placeholders from the guide.
Adjust ``LOGIN_*_SELECTOR`` / ``LOGIN_PATH`` to the real staging login form and
record them in ``project_map.md`` (auth flow). This script drives the live
staging app, so it only runs on the company laptop — never the private dev PC.

Implements AI_TEST_GENERATION_GUIDE.md §3.7 (Phase 1.B).
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from ai_test_gen.config import load_config

# --- Staging login form selectors ------------------------------------------
# Placeholders from the guide. Adjust to the real staging app and document the
# real values in project_map.md (auth flow). verify_auth_state.py probes the
# post-login route to confirm these worked.
LOGIN_PATH = "/login"
LOGIN_USERNAME_SELECTOR = "#username"
LOGIN_PASSWORD_SELECTOR = "#password"
LOGIN_SUBMIT_SELECTOR = "#login-submit"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log into staging and save a Playwright storage_state.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to write storage_state.json (default: <output_dir>/storage_state.json).",
    )
    parser.add_argument(
        "--login-path",
        default=LOGIN_PATH,
        help=f"Login route appended to STAGING_BASE_URL (default: {LOGIN_PATH}).",
    )
    return parser.parse_args()


async def save_auth_state(output: Path | None, login_path: str) -> Path:
    """Drive a headed browser through login and persist the session state."""
    cfg = load_config()
    out = output or (cfg.output_dir / "storage_state.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headed: watch login happen
        try:
            ctx = await browser.new_context(ignore_https_errors=True)
            page = await ctx.new_page()
            await page.goto(f"{cfg.staging_base_url}{login_path}")
            await page.fill(LOGIN_USERNAME_SELECTOR, cfg.staging_username)
            await page.fill(LOGIN_PASSWORD_SELECTOR, cfg.staging_password)
            await page.click(LOGIN_SUBMIT_SELECTOR)
            await page.wait_for_load_state("networkidle")
            await ctx.storage_state(path=str(out))
        finally:
            await browser.close()
    return out


def main() -> None:
    args = _parse_args()
    out = asyncio.run(save_auth_state(args.output, args.login_path))
    print(f"Saved auth state to {out}")


if __name__ == "__main__":
    main()
