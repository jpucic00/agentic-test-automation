"""Verify a saved ``storage_state.json`` yields an authenticated staging session.

Loads ``output/storage_state.json`` into a fresh Playwright context, navigates to
a protected route, and asserts we are NOT bounced back to the login page. Run
this right after ``save_auth_state.py`` to confirm the captured session actually
authenticates before you wire it into the agents::

    uv run python scripts/verify_auth_state.py
    uv run python scripts/verify_auth_state.py --check-path /dashboard

Exits 0 when authenticated, 1 otherwise — usable as a company-laptop checklist
gate. Drives the live staging app, so company laptop only.

Implements AI_TEST_GENERATION_GUIDE.md §3.7 (Phase 1.B).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from ai_test_gen.config import load_config

# A route that requires authentication. Adjust to the real staging app and keep
# in sync with project_map.md (post-login landing). LOGIN_PATH is the route an
# unauthenticated session gets redirected to.
DEFAULT_CHECK_PATH = "/dashboard"
LOGIN_PATH = "/login"


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
        help=f"Protected route to probe (default: {DEFAULT_CHECK_PATH}).",
    )
    return parser.parse_args()


async def verify_auth_state(state: Path | None, check_path: str) -> bool:
    """Return True if loading ``state`` reaches ``check_path`` without a login redirect."""
    cfg = load_config()
    state_path = state or (cfg.output_dir / "storage_state.json")
    if not state_path.exists():
        print(f"No storage state at {state_path} — run save_auth_state.py first.")
        return False

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

    authenticated = LOGIN_PATH not in landed
    state_desc = "authenticated" if authenticated else "NOT authenticated (redirected to login)"
    print(f"{check_path} -> {landed} :: {state_desc}")
    return authenticated


def main() -> None:
    args = _parse_args()
    ok = asyncio.run(verify_auth_state(args.state, args.check_path))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
