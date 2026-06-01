"""Save a Playwright ``storage_state.json`` for the staging app (run once).

DEPRECATED / legacy: the pipeline uses context-driven login (agents and generated
tests log in live from the ``project_context.md`` dummy creds), so storage_state is
no longer part of the runtime path. This script is retained only as a manual
session-capture utility (e.g. to debug the Keycloak flow). Deletion is a separate
decision; ``STAGING_USERNAME``/``STAGING_PASSWORD`` in ``.env`` now feed only this
script and its verifier.

Launches a *headed* Chromium, logs into the staging app, and writes
``output/storage_state.json``.

Auth flow (staging app + Keycloak): the app has **no direct ``/login`` URL**.
Instead we open the app, click the Login control in the nav bar, follow the
redirect to Keycloak (a separate origin with a dynamic, non-hardcoded URL),
submit credentials on the standard Keycloak form, and wait to land back on the
app origin.

Re-run whenever the session expires (weekly is fine for most apps)::

    uv run python scripts/save_auth_state.py
    uv run python scripts/save_auth_state.py --output /tmp/state.json
    uv run python scripts/save_auth_state.py --login-button "#metaMenuItem5"

COMPANY LAPTOP ONLY: drives the live staging app + Keycloak, so it never runs on
the private dev PC. The selectors below are the known staging values; override
via the CLI flags if the app nav or Keycloak theme changes, and keep
``project_map.md`` (auth flow) in sync.

Implements AI_TEST_GENERATION_GUIDE.md §3.7 (Phase 1.B).
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from ai_test_gen.config import load_config

# --- Auth flow selectors ----------------------------------------------------
# The staging app has no /login route; a nav-bar control redirects to Keycloak.
# LOGIN_BUTTON is the app's nav login item; the KC_* selectors are the standard
# Keycloak login form (stable across themes). Override per environment via the
# CLI flags rather than editing this file.
LOGIN_BUTTON_SELECTOR = "#metaMenuItem5"
KC_USERNAME_SELECTOR = "#username"
KC_PASSWORD_SELECTOR = "#password"
KC_SUBMIT_SELECTOR = "#kc-login"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log into staging (via the Keycloak redirect) and save a "
        "Playwright storage_state.json.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to write storage_state.json (default: <output_dir>/storage_state.json).",
    )
    parser.add_argument(
        "--login-button",
        default=LOGIN_BUTTON_SELECTOR,
        help=f"Selector for the app's nav login control (default: {LOGIN_BUTTON_SELECTOR}).",
    )
    parser.add_argument(
        "--kc-username-selector",
        default=KC_USERNAME_SELECTOR,
        help=f"Keycloak username field selector (default: {KC_USERNAME_SELECTOR}).",
    )
    parser.add_argument(
        "--kc-password-selector",
        default=KC_PASSWORD_SELECTOR,
        help=f"Keycloak password field selector (default: {KC_PASSWORD_SELECTOR}).",
    )
    parser.add_argument(
        "--kc-submit-selector",
        default=KC_SUBMIT_SELECTOR,
        help=f"Keycloak submit button selector (default: {KC_SUBMIT_SELECTOR}).",
    )
    return parser.parse_args()


async def save_auth_state(
    output: Path | None,
    login_button: str,
    kc_username_selector: str,
    kc_password_selector: str,
    kc_submit_selector: str,
) -> Path:
    """Drive a headed browser through the Keycloak login and persist session state."""
    cfg = load_config()
    out = output or (cfg.output_dir / "storage_state.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    app_host = urlparse(cfg.staging_base_url).hostname or ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headed: watch login happen
        try:
            ctx = await browser.new_context(ignore_https_errors=True)
            page = await ctx.new_page()

            # 1. Open the app and click the nav Login control (no /login route).
            await page.goto(cfg.staging_base_url)
            await page.click(login_button)

            # 2. Keycloak (separate origin, dynamic URL): wait for its form, submit.
            await page.wait_for_selector(kc_username_selector)
            await page.fill(kc_username_selector, cfg.staging_username)
            await page.fill(kc_password_selector, cfg.staging_password)
            await page.click(kc_submit_selector)

            # 3. A successful submit bounces back to the app origin; wait for that
            #    (bad credentials keep us on Keycloak's host, so this raises — the
            #    right outcome). Then capture the authenticated session.
            await page.wait_for_url(
                lambda url: (urlparse(url).hostname or "") == app_host,
                wait_until="networkidle",
            )
            await ctx.storage_state(path=str(out))
        finally:
            await browser.close()
    return out


def main() -> None:
    args = _parse_args()
    out = asyncio.run(
        save_auth_state(
            args.output,
            args.login_button,
            args.kc_username_selector,
            args.kc_password_selector,
            args.kc_submit_selector,
        )
    )
    print(f"Saved auth state to {out}")


if __name__ == "__main__":
    main()
