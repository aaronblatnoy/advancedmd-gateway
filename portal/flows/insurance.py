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
import os

from playwright.async_api import Page

from ._runner import AmbiguousMatchError, Checkpoints, _ambiguity_details
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


# Ambiguous search + System One declined -> check EVERY candidate row
# (owner decision 2026-09-27: "when faced between multiple check both").
# Bounded so a one-letter search cannot fan out across the whole practice.
def _fanout_enabled() -> bool:
    return (os.environ.get("PORTAL_AMBIGUOUS_FANOUT", "1") or "1").strip() != "0"


def _fanout_max() -> int:
    try:
        return int(os.environ.get("PORTAL_AMBIGUOUS_MAX_FANOUT", "4"))
    except ValueError:
        return 4


async def _run_with_fanout(
    runner, page, patient: str, insurance_index: int, checkpoints, patient_hint: str
) -> dict:
    """Run one graph; on a declined ambiguity, run it once per candidate row.

    Returns the single result when the search is unique or System One
    picked a row. Otherwise returns
    {"ambiguous": true, "candidates": n, "system_one": {...},
     "matches": [{"candidate": row_text, "ok": bool, ...fields | error}]}.
    """
    extra = {"patient_hint": patient_hint} if patient_hint else {}
    try:
        return await runner(
            page, patient, insurance_index, checkpoints=checkpoints, **extra
        )
    except AmbiguousMatchError as exc:
        cands = list(getattr(exc, "fanout_candidates", None) or exc.candidates)
        if not _fanout_enabled() or len(cands) < 2 or len(cands) > _fanout_max():
            raise
        cp = checkpoints
        if cp is not None:
            cp.note(
                f"system_one declined; checking all {len(cands)} candidate rows"
            )
        log.info("flow=insurance ambiguous fan-out candidates=%s", len(cands))
        matches: list[dict] = []
        for text in cands:
            sub_cp = Checkpoints(capture=False)
            item: dict
            try:
                data = await runner(
                    page, patient, insurance_index, checkpoints=sub_cp,
                    chosen_candidate=text, **extra,
                )
                item = {"candidate": text, "ok": True, **data}
            except Exception as sub_exc:  # isolate; keep checking the rest
                log.info(
                    "flow=insurance fan-out candidate failed error=%s",
                    type(sub_exc).__name__,
                )
                item = {
                    "candidate": text,
                    "ok": False,
                    "error": type(sub_exc).__name__,
                }
            item["trace"] = sub_cp.trace_lines()
            matches.append(item)
        if cp is not None:
            cp.note(
                f"checked {len(matches)} candidate rows "
                f"({sum(1 for m in matches if m['ok'])} ok)"
            )
        from .eligibility import aggregate_outcome

        return {
            "patient": patient,
            "insurance_index": insurance_index,
            "ambiguous": True,
            "candidates": len(cands),
            "system_one": dict(exc.s1),
            "matches": matches,
            # Closed verdict over the rows so the caller reads, not derives.
            "eligibility_outcome": aggregate_outcome(
                [str(m.get("eligibility_outcome") or "unverifiable") for m in matches if m.get("ok")]
            ),
        }


async def get_insurance_details(
    page: Page,
    patient: str,
    insurance_index: int = 1,
    checkpoints=None,
    patient_hint: str = "",
) -> dict:
    """patient_hint: optional caller context (DOB, appointment date, address)
    that System One uses to pick one row when the search is ambiguous. If
    it declines, every candidate row is checked (see _run_with_fanout)."""
    return await _run_with_fanout(
        run_get_insurance_details_graph, page, patient, insurance_index,
        checkpoints, patient_hint,
    )


async def check_eligibility(
    page: Page,
    patient: str,
    insurance_index: int = 1,
    checkpoints=None,
    patient_hint: str = "",
) -> dict:
    """Fire AMD Check Eligibility (billable) then scrape the fresh 271.

    Same whitelist as get_insurance_details. Gated at the executor /
    env layer — this body itself always performs the click when invoked.
    """
    return await _run_with_fanout(
        run_check_eligibility_graph, page, patient, insurance_index,
        checkpoints, patient_hint,
    )


# Fields a batch item result is allowed to carry (whitelist). No raw
# content ever leaves the server: only the scraped FIELDS plus these
# bounded metadata/status keys.
_BATCH_OK_KEYS = set(FIELDS) | set(ELIGIBILITY_FIELDS) | set(
    CLAIMS_ADDRESS_FIELDS
) | {
    "patient", "insurance_index", "index", "session_reestablished", "ok",
    "trace", "patient_disambiguation", "matched_candidate",
    "ambiguous", "candidates", "system_one", "matches", "eligibility_outcome",
}
_BATCH_ERR_KEYS = {
    "ok", "flow", "error", "message", "diagnosis", "next_action",
    "retryable", "run_id", "index", "session_reestablished", "trace",
    "candidates", "system_one",
}


def _normalize_patient_item(
    item, default_index: int
) -> tuple[str, int, str]:
    """Accept "last, first" / chart number, or
    {patient, insurance_index, patient_hint}."""
    if isinstance(item, dict):
        patient = str(item.get("patient", "")).strip()
        idx = item.get("insurance_index", default_index)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = default_index
        hint = str(item.get("patient_hint", "") or "").strip()
        return patient, idx, hint
    return str(item).strip(), default_index, ""


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
        patient, idx, hint = _normalize_patient_item(raw, insurance_index)
        item_cp = Checkpoints(capture=False)
        try:
            extra = {"patient_hint": hint} if hint else {}
            details = await get_insurance_details(
                page, patient, idx, checkpoints=item_cp, **extra
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
                **_ambiguity_details(exc),
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
