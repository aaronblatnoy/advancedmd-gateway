"""Per-patient normalize: return the app to a clean Scheduler baseline.

Batch processing keeps one warm session across many patients. Without a
reset between patients the portal accumulates state: opening a second
patient's info stacks a NEW patient-info panel/iframe on top of the
prior one (the recording showed ``frmPatientInfo`` then
``frmPatientInfo2``), and leftover patient context can pop a "Patient
Memo" modal over the search box. reset_to_scheduler() runs at the START
of each single-patient flow so every run begins from a known baseline
whether or not a prior patient is loaded: dismiss blocking modals, close
any open patient-info panel left from a previous patient, and clear the
scheduler search box.

Bounded and timed: every wait carries an explicit short timeout; the
no-prior-patient case costs little. Never logs or returns page content.
"""
from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

from .login import dismiss_blocking_dialogs

log = logging.getLogger("amd_portal_mcp")

# Close controls for an open patient-info panel/tab. AMD renders the
# patient card in an iframe named/id frmPatientInfo(N); the surrounding
# tab chrome carries a close X. These selectors target that close control
# on the app page (outside the iframe).
_PATIENT_PANEL_CLOSE = [
    "i.amds-click-out-x",
    ".amds-tab-close",
    'button[aria-label="Close"]',
    ".tab .close",
    'span[title="Close"]',
]

_PATIENT_INFO_IFRAMES = (
    'iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]'
)


async def _close_open_patient_panels(app: Page, max_panels: int = 4) -> int:
    """Close any open patient-info panels stacked from prior patients.

    Returns the number of panels observed closed. Bounded by max_panels
    and short per-click timeouts so the common (no panel) case is cheap.
    """
    closed = 0
    for _ in range(max_panels):
        try:
            panels = app.locator(_PATIENT_INFO_IFRAMES)
            n = await panels.count()
        except Exception:
            break
        if n == 0:
            break
        # Try each known close control; stop when the panel count drops.
        did_close = False
        for sel in _PATIENT_PANEL_CLOSE:
            try:
                btn = app.locator(sel)
                if await btn.count():
                    await btn.last.click(timeout=2500)
                    did_close = True
                    break
            except Exception:
                continue
        await asyncio.sleep(0.4)
        try:
            n_after = await app.locator(_PATIENT_INFO_IFRAMES).count()
        except Exception:
            n_after = n
        if n_after < n:
            closed += 1
        if not did_close or n_after >= n:
            # Nothing left we know how to close, or click had no effect.
            break
    return closed


async def reset_to_scheduler(app: Page) -> dict:
    """Normalize the app to a clean Scheduler with an empty patient search.

    Steps (all bounded/timed):
    1. Dismiss blocking modals (e.g. leftover "Patient Memo").
    2. Close any open patient-info panel/tab from a previous patient so
       frmPatientInfo panels do not accumulate across patients.
    3. Click Scheduler and clear the search combobox.

    Returns a small status dict (presence booleans / counts only, no page
    content): {"panels_closed": N, "search_cleared": bool}.
    """
    await dismiss_blocking_dialogs(app)
    # A Details / Check Eligibility panel left open by the previous flow
    # covers the scheduler search box. Close it before the patient panels.
    try:
        from .eligibility import close_eligibility_panel, ELIGIBILITY_FRAME_NAME
        if any(getattr(f, "name", None) == ELIGIBILITY_FRAME_NAME
               for f in (getattr(app, "frames", None) or [])):
            await close_eligibility_panel(app)
    except Exception:
        pass
    panels_closed = await _close_open_patient_panels(app)
    # Re-dismiss: closing a panel can re-pop a memo modal.
    await dismiss_blocking_dialogs(app)

    search_cleared = False
    reloaded = False
    for attempt in range(2):
        try:
            await app.get_by_title("Scheduler").click(timeout=8000)
            sched = app.frame_locator('iframe[name="frmScheduler"]')
            search = sched.get_by_role("combobox", name="Search for patient")
            await search.wait_for(state="visible", timeout=8000)
            try:
                await search.fill("", timeout=4000)
                search_cleared = True
            except Exception:
                search_cleared = False
            break
        except Exception:
            if attempt == 0:
                # The scheduler view can be left unusable by a previous flow
                # (a closed tab, a navigated iframe). A reload of the app
                # shell keeps the session cookie and rebuilds the chrome.
                await _log_scheduler_state(app, "before-reload")
                try:
                    await app.reload(wait_until="domcontentloaded", timeout=30000)
                    await app.get_by_title("Scheduler").wait_for(
                        state="visible", timeout=30000
                    )
                    reloaded = True
                    await dismiss_blocking_dialogs(app)
                except Exception as exc:
                    log.info("flow=reset_to_scheduler reload failed type=%s", type(exc).__name__)
                    break
            # else: the insurance flow's own scheduler_open stage retries.

    log.info(
        "flow=reset_to_scheduler panels_closed=%s search_cleared=%s reloaded=%s",
        panels_closed, search_cleared, reloaded,
    )
    return {"panels_closed": panels_closed, "search_cleared": search_cleared}


async def _log_scheduler_state(app: Page, where: str) -> None:
    """PHI-free view of why the scheduler search may be unreachable."""
    try:
        opener = app.get_by_title("Scheduler")
        opener_visible = await opener.first.is_visible() if await opener.count() else False
    except Exception:
        opener_visible = None
    frame_url = "-"
    box = None
    combos = None
    try:
        for f in app.frames:
            if getattr(f, "name", None) == "frmScheduler":
                frame_url = (f.url or "").split("?")[0][-60:]
                combos = await f.get_by_role("combobox").count()
                break
        el = app.locator('iframe[name="frmScheduler"]')
        if await el.count():
            box = await el.first.bounding_box()
    except Exception:
        pass
    size = f"{int(box['width'])}x{int(box['height'])}" if box else "none"
    log.info(
        "flow=reset_to_scheduler state=%s opener_visible=%s sched_frame=%s comboboxes=%s url_tail=%s",
        where, opener_visible, size, combos, frame_url,
    )


async def describe_ui_state(app) -> dict:
    """PHI-free snapshot for diagnosing a blocked scheduler.

    Frame names (structural identifiers) plus the counts-only summary of the
    full interactable outline. Never labels, never page text.
    """
    out: dict = {"frames": [], "summary": ""}
    try:
        out["frames"] = sorted(
            {str(getattr(f, "name", "") or "") for f in (getattr(app, "frames", None) or [])}
            - {""}
        )[:12]
    except Exception:
        pass
    try:
        from ..recovery.observe import observe_page, phi_free_summary
        actions, dialog_seen = await observe_page(app)
        out["summary"] = phi_free_summary(actions, dialog_seen)
    except Exception:
        pass
    return out
