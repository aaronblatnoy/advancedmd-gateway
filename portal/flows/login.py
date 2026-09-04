"""AMD portal login flow.

Recorded against the real portal (see docs/insurance-flow.md):

- The login form lives inside iframe ``#frame-login`` on
  https://login.advancedmd.com/ (textboxes "Login name", "Password",
  "Office key"; button "Log in").
- A dialog with a "Close" button may cover the outer page first.
- Clicking "Log in" opens the real app as a POPUP window at
  static-100.advancedmd.com/amds/pm/app/. All flows run against that
  popup page, which ensure_logged_in() returns.
- With the persistent profile the session may already be live: an app
  window can already be open, or navigating to login.advancedmd.com can
  auto-open the app popup without showing the form.
"""
from __future__ import annotations

import asyncio
import logging
import os

from playwright.async_api import Page

from ..browser import AMD_APP_URL_MARKER, AMD_PORTAL_URL, find_app_page
from ._runner import Checkpoints

log = logging.getLogger("amd_portal_mcp")


def _is_app_page(page: Page) -> bool:
    return AMD_APP_URL_MARKER in page.url


async def _dismiss_dialog(page: Page) -> None:
    """Dismiss the announcement dialog on the login page if present."""
    close = page.get_by_role("button", name="Close")
    try:
        if await close.count():
            await close.first.click(timeout=3000)
            log.info("flow=login dismissed announcement dialog")
    except Exception:
        pass  # dialog absent or already gone


# In-app modal overlays (recorded via live DOM probe 2026-08-18): the
# "Patient Memo" dialog renders INSIDE iframe frmScheduler as
# div.modal[role="dialog"] (class "modal scheduler-modal fade in") with
# an i.amds-click-out-x close control and an OK button. Other AMD
# modals share the .modal / [role="dialog"] shape.
_DIALOG_SELECTORS = ['[role="dialog"]', ".modal-dialog", "mat-dialog-container"]
_CLOSE_SELECTORS = [
    "i.amds-click-out-x",
    'button[aria-label="Close"]',
    ".close",
    'button:has-text("OK")',
    'button:has-text("Close")',
]


async def _visible_dialog(root):
    """Return a visible modal dialog locator under root, else None."""
    for sel in _DIALOG_SELECTORS:
        loc = root.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(min(n, 4)):
            item = loc.nth(i)
            try:
                if await item.is_visible():
                    return item
            except Exception:
                continue
    return None


async def dismiss_blocking_dialogs(page: Page, max_passes: int = 3) -> bool:
    """Detect and close blocking AMD modals (e.g. "Patient Memo").

    Scans the app page and every child frame (modals render inside
    frmScheduler and frmPatientInfo iframes). Bounded: at most
    ``max_passes`` passes, each close click with a short timeout so the
    no-dialog case costs little. Returns True when a dialog is STILL
    visible afterwards (could not be dismissed). Logs presence booleans
    only, never dialog content.
    """
    frames = getattr(page, "frames", None)
    roots = list(frames) if frames else [page]

    async def _find():
        for root in roots:
            d = await _visible_dialog(root)
            if d is not None:
                return d
        return None

    seen = False
    for _ in range(max_passes):
        dialog = await _find()
        if dialog is None:
            break
        seen = True
        closed = False
        for csel in _CLOSE_SELECTORS:
            btn = dialog.locator(csel)
            try:
                if await btn.count():
                    await btn.first.click(timeout=2500)
                    closed = True
                    break
            except Exception:
                continue
        if not closed:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
        await asyncio.sleep(0.5)
    remaining = seen and (await _find()) is not None
    log.info(
        "flow=dialogs blocking dialog seen=%s remaining=%s", seen, remaining
    )
    return remaining


# Stable authenticated chrome: the "Scheduler" nav title is present and
# visible on the app page once the session is truly authenticated. A
# reused app window whose session has expired keeps the app URL but drops
# this element (the portal shows a re-login shell / blank), so a URL-only
# check is not enough.
_AUTH_CHROME_TITLE = "Scheduler"

# Set to True by the most recent ensure_logged_in() call when it had to
# tear down a zombie app window and log in fresh (in-place re-login).
# Callers read + clear it to surface a session_reestablished note.
_last_relogin = False


def took_relogin() -> bool:
    """Return and clear the in-place-relogin flag from the last login.

    ensure_logged_in() sets a module-level flag when it discovered a
    reused-but-dead app window and had to re-login in place. Callers
    (the batch/single flow) read this right after ensure_logged_in to
    decide whether to stamp ``session_reestablished`` on the result.
    """
    global _last_relogin
    v = _last_relogin
    _last_relogin = False
    return v


async def is_session_live(app_page: Page, timeout_ms: int = 5000) -> bool:
    """Probe that a reused app window is actually authenticated and live.

    Not just that the URL contains the app marker: verify a stable
    authenticated chrome element (the "Scheduler" nav title) is present
    and visible within a short timeout. Returns False on timeout or any
    exception (treat as expired). Never inspects page content.
    """
    if app_page is None or app_page.is_closed():
        return False
    if not _is_app_page(app_page):
        return False
    try:
        loc = app_page.get_by_title(_AUTH_CHROME_TITLE).first
        await loc.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def ensure_logged_in(page: Page, checkpoints=None) -> Page:
    """Return the logged-in APP page, logging in via the iframe if needed.

    Marks checkpoints ``logged_in`` (session established / login form
    submitted and the app popup appeared) and ``app_ready`` (app window
    finished loading).

    If a reused app window is found but ``is_session_live`` reports it is
    a zombie (URL still on the app but not authenticated/responsive), it
    is torn down and a fresh login is performed in place; the module-level
    relogin flag is set so callers can surface ``session_reestablished``.
    """
    global _last_relogin
    _last_relogin = False
    cp = checkpoints if checkpoints is not None else Checkpoints(capture=False)
    ctx = page.context

    reused = _is_app_page(page) or find_app_page(ctx) is not None
    if reused:
        candidate = page if _is_app_page(page) else find_app_page(ctx)
        if not await is_session_live(candidate):
            # Zombie: URL on app but not authenticated. Tear it down and
            # force a fresh login rather than reusing the dead window.
            log.info("flow=login reused app window is a zombie; re-login")
            _last_relogin = True
            reused = False
            try:
                await candidate.close()
            except Exception:
                pass
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    already = reused

    async with cp.stage("logged_in", page):
        app = await _obtain_app_page(page, ctx)

    async with cp.stage("app_ready", app):
        await app.wait_for_load_state("domcontentloaded")
        if not already:
            await asyncio.sleep(5)
        await dismiss_blocking_dialogs(app)
        log.info("flow=login app window ready")
    return app


async def _dismiss_iframe_maintenance(page: Page) -> bool:
    """Dismiss Required Maintenance / announcement inside #frame-login."""
    fl = page.frame_locator("#frame-login")
    dismissed = False
    for label in ("Snooze all", "Dismiss", "Snooze", "Close"):
        btn = fl.get_by_role("button", name=label)
        try:
            n = await btn.count()
            for i in range(min(n, 4)):
                item = btn.nth(i)
                if await item.is_visible():
                    await item.click(timeout=3000)
                    log.info("flow=login dismissed iframe maintenance (%s)", label)
                    dismissed = True
                    await asyncio.sleep(0.75)
        except Exception:
            continue
    return dismissed


async def _submit_login_form(frame) -> None:
    username = os.environ["AMD_USERNAME"]
    password = os.environ["AMD_PASSWORD"]
    office_key = os.environ["AMD_OFFICE_KEY"]
    await frame.get_by_role("textbox", name="Login name").fill(username)
    await frame.get_by_role("textbox", name="Password").fill(password)
    await frame.get_by_role("textbox", name="Office key").fill(office_key)
    await frame.get_by_role("button", name="Log in").click()
    log.info("flow=login credentials submitted")


async def _poll_for_app_page(ctx, *, seconds: int = 90):
    for _ in range(seconds):
        app = find_app_page(ctx)
        if app is not None and not app.is_closed():
            if await is_session_live(app, timeout_ms=8000):
                return app
        await asyncio.sleep(1)
    return None


async def _obtain_app_page(page: Page, ctx) -> Page:
    """Return a live app page, performing the iframe login if needed."""
    if _is_app_page(page):
        log.info("flow=login session already active (app page)")
        return page
    app = find_app_page(ctx)
    if app is not None and await is_session_live(app):
        log.info("flow=login session already active (app window open)")
        return app

    await page.goto(AMD_PORTAL_URL, wait_until="domcontentloaded")
    log.info("flow=login navigated to portal")
    await asyncio.sleep(2)

    # The profile session may auto-redirect / auto-open the app.
    if _is_app_page(page):
        return page
    app = find_app_page(ctx)
    if app is not None:
        return app

    await _dismiss_dialog(page)
    await _dismiss_iframe_maintenance(page)

    frame = page.frame_locator("#frame-login")
    await _submit_login_form(frame)
    await asyncio.sleep(4)
    # Post-submit AMD often surfaces Required Maintenance; snooze then retry.
    if await _dismiss_iframe_maintenance(page):
        if await frame.get_by_role("textbox", name="Login name").count():
            await _submit_login_form(frame)

    log.info("flow=login polling for app window")
    app = await _poll_for_app_page(ctx)
    if app is None:
        raise RuntimeError("app window did not open after login")
    return app
