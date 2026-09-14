"""get_insurance_details flow.

Recorded navigation (see docs/insurance-flow.md for the full chain):
app page -> Scheduler -> frmScheduler iframe patient search -> pencil ->
frmPatientInfo iframe -> Insurance -> nested "Insurance N" iframe ->
passive claims-address read -> Details.

Orchestration is a LangGraph of deterministic Playwright stages with a
local-LLM recovery node when a stage fails recoverably. See
``portal/graphs/insurance_graph.py``.
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

from ._runner import Checkpoints
from portal.graphs.insurance_graph import (
    run_check_eligibility_graph,
    run_get_insurance_details_graph,
    run_insurance_navigation_graph,
)
from . import trace as _trace_mod

log = logging.getLogger("amd_portal_mcp")

# Fixed whitelist of scraped fields. Matches what the legacy insurance
# panel actually offers (plan_name does not exist there; coverage_type,
# group_name, subscriber_name, payer_id and eligibility_last_checked do).
FIELDS = [
    "carrier_name",
    "carrier_code",
    "coverage_type",
    "policy_number",
    "group_name",
    "group_number",
    "subscriber_name",
    "subscriber_relationship",
    "effective_date",
    "termination_date",
    "copay",
    "payer_id",
    "eligibility_status",
    "eligibility_last_checked",
]

# Re-export additive field groups for tests and the batch whitelist.
from .eligibility import ELIGIBILITY_FIELDS  # noqa: E402
from .claims_address import CLAIMS_ADDRESS_FIELDS  # noqa: E402


async def open_insurance_details(
    page: Page, patient: str, insurance_index: int = 1, checkpoints=None
):
    """Navigate to the insurance card iframe (LangGraph, navigation only).

    Marks checkpoints scheduler_open, patient_found, patient_info_open,
    insurance_card_open (login marks logged_in / app_ready).
    """
    flow = await run_insurance_navigation_graph(
        page, patient, insurance_index, checkpoints=checkpoints
    )
    return flow.app, flow.ins, flow.relogin


async def get_insurance_details(
    page: Page, patient: str, insurance_index: int = 1, checkpoints=None
) -> dict:
    return await run_get_insurance_details_graph(
        page, patient, insurance_index, checkpoints=checkpoints
    )


async def check_eligibility(
    page: Page, patient: str, insurance_index: int = 1, checkpoints=None
) -> dict:
    """Fire AMD Check Eligibility (billable) then scrape the fresh 271.

    Same whitelist as get_insurance_details. Gated at the executor /
    env layer — this body itself always performs the click when invoked.
    """
    return await run_check_eligibility_graph(
        page, patient, insurance_index, checkpoints=checkpoints
    )


# Fields a batch item result is allowed to carry (whitelist). No raw
# content ever leaves the server: only the scraped FIELDS plus these
# bounded metadata/status keys.
_BATCH_OK_KEYS = set(FIELDS) | set(ELIGIBILITY_FIELDS) | set(
    CLAIMS_ADDRESS_FIELDS
) | {
    "patient", "insurance_index", "index", "session_reestablished", "ok",
    "trace",
}
_BATCH_ERR_KEYS = {
    "ok", "flow", "error", "message", "diagnosis", "next_action",
    "retryable", "run_id", "index", "session_reestablished", "trace",
}


def _normalize_patient_item(item, default_index: int) -> tuple[str, int]:
    """Accept "last, first" / chart number, or {patient, insurance_index}."""
    if isinstance(item, dict):
        patient = str(item.get("patient", "")).strip()
        idx = item.get("insurance_index", default_index)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = default_index
        return patient, idx
    return str(item).strip(), default_index


def _whitelist_item(result: dict, allowed: set) -> dict:
    return {k: v for k, v in result.items() if k in allowed}


async def get_insurance_details_batch(
    page,
    patients: list,
    insurance_index: int = 1,
    checkpoints=None,
) -> dict:
    """Fetch insurance details for many patients over ONE warm session.

    Establishes the session ONCE (login is not repeated per patient),
    then iterates ``patients`` running the single-patient insurance flow
    with reset_to_scheduler between each so per-patient state does not
    accumulate. Each item gets its own liveness check via ensure_logged_in;
    on a mid-batch session expiry a single in-place re-login happens and
    the batch continues (never logs out). Per-item failures are isolated:
    a bad patient yields its structured error and the batch continues.

    Each ``patients`` entry is a scheduler search string / chart number,
    or a dict {"patient": str, "insurance_index": int}. Returns a summary
    dict plus per-item results, each whitelisted (no raw content) and
    carrying its 0-based ``index``.
    """
    from .login import ensure_logged_in, took_relogin

    cp = checkpoints if checkpoints is not None else Checkpoints(capture=False)

    # Establish the session once up front (also warms the app window).
    await ensure_logged_in(page, checkpoints=cp)
    took_relogin()  # clear any flag from the initial establish

    results: list[dict] = []
    relogins = 0
    ok_count = 0

    for i, raw in enumerate(patients):
        patient, idx = _normalize_patient_item(raw, insurance_index)
        item_cp = Checkpoints(capture=False)
        try:
            details = await get_insurance_details(
                page, patient, idx, checkpoints=item_cp
            )
            item = _whitelist_item(details, _BATCH_OK_KEYS)
            item["ok"] = True
            item["index"] = i
            # Per-item story (batch outer run_flow has its own session trace).
            item["trace"] = [
                _trace_mod.format_started("get_insurance_details"),
                *item_cp.trace_lines(),
                _trace_mod.format_completed("get_insurance_details"),
            ]
            if item.get("session_reestablished"):
                relogins += 1
            ok_count += 1
        except Exception as exc:  # isolate per-item failure; keep going
            log.info(
                "flow=insurance_batch item=%s failed error=%s",
                i, type(exc).__name__,
            )
            item = {
                "ok": False,
                "index": i,
                "error": type(exc).__name__,
                "message": type(exc).__name__,
                "trace": [
                    _trace_mod.format_started("get_insurance_details"),
                    *item_cp.trace_lines(),
                    _trace_mod.format_failed("get_insurance_details"),
                ],
            }
            item = _whitelist_item(item, _BATCH_ERR_KEYS)
        results.append(item)

    total = len(patients)
    return {
        "summary": {
            "total": total,
            "ok_count": ok_count,
            "failed_count": total - ok_count,
            "relogins": relogins,
        },
        "results": results,
    }


async def _input_value(ins, selector: str) -> str:
    loc = ins.locator(selector)
    try:
        if await loc.count():
            return (await loc.first.input_value()).strip()
    except Exception:
        pass
    return ""


async def _selected_option_text(ins, selector: str) -> str:
    loc = ins.locator(selector)
    try:
        if await loc.count():
            return (
                await loc.first.evaluate(
                    "e => e.selectedOptions && e.selectedOptions[0]"
                    " ? e.selectedOptions[0].textContent.trim() : ''"
                )
            ).strip()
    except Exception:
        pass
    return ""


async def _grid_cell(ins, selector: str, attr: str | None = None) -> str:
    loc = ins.locator(selector)
    try:
        if await loc.count():
            if attr:
                return (await loc.first.get_attribute(attr) or "").strip()
            return (await loc.first.inner_text()).strip()
    except Exception:
        pass
    return ""


# Selected coverage row in the legacy panel's coverage grid.
_ROW = "#tblInsCoverages tr[data-selected='1']"


async def _scrape_fields(ins) -> dict:
    return {
        # ellipsis widgets: the inner text input carries the chosen value
        "carrier_name": await _input_value(ins, "#ellCarrier input"),
        "carrier_code": await _input_value(ins, "#txtCarrierCode"),
        "coverage_type": await _selected_option_text(ins, "#selCoverage"),
        "policy_number": await _input_value(ins, "#txtSubScriberIDNumber"),
        "group_name": await _input_value(ins, "#txtGroupName"),
        "group_number": await _input_value(ins, "#txtGroupNumber"),
        "subscriber_name": await _input_value(ins, "#ellSubscriber input"),
        "subscriber_relationship": await _selected_option_text(
            ins, "#selInsHipaaRel"
        ),
        "effective_date": await _input_value(ins, "#txtInsBeginDate"),
        "termination_date": await _input_value(ins, "#txtInsEndDate"),
        "copay": await _input_value(ins, "#txtCopay"),
        "payer_id": await _input_value(ins, "#txtPayerID"),
        # eligibility columns of the selected coverage grid row
        "eligibility_status": await _grid_cell(
            ins, f"{_ROW} td:last-child", attr="title"
        ),
        "eligibility_last_checked": await _grid_cell(
            ins, f"{_ROW} td:nth-child(7)"
        ),
    }
