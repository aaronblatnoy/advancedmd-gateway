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
    panels_closed = await _close_open_patient_panels(app)
    # Re-dismiss: closing a panel can re-pop a memo modal.
    await dismiss_blocking_dialogs(app)

    search_cleared = False
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
    except Exception:
        # Scheduler not reachable yet; the insurance flow's own
        # scheduler_open stage (with retries) will recover.
        pass

    log.info(
        "flow=reset_to_scheduler panels_closed=%s search_cleared=%s",
        panels_closed, search_cleared,
    )
    return {"panels_closed": panels_closed, "search_cleared": search_cleared}
