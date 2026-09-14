"""Eligibility (271) Details panel scrape.

The legacy insurance card has a "Details" button that opens a SEPARATE
iframe ``name="frmEligibilityDetails"`` showing the real-time eligibility
(271) carrier response already ON FILE for the coverage -- the
clearinghouse-replacement data. This module clicks that button and scrapes
a fixed whitelist of named fields from the panel.

READ-ONLY boundary (HARD)
-------------------------
This module clicks ONLY the "Details" button, which DISPLAYS the stored
271 response. It NEVER clicks "Check Eligibility" (fires a fresh
real-time inquiry -- a billable/write action) nor any Save Order / Bypass
/ write control. Details renders the response already on file; on a chart
with none it shows a "No Data Received From Carrier" banner, which we
report as a clean no-data state rather than an error (Aaron's directive).

Panel shape (mapped 2026-08-18 via a PHI-free structural capture)
-----------------------------------------------------------------
frmEligibilityDetails is a modern Angular Material SPA hosted at
``static-100.advancedmd.com/apps/eligibility-details/#/`` -- NOT the
legacy ``#txt...`` input form. It has no text inputs; it is a read-only
benefits DISPLAY built from mat- components and text nodes:

  .eligibility-details-page
    .header-container-outer
      .subscriber-container / .subscriber-text        (subscriber label)
      .patient-info .anchor-nav (xN)                  (service-type anchors)
      .service-type-and-check-eligibility-section
        button "Check Eligibility"   <-- NEVER CLICK (billable write)
    .body-container-outer
      .progress-bar-section .amds-loading-text        (async load spinner)
      .body-inner-container                           (benefit rows render here)
    .footer-container-outer .buttons-container
      button "Close" / button "Print ..."

Because the body loads asynchronously, we wait for the loading text to
clear and the body to populate (or for the no-data banner) before
reading. The scrape returns a bounded whitelist of named fields plus a
deterministic ``eligibility_available`` flag; it never returns raw panel
content.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("amd_portal_mcp")

# Frame opened by the legacy-frame "Details" button.
ELIGIBILITY_FRAME_NAME = "frmEligibilityDetails"

# Billable control. The read-only Details scrape never clicks it.
# Owner-gated ``check_eligibility`` (see memory/decisions/2026-09-14-…)
# is the ONE flow allowed to fire it.
_CHECK_ELIGIBILITY_LABEL = "Check Eligibility"
_CHECK_ELIGIBILITY_SECTION = ".service-type-and-check-eligibility-section"

# Whitelist of eligibility fields merged into the insurance result under an
# ``eligibility_`` prefix. Only fields that the read-only 271 Details panel
# exposes. ``eligibility_available`` is the deterministic presence flag;
# the rest are best-effort text reads that stay empty when absent.
ELIGIBILITY_FIELDS = [
    "eligibility_available",     # bool: a carrier response is on file
    "eligibility_no_data",       # bool: "No Data Received From Carrier"
    "eligibility_plan_status",   # e.g. Active / Inactive coverage
    "eligibility_plan_name",     # plan / product name if shown
    "eligibility_group",         # group name/number if shown
    "eligibility_coverage_dates",  # plan begin/end if shown
    "eligibility_copay",         # copay text if shown
    "eligibility_coinsurance",   # coinsurance text if shown
    "eligibility_deductible",    # deductible (indiv/family, met/remaining)
    "eligibility_out_of_pocket",  # OOP max (met/remaining) if shown
    "eligibility_service_types",  # list of benefit service-type sections
]

# No-data banner text (deterministic). Matched case-insensitively against
# the panel body; substring, so trailing punctuation does not matter.
_NO_DATA_MARKERS = (
    "no data received from carrier",
    "no eligibility data",
)

# Stable structural selectors confirmed from the PHI-free capture.
_LOADING = ".amds-loading-text"
_BODY_INNER = ".body-inner-container"
_SUBSCRIBER = ".subscriber-text"
_ANCHOR_NAV = ".patient-info .anchor-nav"


async def _empty_eligibility(available: bool, no_data: bool) -> dict:
    d = {f: "" for f in ELIGIBILITY_FIELDS}
    d["eligibility_available"] = available
    d["eligibility_no_data"] = no_data
    d["eligibility_service_types"] = []
    return d


async def _find_eligibility_frame(app, timeout_s: int = 20):
    """Return the frmEligibilityDetails frame once attached, else None."""
    for _ in range(timeout_s):
        for f in app.frames:
            if getattr(f, "name", None) == ELIGIBILITY_FRAME_NAME:
                try:
                    await f.locator("body").wait_for(
                        state="attached", timeout=2000
                    )
                    return f
                except Exception:
                    pass
        await asyncio.sleep(1)
    return None


async def _wait_settled(frame, timeout_s: int = 20) -> None:
    """Wait for the async body to finish loading (bounded)."""
    for _ in range(timeout_s):
        try:
            loading = await frame.locator(_LOADING).count()
            has_body = await frame.locator(f"{_BODY_INNER} > *").count()
            has_nodata = await _has_no_data(frame)
        except Exception:
            loading, has_body, has_nodata = 1, 0, False
        if has_nodata or (loading == 0 and has_body > 0):
            return
        await asyncio.sleep(1)


async def _has_no_data(frame) -> bool:
    """Deterministically detect the no-data banner (presence only)."""
    try:
        body = frame.locator("body")
        # Bounded text read used ONLY to test for the fixed banner string;
        # the boolean result is all that leaves this function.
        txt = (await body.inner_text(timeout=4000)).lower()
    except Exception:
        return False
    return any(m in txt for m in _NO_DATA_MARKERS)


# Benefit-label -> whitelist-field mapping. We read the VALUE sitting next
# to a known static LABEL rather than trusting per-tenant element ids we
# could not verify live. Kept conservative: only labels we are confident
# are stable field names on the 271 panel.
_LABEL_MAP = {
    "eligibility_plan_status": ["coverage status", "plan status", "status"],
    "eligibility_plan_name": ["plan name", "plan", "product"],
    "eligibility_group": ["group"],
    "eligibility_coverage_dates": ["plan begin", "coverage date", "effective"],
    "eligibility_copay": ["copay", "co-pay"],
    "eligibility_coinsurance": ["coinsurance", "co-insurance"],
    "eligibility_deductible": ["deductible"],
    "eligibility_out_of_pocket": ["out of pocket", "out-of-pocket"],
}

# JS that, for each label keyword, finds the nearest sibling/adjacent value
# text. Runs INSIDE the panel; returns a fixed dict of {field: value}. This
# is the one place a benefit VALUE is read; it returns only the whitelisted
# fields, never the whole panel, and never logs.
_READ_JS = r"""
(labelMap) => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const nodes = Array.from(document.querySelectorAll(
    'div,span,td,th,label,li'));
  function valueNear(el) {
    // sibling value: next element sibling with short non-empty text
    let sib = el.nextElementSibling;
    for (let i=0; i<3 && sib; i++, sib = sib.nextElementSibling) {
      const t = norm(sib.textContent);
      if (t && t.length <= 80) return t;
    }
    // else parent's text minus the label
    const p = el.parentElement;
    if (p) {
      const pt = norm(p.textContent);
      const lt = norm(el.textContent);
      const rest = norm(pt.replace(lt, ''));
      if (rest && rest.length <= 80) return rest;
    }
    return '';
  }
  const out = {};
  for (const [field, kws] of Object.entries(labelMap)) {
    let found = '';
    for (const el of nodes) {
      let own = '';
      for (const n of el.childNodes)
        if (n.nodeType === 3) own += n.nodeValue;
      own = norm(own).toLowerCase().replace(/[:*]+$/,'').trim();
      if (!own || own.length > 40) continue;
      if (kws.some(k => own === k || own.startsWith(k + ' ') || own === k)) {
        const v = valueNear(el);
        if (v) { found = v; break; }
      }
    }
    out[field] = found;
  }
  return out;
}
"""


async def _read_service_types(frame) -> list:
    """Return the service-type anchor labels (benefit section names).

    These are static section names (schema, not PHI). Bounded to a small
    count; empty on any error.
    """
    try:
        loc = frame.locator(_ANCHOR_NAV)
        n = min(await loc.count(), 24)
        names = []
        for i in range(n):
            try:
                t = (await loc.nth(i).inner_text(timeout=1500)).strip()
            except Exception:
                t = ""
            if t:
                names.append(t)
        return names
    except Exception:
        return []


async def open_eligibility_frame(app, ins):
    """Click Details and return frmEligibilityDetails frame (deterministic).

    Raises on click/wait failure. Returns ``None`` when the frame never
    attaches (caller treats as empty eligibility).
    """
    details_btn = ins.get_by_role("button", name="Details")
    await details_btn.first.wait_for(state="visible", timeout=15000)
    await details_btn.first.click(timeout=15000)
    log.info("flow=eligibility clicked Details (read-only display)")
    frame = await _find_eligibility_frame(app, timeout_s=20)
    if frame is None:
        log.info("flow=eligibility frmEligibilityDetails did not attach")
    return frame


async def fire_check_eligibility(frame, *, settle_timeout_s: int = 60) -> None:
    """Click the billable Check Eligibility control and wait for settle.

    Caller must already have opened ``frmEligibilityDetails``. This is the
    gated write used by the ``check_eligibility`` portal tool only — never
    by ``get_insurance_details`` / Details scrape.
    """
    if frame is None:
        raise RuntimeError("eligibility frame missing; cannot Check Eligibility")
    # Prefer role name (stable); fall back to the documented section button.
    btn = frame.get_by_role("button", name=_CHECK_ELIGIBILITY_LABEL)
    if await btn.count() == 0:
        btn = frame.locator(
            f"{_CHECK_ELIGIBILITY_SECTION} button"
        )
    await btn.first.wait_for(state="visible", timeout=15000)
    await btn.first.click(timeout=15000)
    log.info("flow=eligibility fired Check Eligibility (gated write)")
    await _wait_settled(frame, timeout_s=settle_timeout_s)


async def read_eligibility_from_frame(frame) -> dict:
    """Read whitelisted eligibility fields from an open panel frame."""
    if frame is None:
        return await _empty_eligibility(available=False, no_data=False)

    await _wait_settled(frame, timeout_s=20)

    if await _has_no_data(frame):
        log.info("flow=eligibility no carrier data on file")
        return await _empty_eligibility(available=False, no_data=True)

    result = await _empty_eligibility(available=True, no_data=False)
    try:
        values = await frame.evaluate(_READ_JS, _LABEL_MAP)
        if isinstance(values, dict):
            for field in _LABEL_MAP:
                v = values.get(field, "")
                if isinstance(v, str):
                    result[field] = v.strip()
    except Exception:
        log.info("flow=eligibility benefit-field read failed; presence-only")

    try:
        result["eligibility_service_types"] = await _read_service_types(frame)
    except Exception:
        result["eligibility_service_types"] = []

    log.info(
        "flow=eligibility field presence: %s",
        {
            f: bool(result[f])
            for f in ELIGIBILITY_FIELDS
            if f not in ("eligibility_available", "eligibility_no_data")
        },
    )
    return result


async def scrape_eligibility_details(app, ins, checkpoints=None) -> dict:
    """Click Details, open frmEligibilityDetails, scrape the 271 whitelist.

    ``app`` is the app page; ``ins`` is the legacy insurance frame locator
    returned by ``open_insurance_details``. Marks the checkpoint stage
    ``eligibility_details_open`` so failures localize to opening the panel.

    Returns the ELIGIBILITY_FIELDS whitelist. On a chart with no carrier
    response (the "No Data Received From Carrier" banner) returns
    ``eligibility_available=False`` / ``eligibility_no_data=True`` with the
    other fields empty -- NOT an error. Never returns raw panel content;
    never clicks Check Eligibility or any write control.
    """
    from ._runner import Checkpoints  # local import avoids a cycle

    cp = checkpoints if checkpoints is not None else Checkpoints(capture=False)

    async with cp.stage("eligibility_details_open", app):
        frame = await open_eligibility_frame(app, ins)

    return await read_eligibility_from_frame(frame)
