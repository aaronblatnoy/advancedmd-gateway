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
import os
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
    "eligibility_outcome",       # closed: active | inactive | unverifiable | blocked (portal's verdict)
    "eligibility_click_skipped",  # bool: idempotent no-op, the row was already checked today
    "eligibility_last_checked_card",  # the selected row's Last Checked date as shown on the card
    "eligibility_blocked",       # bool: AMD cannot run eligibility on this plan (Details disabled)
    "eligibility_blocked_reason",  # closed: missing_payer_id | invalid_subscriber | not_eligible_plan | other
    "eligibility_click_fired",   # bool: check_eligibility mode clicked the card control (any row)
    "eligibility_grid_refreshed",  # bool: a Last-checked cell changed after a click (any row)
    "eligibility_rows",          # list: one closed record per coverage row (index, dates, skipped/blocked/fired/refreshed)
    "eligibility_rows_total",    # int: coverage rows in the grid
    "eligibility_rows_fired",    # int: rows whose Check Eligibility was clicked this call
    "eligibility_rows_refreshed",  # int: rows whose Last Checked advanced after the click
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
    d["eligibility_blocked"] = False
    d["eligibility_blocked_reason"] = ""
    d["eligibility_click_fired"] = False
    d["eligibility_grid_refreshed"] = False
    d["eligibility_click_skipped"] = False
    d["eligibility_last_checked_card"] = ""
    d["eligibility_service_types"] = []
    d["eligibility_outcome"] = "unverifiable"
    return d


async def selected_row_last_checked(ins) -> str:
    """The selected coverage row's Last Checked cell (mm/dd/yyyy) or ''."""
    try:
        loc = ins.locator("#tblInsCoverages tr[data-selected='1'] td:nth-child(7)")
        if await loc.count():
            return (await loc.first.inner_text()).strip()
    except Exception:
        pass
    return ""


#: AMD stamps Last Checked and eligibilityresponsedate on its own clock.
#: Observed 2026-09-30 02:55 EDT: a click stored 09/29 23:55, so the AMD day
#: is Pacific. "Today" for idempotency is AMD's today, not the container's.
AMD_TZ = os.environ.get("PORTAL_AMD_TZ", "America/Los_Angeles")


def amd_today():
    import datetime as _dt
    from zoneinfo import ZoneInfo

    try:
        return _dt.datetime.now(ZoneInfo(AMD_TZ)).date()
    except Exception:
        return _dt.date.today()


def checked_today(last_checked: str, today=None) -> bool:
    """True when the card's Last Checked date is AMD's current date (the card
    shows a date only, on AMD's clock). Makes check_eligibility idempotent
    within AMD's day: a second call does not fire a second billable inquiry."""
    import datetime as _dt

    today = today or amd_today()
    try:
        return _dt.datetime.strptime((last_checked or "").strip(), "%m/%d/%Y").date() == today
    except ValueError:
        return False


_ACTIVE_WORDS = ("active", "eligible", "covered")
_INACTIVE_WORDS = ("inactive", "not active", "ineligible", "terminated", "not eligible", "not covered")


def closed_outcome(elig: dict) -> str:
    """The portal's own closed verdict for one plan: active | inactive |
    unverifiable | blocked. Fail-closed: green needs a positive active
    status. Callers may still confirm green from the stored record."""
    if elig.get("eligibility_blocked") is True:
        return "blocked"
    if elig.get("eligibility_no_data") is True or elig.get("eligibility_available") is not True:
        return "unverifiable"
    status = " ".join(str(elig.get(k) or "") for k in ("eligibility_plan_status", "eligibility_status")).lower()
    if any(w in status for w in _INACTIVE_WORDS):
        return "inactive"
    if any(w in status for w in _ACTIVE_WORDS):
        return "active"
    return "unverifiable"


def aggregate_outcome(outcomes: list[str]) -> str:
    """Closed verdict over duplicate rows: green only if every checked row is
    active; all blocked -> blocked; any inactive with no active -> inactive;
    otherwise unverifiable."""
    if not outcomes:
        return "unverifiable"
    if all(o == "active" for o in outcomes):
        return "active"
    if all(o == "blocked" for o in outcomes):
        return "blocked"
    if "inactive" in outcomes and "active" not in outcomes:
        return "inactive"
    return "unverifiable"


async def blocked_eligibility(comment: str) -> dict:
    """The record for a plan AMD refuses to check (Details disabled)."""
    d = await _empty_eligibility(available=False, no_data=False)
    d["eligibility_blocked"] = True
    d["eligibility_blocked_reason"] = classify_blocked_reason(comment)
    d["eligibility_outcome"] = "blocked"
    return d


# Polling cadence. The only real limit is AMD's web app, so poll fast and
# let the deadline (in seconds) stay what it was.
_POLL_S = 0.25


def _ticks(seconds: float) -> int:
    return max(1, int(round(float(seconds) / _POLL_S)))


async def _find_eligibility_frame(app, timeout_s: int = 20):
    """Return the frmEligibilityDetails frame once attached, else None."""
    for _ in range(_ticks(timeout_s)):
        for f in app.frames:
            if getattr(f, "name", None) == ELIGIBILITY_FRAME_NAME:
                try:
                    await f.locator("body").wait_for(
                        state="attached", timeout=2000
                    )
                    return f
                except Exception:
                    pass
        await asyncio.sleep(_POLL_S)
    return None


async def _wait_settled(frame, timeout_s: int = 20) -> None:
    """Wait for the async body to finish loading (bounded)."""
    for _ in range(_ticks(timeout_s)):
        try:
            loading = await frame.locator(_LOADING).count()
            has_body = await frame.locator(f"{_BODY_INNER} > *").count()
            has_nodata = await _has_no_data(frame)
        except Exception:
            loading, has_body, has_nodata = 1, 0, False
        if has_nodata or (loading == 0 and has_body > 0):
            return
        await asyncio.sleep(_POLL_S)


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


_BLOCKED_REASONS = (
    ("missing eligibility payer", "missing_payer_id"),
    ("payer id", "missing_payer_id"),
    ("invalid/missing subscr", "invalid_subscriber"),
    ("invalid subscriber", "invalid_subscriber"),
    ("missing subscriber", "invalid_subscriber"),
    ("not eligible", "not_eligible_plan"),
)


def classify_blocked_reason(comment: str) -> str:
    """Closed reason for a plan whose Details/Check controls are disabled,
    from the grid's eligibility comment (PHI-free status text)."""
    low = (comment or "").lower()
    for needle, reason in _BLOCKED_REASONS:
        if needle in low:
            return reason
    return "other"


async def details_disabled(ins) -> bool:
    """True when the legacy card renders Details as a disabled control.

    Live 2026-09-29: plans flagged 'Missing Eligibility Payer ID' or
    'INVALID/MISSING SUBSCRIBER' show Details greyed out; clicking waits
    30 s for nothing. That is an insurance-setup gap, not a portal fault.
    """
    try:
        btn = ins.get_by_role("button", name="Details")
        if await btn.count() == 0:
            return False
        first = btn.first
        if await first.is_disabled():
            return True
        aria = await first.get_attribute("aria-disabled")
        cls = (await first.get_attribute("class")) or ""
        return aria == "true" or "disabled" in cls.split()
    except Exception:
        return False


async def _card_control(ins, label: str):
    """Stable id first (observed live), role/name and text as fallbacks."""
    by_id = {_CHECK_ELIGIBILITY_LABEL: _BTN_CHECK_ELIGIBILITY, "Details": _BTN_DETAILS}.get(label)
    if by_id:
        loc = ins.locator(by_id)
        if await loc.count():
            return loc
    btn = ins.get_by_role("button", name=label)
    if await btn.count() == 0:
        btn = ins.get_by_text(label, exact=True)
    return btn


async def _control_disabled(loc) -> bool:
    try:
        if await loc.count() == 0:
            return False
        first = loc.first
        if await first.is_disabled():
            return True
        aria = await first.get_attribute("aria-disabled")
        cls = (await first.get_attribute("class")) or ""
        return aria == "true" or "disabled" in cls.split()
    except Exception:
        return False


async def check_eligibility_disabled(ins) -> bool:
    """True when the card renders Check Eligibility as a disabled control."""
    return await _control_disabled(await _card_control(ins, _CHECK_ELIGIBILITY_LABEL))


# Observed live 2026-09-29 on the legacy card (test patient): coverage rows are
# <tr id="ins…" onclick="ins_gridSelectRow()" data-selected="1|null">; the
# header is a plain <tr>. Buttons carry stable ids.
_COVERAGE_ROWS = "#tblInsCoverages tr[id^='ins']"
_BTN_CHECK_ELIGIBILITY = "#btnEligibilityOnDemand"   # onclick="checkEligibility();"
_BTN_DETAILS = "#btnEligibilityDetails"              # onclick="showEligibilityDetails();"


async def select_coverage_row(ins, insurance_index: int = 1, timeout_s: int = 10) -> bool:
    """Click the coverage grid row (1 = primary) and wait until the card marks
    it selected. Owner 2026-09-29: "select the insurance and check
    eligibility" - the card's Check Eligibility acts on the selected row, so
    clicking it with nothing selected does nothing (25 of 29 not registered).
    """
    rows = ins.locator(_COVERAGE_ROWS)
    try:
        await rows.first.wait_for(state="visible", timeout=timeout_s * 1000)
    except Exception:
        log.info("flow=eligibility coverage grid not visible")
        return False
    n = await rows.count()
    idx = max(0, min(insurance_index - 1, n - 1))
    row = rows.nth(idx)
    try:
        await row.click(timeout=5000)
    except Exception as exc:
        log.info("flow=eligibility coverage row click failed type=%s", type(exc).__name__)
        return False
    for _ in range(_ticks(timeout_s)):
        try:
            if (await row.get_attribute("data-selected")) == "1":
                log.info("flow=eligibility coverage row selected index=%s rows=%s", insurance_index, n)
                return True
            sel = ins.locator("#tblInsCoverages tr[data-selected='1']")
            if await sel.count() and idx == 0 and n == 1:
                return True
        except Exception:
            pass
        await asyncio.sleep(_POLL_S)
    log.info("flow=eligibility coverage row not marked selected after click index=%s", insurance_index)
    return False


async def coverage_row_count(ins) -> int:
    try:
        return await ins.locator(_COVERAGE_ROWS).count()
    except Exception:
        return 0


def _row_last_checked_locator(ins, insurance_index: int):
    try:
        return ins.locator(_COVERAGE_ROWS).nth(insurance_index - 1).locator("td:nth-child(7)").first
    except Exception:
        return None


async def row_last_checked(ins, insurance_index: int) -> str:
    """Last Checked (column 7) of coverage row ``insurance_index`` (1-based)."""
    try:
        loc = ins.locator(_COVERAGE_ROWS).nth(insurance_index - 1).locator("td:nth-child(7)")
        if await loc.count():
            return (await loc.first.inner_text()).strip()
    except Exception:
        pass
    return ""


async def row_active_flag(ins, insurance_index: int) -> str:
    """The A/I column (6) of coverage row ``insurance_index``: 'A' active,
    'I' inactive, '' unknown. AMD silently ignores Check Eligibility on an
    inactive row (observed live 2026-09-30 on an ended plan)."""
    try:
        loc = ins.locator(_COVERAGE_ROWS).nth(insurance_index - 1).locator("td:nth-child(6)")
        if await loc.count():
            return (await loc.first.inner_text()).strip().upper()[:1]
    except Exception:
        pass
    return ""


async def _form_value(ins, selector: str) -> str:
    try:
        loc = ins.locator(selector)
        if await loc.count():
            return (await loc.first.input_value()).strip()
    except Exception:
        pass
    return ""


async def blocked_reason_for_selected_row(ins) -> str:
    """Closed reason for the selected row when AMD disables the controls.
    The grid comment is the first source; when it is blank (2026-09-30:
    four production charts, every one with an empty Subscriber ID and an
    empty comment cell) the card form's Subscriber ID and Payer ID fields
    decide. Never returns page text, only the closed enum."""
    reason = classify_blocked_reason(await selected_row_comment(ins))
    if reason != "other":
        return reason
    if not await _form_value(ins, "#txtSubscriberID"):
        return "invalid_subscriber"
    if not await _form_value(ins, "#txtPayerID"):
        return "missing_payer_id"
    return "other"


async def selected_row_comment(ins) -> str:
    """The selected row's Eligibility Comments title (AMD status text, no PHI)."""
    try:
        loc = ins.locator("#tblInsCoverages tr[data-selected='1'] td:last-child")
        if await loc.count():
            return (await loc.first.get_attribute("title") or "").strip()
    except Exception:
        pass
    return ""


async def check_all_coverage_rows(ins, *, force: bool = False, settle_s: int = 30,
                                  today=None) -> dict:
    """Owner 2026-09-30: "you should click for every insurance row. you click
    the row, then click check eligibility." For each row of the coverage
    grid, in grid order: select it, read its Last Checked, report it blocked
    when AMD disables the controls for that row, skip it when already
    checked today (idempotent), otherwise click Check Eligibility and wait
    for that row's Last Checked to change. One closed record per row; the
    aggregate flags keep the single-row contract callers already read.
    Logs carry indexes, counts and closed reasons only.
    """
    n = await coverage_row_count(ins)
    rows: list[dict] = []
    for i in range(1, n + 1):
        rec: dict = {"index": i, "selected": False, "active_flag": "", "blocked": False,
                     "blocked_reason": None, "skipped": False, "fired": False, "refreshed": False,
                     "last_checked_before": "", "last_checked_after": ""}
        rec["selected"] = await select_coverage_row(ins, i)
        rec["active_flag"] = await row_active_flag(ins, i)
        rec["last_checked_before"] = await row_last_checked(ins, i)
        if not rec["selected"]:
            log.info("flow=eligibility row %s/%s not selectable; not clicked", i, n)
            rows.append(rec)
            continue
        if await check_eligibility_disabled(ins) or await details_disabled(ins):
            rec["blocked"] = True
            rec["blocked_reason"] = await blocked_reason_for_selected_row(ins)
            log.info("flow=eligibility row %s/%s blocked reason=%s", i, n, rec["blocked_reason"])
        elif checked_today(rec["last_checked_before"], today) and not force:
            rec["skipped"] = True
            log.info("flow=eligibility row %s/%s already checked today; skipped (idempotent)", i, n)
        else:
            fired = await fire_check_eligibility_on_card(
                ins, insurance_index=i,
                grid_last_locator=_row_last_checked_locator(ins, i),
                settle_s=settle_s,
            )
            rec["fired"] = bool(fired.get("fired"))
            rec["refreshed"] = bool(fired.get("grid_last_changed"))
            if rec["fired"] and not rec["refreshed"]:
                log.info("flow=eligibility row %s/%s clicked, Last Checked unchanged active_flag=%s",
                         i, n, rec["active_flag"] or "?")
        rec["last_checked_after"] = await row_last_checked(ins, i)
        rows.append(rec)
    fired_n = sum(1 for r in rows if r["fired"])
    refreshed_n = sum(1 for r in rows if r["refreshed"])
    blocked = [r for r in rows if r["blocked"]]
    clickable = [r for r in rows if r["selected"] and not r["blocked"]]
    out = {
        "eligibility_rows": rows,
        "eligibility_rows_total": n,
        "eligibility_rows_fired": fired_n,
        "eligibility_rows_refreshed": refreshed_n,
        "eligibility_click_fired": fired_n > 0,
        "eligibility_grid_refreshed": refreshed_n > 0,
        # every clickable row was already checked today -> idempotent no-op
        "eligibility_click_skipped": bool(clickable) and all(r["skipped"] for r in clickable),
        # AMD refuses every row -> blocked (first row's reason)
        "eligibility_blocked": n > 0 and len(blocked) == n,
        "eligibility_blocked_reason": blocked[0]["blocked_reason"] if blocked and len(blocked) == n else None,
        "eligibility_last_checked_card": rows[0]["last_checked_after"] if rows else "",
    }
    log.info("flow=eligibility rows total=%s fired=%s refreshed=%s skipped=%s blocked=%s",
             n, fired_n, refreshed_n, sum(1 for r in rows if r["skipped"]), len(blocked))
    return out


async def fire_check_eligibility_on_card(ins, *, insurance_index: int = 1,
                                         grid_last_selector: str | None = None,
                                         grid_last_locator=None,
                                         settle_s: int = 30) -> dict:
    """Owner flow (2026-09-29): in the insurance panel, select the coverage
    row, then click Check Eligibility on the card. Green is confirmed by the
    caller from the stored record; this fires the inquiry, accepts a plain
    confirmation dialog if one appears (OK / Yes, never Save), and waits for
    the selected row's Last-checked cell to change.
    """
    selected = await select_coverage_row(ins, insurance_index)
    btn = await _card_control(ins, _CHECK_ELIGIBILITY_LABEL)
    await btn.first.wait_for(state="visible", timeout=15000)
    if grid_last_locator is None and grid_last_selector:
        grid_last_locator = ins.locator(grid_last_selector).first
    before = ""
    if grid_last_locator is not None:
        try:
            before = (await grid_last_locator.inner_text()).strip()
        except Exception:
            before = ""
    await _log_structure(ins, "ins-card-before-click")
    await btn.first.click(timeout=15000)
    log.info("flow=eligibility clicked Check Eligibility on the card (billable) row_selected=%s", selected)
    page = getattr(ins, "page", None)
    for _ in range(_ticks(3)):
        try:
            for root in ([page] if page is not None else []) + [ins]:
                for label in ("OK", "Yes"):
                    ok = root.get_by_role("button", name=label)
                    if await ok.count() and await ok.first.is_visible():
                        await ok.first.click(timeout=3000)
                        log.info("flow=eligibility confirmed dialog label=%s", label)
                        raise StopAsyncIteration
        except StopAsyncIteration:
            break
        except Exception:
            pass
        await asyncio.sleep(_POLL_S)
    changed = False
    if grid_last_locator is not None:
        for _ in range(_ticks(settle_s)):
            try:
                now = (await grid_last_locator.inner_text()).strip()
                if now and now != before:
                    changed = True
                    break
            except Exception:
                pass
            await asyncio.sleep(_POLL_S)
    log.info("flow=eligibility card click fired row_selected=%s grid_last_changed=%s", selected, changed)
    return {"fired": True, "row_selected": selected, "grid_last_changed": changed}


async def open_eligibility_frame(app, ins):
    """Click Details and return frmEligibilityDetails frame (deterministic).

    Raises on click/wait failure. Returns ``None`` when the frame never
    attaches (caller treats as empty eligibility).
    """
    details_btn = ins.get_by_role("button", name="Details")
    try:
        await details_btn.first.wait_for(state="visible", timeout=30000)
    except Exception:
        # The legacy card sometimes renders the control as a link or plain
        # text; one bounded fallback before giving up (live 2026-09-27:
        # eligibility_details_open was the most common failed stage).
        details_btn = ins.get_by_text("Details", exact=True)
        await details_btn.first.wait_for(state="visible", timeout=10000)
    await details_btn.first.click(timeout=15000)
    log.info("flow=eligibility clicked Details (read-only display)")
    frame = await _find_eligibility_frame(app, timeout_s=20)
    if frame is None:
        log.info("flow=eligibility frmEligibilityDetails did not attach")
    return frame


async def fire_check_eligibility(
    frame,
    *,
    settle_timeout_s: int = 60,
    refresh_timeout_s: int = 45,
    status_timeout_s: int = 60,
    nodata_confirm_s: int = 20,
) -> None:
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
    before = await _body_fingerprint(frame)
    await btn.first.click(timeout=15000)
    log.info("flow=eligibility fired Check Eligibility (gated write)")
    # The panel still shows the OLD on-file 271 at this instant, so a plain
    # settle check would return immediately and the caller would read stale
    # content. Wait for the refresh to actually start (loading indicator or
    # the body changing), then for it to finish, then for a status to be
    # readable. All bounded; every step logs booleans only.
    started = await _wait_refresh_started(frame, before, timeout_s=refresh_timeout_s)
    await _wait_settled(frame, timeout_s=settle_timeout_s)
    readable = await _wait_status_readable(frame, timeout_s=status_timeout_s)
    log.info(
        "flow=eligibility check refresh started=%s status_readable=%s",
        started, readable,
    )
    # While the payer request is in flight AMD clears the old response and
    # shows the no-data banner for a few seconds. Do not accept that banner
    # as the answer unless it holds steady with no loading indicator.
    if readable and await _has_no_data(frame):
        stable = await _wait_no_data_stable(frame, hold_s=nodata_confirm_s)
        log.info("flow=eligibility check no-data banner stable=%s", stable)
        # Six-for-six no-data after a click is not a payer pattern. Record
        # what the panel offers (fixed labels only) so the click target and
        # any service-type / confirm control can be identified.
        await _log_structure(frame, "after-click")
        await _log_controls(frame)


async def _body_fingerprint(frame) -> tuple[int, int]:
    """(length, hash) of the panel body text. Never leaves this module."""
    try:
        txt = await frame.locator("body").inner_text(timeout=4000)
    except Exception:
        return (-1, 0)
    return (len(txt), hash(txt))


async def _wait_refresh_started(frame, before, *, timeout_s: int) -> bool:
    """True once the panel visibly begins reloading after the click."""
    for _ in range(_ticks(timeout_s)):
        try:
            if await frame.locator(_LOADING).count():
                return True
            if await _has_no_data(frame):
                return True
            if await _body_fingerprint(frame) != before:
                return True
        except Exception:
            pass
        await asyncio.sleep(_POLL_S)
    return False


async def _wait_status_readable(frame, *, timeout_s: int) -> bool:
    """True once a plan status value or the no-data banner is present."""
    for _ in range(_ticks(timeout_s)):
        try:
            if await _has_no_data(frame):
                return True
            values = await frame.evaluate(_READ_JS, _LABEL_MAP)
            if isinstance(values, dict) and str(
                values.get("eligibility_plan_status") or ""
            ).strip():
                return True
        except Exception:
            pass
        await asyncio.sleep(_POLL_S)
    return False


async def _wait_no_data_stable(frame, *, hold_s: int) -> bool:
    """True when the no-data banner persists ``hold_s`` seconds with no
    loading indicator and no status value appearing. False the moment a
    status value shows up (the real 271 arrived) or loading resumes."""
    held = 0
    for _ in range(_ticks(hold_s) * 3):
        try:
            if await frame.locator(_LOADING).count():
                held = 0
            elif not await _has_no_data(frame):
                return False
            else:
                values = await frame.evaluate(_READ_JS, _LABEL_MAP)
                if isinstance(values, dict) and str(
                    values.get("eligibility_plan_status") or ""
                ).strip():
                    return False
                held += 1
                if held >= _ticks(hold_s):
                    return True
        except Exception:
            held = 0
        await asyncio.sleep(_POLL_S)
    return held >= _ticks(hold_s)


def classify_plan_status(value: str | None) -> str:
    """Closed PHI-free class of a scraped status: active/inactive/blank/other."""
    s = (value or "").strip().lower()
    if not s:
        return "blank"
    if any(m in s for m in ("inactive", "terminated", "not eligible", "ineligible",
                            "cancelled", "canceled", "not active")):
        return "inactive"
    if "active" in s or "eligible" in s:
        return "active"
    return "other"


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
        "flow=eligibility status_class=%s",
        classify_plan_status(result.get("eligibility_plan_status")),
    )
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


# Only controls that plausibly belong to the Details panel itself. Tab-strip
# closers are deliberately NOT here: hitting one closes the Scheduler tab.
_ELIGIBILITY_CLOSE_SELECTORS = (
    "i.amds-click-out-x",
    '[class*="click-out"]',
    'button[aria-label="Close"]',
    '[aria-label="Close"]',
    'mat-icon:has-text("close")',
    ".modal-header .close",
    '[class*="close-button"]',
    'button:has-text("Close")',
)

_STRUCTURE_PROBE_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(
      'button,[role=button],i,mat-icon,[class*=close],[class*=click-out],[aria-label]')) {
    const tag = el.tagName.toLowerCase();
    const cls = (el.getAttribute('class')||'').split(/\s+/).filter(c => /close|click|dismiss|x$|icon/i.test(c)).slice(0,2).join('.');
    const aria = (el.getAttribute('aria-label')||'').trim();
    const txt = (el.textContent||'').replace(/\s+/g,' ').trim();
    const label = aria && aria.length <= 20 && /^[A-Za-z _-]+$/.test(aria) ? aria
                : (tag === 'button' || tag === 'mat-icon') && txt.length <= 16 && /^[A-Za-z _-]+$/.test(txt) ? txt : '';
    const key = tag + '.' + cls + '#' + label;
    if (!cls && !label) continue;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(key);
    if (out.length >= 14) break;
  }
  return out;
}
"""


_CONTROLS_PROBE_JS = r"""
() => {
  const q = s => document.querySelectorAll(s).length;
  const vis = s => Array.from(document.querySelectorAll(s)).filter(e => e.offsetParent !== null).length;
  const btnLabels = Array.from(document.querySelectorAll('button'))
    .map(b => (b.getAttribute('aria-label') || b.textContent || '').replace(/\s+/g,' ').trim())
    .filter(t => t && t.length <= 24 && /^[A-Za-z ._-]+$/.test(t)).slice(0, 10);
  return {
    buttons: q('button'), buttons_visible: vis('button'), selects: q('select,mat-select,[role=combobox],[role=listbox]'),
    dialogs: vis('[role=dialog],.modal,mat-dialog-container'), loading: q('.amds-loading-text'),
    inputs: q('input'), labels: btnLabels
  };
}
"""


async def _log_controls(frame) -> None:
    """PHI-free counts of controls in the eligibility panel + button labels."""
    try:
        c = await frame.evaluate(_CONTROLS_PROBE_JS)
    except Exception as exc:
        log.info("flow=eligibility controls probe_failed=%s", type(exc).__name__)
        return
    if not isinstance(c, dict):
        return
    labels = " | ".join(str(x) for x in (c.get("labels") or []))[:120]
    log.info(
        "flow=eligibility controls buttons=%s visible=%s selects=%s dialogs=%s loading=%s inputs=%s labels=%s",
        c.get("buttons"), c.get("buttons_visible"), c.get("selects"), c.get("dialogs"),
        c.get("loading"), c.get("inputs"), labels,
    )


async def _log_structure(root, where: str) -> None:
    """PHI-free: tag, class fragments matching close/icon, fixed button labels."""
    try:
        if hasattr(root, "evaluate"):
            items = await root.evaluate(_STRUCTURE_PROBE_JS)
        else:  # FrameLocator (the insurance card): evaluate from its body element
            items = await root.locator("body").evaluate(_STRUCTURE_PROBE_JS)
    except Exception as exc:
        log.info("flow=eligibility structure %s probe_failed=%s", where, type(exc).__name__)
        return
    if not isinstance(items, list):
        return
    line = " ".join(str(i)[:40] for i in items)[:180]
    log.info("flow=eligibility structure %s: %s", where, line or "-")


async def close_eligibility_panel(app) -> bool:
    """Best-effort close of the Details / Check Eligibility panel.

    Leaving it open blocks the next flow's scheduler search. Tries the
    panel's own close controls (searching the app page and every frame),
    then Escape. Returns True when frmEligibilityDetails is gone.
    """
    roots = [app] + [f for f in getattr(app, "frames", []) or []]
    for _ in range(2):
        clicked = False
        for root in roots:
            for sel in _ELIGIBILITY_CLOSE_SELECTORS:
                try:
                    loc = root.locator(sel)
                    n = await loc.count()
                except Exception:
                    continue
                for i in range(min(n, 3)):
                    try:
                        item = loc.nth(i)
                        if await item.is_visible():
                            await item.click(timeout=2500)
                            clicked = True
                            log.info(
                                "flow=eligibility panel close clicked selector=%s in=%s",
                                sel, "app" if root is app else "frame",
                            )
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            if clicked:
                break
        if not clicked:
            try:
                await app.keyboard.press("Escape")
            except Exception:
                pass
        await asyncio.sleep(_POLL_S)
        if await _find_eligibility_frame(app, timeout_s=1) is None:
            log.info("flow=eligibility panel closed clicked=%s", clicked)
            return True
    log.info("flow=eligibility panel still open after close attempts")
    # Diagnostics so the next change targets the real control.
    frame = await _find_eligibility_frame(app, timeout_s=1)
    if frame is not None:
        await _log_structure(frame, "elig-frame")
    await _log_structure(app, "app-page")
    try:
        names = sorted({str(getattr(f, "name", "") or "") for f in app.frames} - {""})
        log.info("flow=eligibility frames: %s", " ".join(names)[:180])
    except Exception:
        pass
    return False
