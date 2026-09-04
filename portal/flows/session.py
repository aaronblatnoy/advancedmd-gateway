"""Portal session tools: status probe and deterministic re-login."""
from __future__ import annotations

import logging

from playwright.async_api import Page

from portal import browser
from portal.flows.login import ensure_logged_in, is_session_live, took_relogin

log = logging.getLogger("portal.flows.session")

__all__ = ["portal_session_status", "portal_login"]


def _safe_url(page: Page | None) -> str:
    if page is None or page.is_closed():
        return ""
    try:
        return page.url or ""
    except Exception:
        return ""


async def portal_session_status(page: Page, checkpoints=None) -> dict:
    """Report whether a live authenticated app window is present (no login)."""
    ctx = await browser.get_context()
    app = browser.find_app_page(ctx)
    live = bool(app is not None and await is_session_live(app))
    url = _safe_url(app) if live else _safe_url(page)
    log.info("flow=portal_session_status logged_in=%s", live)
    return {"logged_in": live, "url": url}


async def portal_login(page: Page, checkpoints=None) -> dict:
    """Ensure a live AMD portal session (deterministic Playwright login).

    Reuses a warm app window when Scheduler chrome is visible. If the
    session is closed or a zombie (app URL but not authenticated),
    performs an in-place re-login via the login iframe and returns the
    app page readiness flags. Does not scrape patient data.
    """
    app = await ensure_logged_in(page, checkpoints=checkpoints)
    reestablished = took_relogin()
    live = await is_session_live(app)
    log.info(
        "flow=portal_login logged_in=%s session_reestablished=%s",
        live,
        reestablished,
    )
    return {
        "logged_in": live,
        "session_reestablished": reestablished,
        "url": _safe_url(app),
    }
