"""Verify a saved ``storage_state.json`` yields an authenticated staging session.

Loads ``output/storage_state.json`` into a fresh Playwright context, navigates to
a protected route, and confirms we are NOT bounced out to Keycloak. With the
Keycloak redirect flow there is no ``/login`` route to look for; instead we treat
"still on the app's own origin after hitting a protected route" as authenticated,
and "redirected to the Keycloak origin" as not. Run this right after
``save_auth_state.py`` to confirm the captured session actually authenticates
before you wire it into the agents::

    uv run python scripts/verify_auth_state.py
    uv run python scripts/verify_auth_state.py --check-path /dashboard

The ``--check-path`` MUST require authentication — an unauthenticated hit has to
redirect to Keycloak, which is exactly the signal this check relies on. A public
route would always look "authenticated".

Exits 0 when authenticated, 1 otherwise — usable as a company-laptop checklist
gate. Drives the live staging app, so company laptop only.

Implements AI_TEST_GENERATION_GUIDE.md §3.7 (Phase 1.B).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from ai_test_gen.config import load_config

# A route that REQUIRES authentication. Unauthenticated access must redirect to
# Keycloak (a different origin) — that redirect is how this check distinguishes
# authenticated from not. Adjust to a real protected route and keep
# project_map.md (auth flow) in sync.
DEFAULT_CHECK_PATH = "/dashboard"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check that a saved storage_state.json authenticates on staging.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="storage_state.json to load (default: <output_dir>/storage_state.json).",
    )
    parser.add_argument(
        "--check-path",
        default=DEFAULT_CHECK_PATH,
        help=f"Protected route to probe; must require auth (default: {DEFAULT_CHECK_PATH!r}).",
    )
    return parser.parse_args()


async def verify_auth_state(state: Path | None, check_path: str) -> bool:
    """Return True if ``state`` reaches ``check_path`` without redirecting to Keycloak."""
    cfg = load_config()
    state_path = state or (cfg.output_dir / "storage_state.json")
    if not state_path.exists():
        print(f"No storage state at {state_path} — run save_auth_state.py first.")
        return False

    app_host = urlparse(cfg.staging_base_url).hostname or ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(
                storage_state=str(state_path), ignore_https_errors=True
            )
            page = await ctx.new_page()
            await page.goto(f"{cfg.staging_base_url}{check_path}")
            await page.wait_for_load_state("networkidle")
            landed = page.url
        finally:
            await browser.close()

    landed_host = urlparse(landed).hostname or ""
    authenticated = landed_host == app_host
    state_desc = (
        "authenticated"
        if authenticated
        else f"NOT authenticated (redirected off-origin to {landed_host!r})"
    )
    print(f"{check_path} -> {landed} :: {state_desc}")
    return authenticated


def main() -> None:
    args = _parse_args()
    ok = asyncio.run(verify_auth_state(args.state, args.check_path))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
