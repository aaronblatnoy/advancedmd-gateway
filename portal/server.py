"""amd-portal-mcp MCP stdio server.

Exposes scripted AMD web-portal flows (Playwright) as MCP tools. The
browser lives inside this process; callers see only structured JSON,
never raw pages or screenshots.

READ-ONLY: every registered flow observes the portal. Flows that submit
or change AMD state require a decision file under memory/decisions/
with Aaron's sign-off, per repo invariants.

Every tool routes through flows._runner.run_flow, so callers get
{"ok": true, "data": {...}} on success and a structured
{"ok": false, "flow", "error", "message"} on failure (no tracebacks,
no page content). Milestone logs go to stderr only.
"""
from __future__ import annotations

import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

from . import browser
from .flows import insurance
from .flows._runner import run_flow

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _attach_file_log() -> None:
    """Also keep the PHI-free milestone log on disk.

    Under an MCP host the server's stderr is not persisted, so a slow or
    failed run could not be diagnosed after the fact (2026-09-28). Logs here
    are milestones, counts and class names only, never page content.
    """
    import os
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    target = Path(
        os.environ.get("AMD_PORTAL_LOG_FILE", "runtime/portal-stdio.log")
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logging.getLogger().addHandler(handler)
    except OSError:
        pass


_attach_file_log()

mcp = FastMCP("amd-portal-mcp")


@mcp.tool()
async def get_insurance_details(
    patient: str, insurance_index: int = 1, patient_hint: str = ""
) -> str:
    """Fetch a patient's insurance details from the AMD portal UI.

    patient is a scheduler search string ("last, first") or a chart
    number; insurance_index picks the coverage card (1 = primary,
    2 = secondary, ...). patient_hint is optional free-text context (date
    of birth, appointment date, address) used only when the search returns
    several rows: System One (on-box Winnow via s1-server) picks the row the
    request means, and the pick is taken only above a probability floor.
    If it declines, EVERY candidate row (up to PORTAL_AMBIGUOUS_MAX_FANOUT,
    default 4) is checked and the result is {"ok": true, "data":
    {"ambiguous": true, "candidates": n, "system_one": {...}, "matches":
    [{"candidate": row_text, "ok": bool, ...fields}]}}. Above the cap the
    call fails ambiguous_match with the candidate rows. Returns JSON {"ok": true, "data": {...}} with
    carrier name/code, coverage type, policy/group numbers, subscriber
    name/relationship, effective/termination dates, copay, payer id, and
    eligibility status/last-checked from the insurance card; the carrier's
    claims mailing address merged flat as claims_address_available,
    claims_address_line1, claims_address_line2, claims_city, claims_state,
    claims_zip, claims_carrier_name, claims_payer_id, and
    claims_address_reason; PLUS the real-time eligibility (271) carrier
    response already on file, scraped from the read-only "Details" panel
    and merged flat under an
    ``eligibility_`` prefix (eligibility_available, eligibility_no_data,
    eligibility_plan_status, eligibility_copay, eligibility_deductible,
    eligibility_out_of_pocket, eligibility_service_types, ...). When no
    carrier response is on file, eligibility_available is false and the
    benefit fields are empty (the card fields still return). Reading the
    claims address is passive and never opens the unverified carrier-detail
    control; until its selectors are confirmed, absence returns
    claims_address_available=false with a closed claims_address_reason.
    Reading the 271 panel is read-only: it clicks only "Details" (a display),
    never "Check Eligibility" (a billable inquiry). Or {"ok": false, ...}
    with a structured error.
    """
    page = await browser.get_page()
    result = await run_flow(
        "get_insurance_details",
        insurance.get_insurance_details,
        page,
        patient=patient,
        insurance_index=insurance_index,
        patient_hint=patient_hint,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_insurance_details_batch(
    patients: list, insurance_index: int = 1
) -> str:
    """Fetch insurance details for many patients over ONE warm session.

    patients is a list of scheduler search strings / chart numbers, or
    dicts {"patient": str, "insurance_index": int, "patient_hint": str}.
    insurance_index is
    the default coverage card index for plain-string items. Logs in ONCE
    and keeps the session warm across all patients (never logs out);
    normalizes state between patients; recovers from a mid-batch session
    expiry with a single in-place re-login and continues. Returns JSON
    {"summary": {total, ok_count, failed_count, relogins},
     "results": [{ok, index, ...whitelisted fields or structured error}]}.
    Per-item failures are isolated so one bad patient does not stop the
    batch. Screenshots are never taken for this tool.
    """
    page = await browser.get_page()
    result = await run_flow(
        "get_insurance_details_batch",
        insurance.get_insurance_details_batch,
        page,
        patients=patients,
        insurance_index=insurance_index,
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def check_eligibility(
    patient: str,
    insurance_index: int = 1,
    confirm: bool = False,
    patient_hint: str = "",
) -> str:
    """Fire AMD portal Check Eligibility (billable) then scrape the fresh 271.

    Owner-gated write. Requires the sidecar env
    AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED=1 and confirm=true. Opens the same
    insurance card path as get_insurance_details, clicks Check Eligibility
    inside frmEligibilityDetails, waits for the carrier response, and
    returns the same eligibility_* whitelist. Never clicks Save / Submit /
    Bypass. Returns JSON {"ok": true, "data": {...}} or {"ok": false, ...}.
    """
    import os

    if (os.environ.get("AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED") or "0").strip() != "1":
        return json.dumps(
            {
                "ok": False,
                "error": "tool_forbidden",
                "message": "AMD_PORTAL_CHECK_ELIGIBILITY_ENABLED=1 required",
            }
        )
    if not confirm:
        return json.dumps(
            {
                "ok": False,
                "error": "confirm_required",
                "message": "check_eligibility requires confirm=true",
            }
        )
    page = await browser.get_page()
    result = await run_flow(
        "check_eligibility",
        insurance.check_eligibility,
        page,
        patient=patient,
        insurance_index=insurance_index,
        patient_hint=patient_hint,
    )
    return json.dumps(result, indent=2)


async def _session_status(page) -> dict:
    from .flows.session import portal_session_status as _status

    return await _status(page)


@mcp.tool()
async def portal_login() -> str:
    """Ensure a live AMD portal web session (deterministic re-login).

    Reuses a warm authenticated app window when possible. If the session
    is closed or expired, logs in via the portal form using AMD_* env
    credentials and returns when the app window is ready. Does not
    scrape patient data. Returns JSON with logged_in,
    session_reestablished, and url (host path only in practice).
    """
    from .flows.session import portal_login as _login

    page = await browser.get_page()
    result = await run_flow("portal_login", _login, page)
    return json.dumps(result, indent=2)


@mcp.tool()
async def portal_session_status() -> str:
    """Report whether the portal browser session is alive and logged in."""
    page = await browser.get_page()
    result = await run_flow("portal_session_status", _session_status, page)
    return json.dumps(result, indent=2)


def main() -> None:
    try:
        mcp.run()
    finally:
        import asyncio

        asyncio.run(browser.shutdown())


if __name__ == "__main__":
    main()
