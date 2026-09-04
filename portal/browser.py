"""Shared Playwright session for AMD portal flows.

One persistent Chromium context per server process. The profile dir keeps
the AMD login session across runs so flows rarely need to re-auth.

The AMD portal has two windows: the login page at login.advancedmd.com
and the real app, which opens as a POPUP window at
static-100.advancedmd.com/amds/pm/app/. Flows must run against the app
window; flows.login.ensure_logged_in(page) returns it.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, async_playwright

# .env at the project root supplies AMD_USERNAME / AMD_PASSWORD /
# AMD_OFFICE_KEY (gitignored; never logged).
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PROFILE_DIR = os.environ.get(
    "AMD_PORTAL_PROFILE_DIR",
    os.path.expanduser("~/.amd-playwright-profile"),
)
AMD_PORTAL_URL = os.environ.get(
    "AMD_PORTAL_URL", "https://login.advancedmd.com"
)
# The real app lives on this path, opened as a popup after login.
AMD_APP_URL_MARKER = "advancedmd.com/amds/"
HEADLESS = os.environ.get("AMD_PORTAL_HEADLESS", "1") != "0"
# AMD sniffs the user agent and blocks "HeadlessChrome"; present a
# normal Chrome UA so the portal serves the real login form.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

_playwright = None
_context: BrowserContext | None = None


async def get_context() -> BrowserContext:
    global _playwright, _context
    if _context is None:
        _playwright = await async_playwright().start()
        _context = await _playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=HEADLESS,
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
        )
    return _context


def find_app_page(ctx: BrowserContext) -> Page | None:
    """Return an already-open app window, if the profile session has one."""
    for p in ctx.pages:
        if AMD_APP_URL_MARKER in p.url and not p.is_closed():
            return p
    return None


async def get_page() -> Page:
    """Return a page to start flows from.

    Prefers an already-open app window (persistent profile may still be
    logged in); otherwise any open page or a fresh one. Flows call
    login.ensure_logged_in() on it to obtain the definitive app page.
    """
    ctx = await get_context()
    app = find_app_page(ctx)
    if app is not None:
        return app
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


async def shutdown() -> None:
    global _playwright, _context
    if _context is not None:
        await _context.close()
        _context = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
