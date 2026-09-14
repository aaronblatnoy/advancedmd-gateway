"""LangGraph orchestration for ``get_insurance_details``.

Pattern: almost entirely **deterministic** Playwright nodes (one per
checkpoint). When a node raises a recoverable error (blocking dialog,
navigation timeout), control routes to ``llm_recover`` (local Ollama via
``recovery_graph``), then retries the same stage. Non-recoverable errors
(ambiguous patient, not found) end immediately.

See ``memory/decisions/2026-09-02-portal-flows-langgraph-deterministic-plus-llm.md``.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from portal.flows._runner import Checkpoints
from portal.flows.eligibility import (
    ELIGIBILITY_FIELDS,
    fire_check_eligibility,
    open_eligibility_frame,
    read_eligibility_from_frame,
)
from portal.flows.claims_address import (
    CLAIMS_ADDRESS_FIELDS,
    scrape_claims_address,
)
from portal.flows.insurance_stages import (
    InsuranceFlowState,
    stage_insurance_card_open,
    stage_patient_found,
    stage_patient_info_open,
    stage_session_and_scheduler,
)
from portal.graphs.flow_support import (
    MAX_STAGE_RETRIES,
    is_recoverable_stage_error,
)

log = logging.getLogger("portal.graphs.insurance")

__all__ = [
    "run_get_insurance_details_graph",
    "run_check_eligibility_graph",
    "run_insurance_navigation_graph",
]

Mode = Literal["full", "navigation_only", "check_eligibility"]


class InsuranceGraphState(TypedDict, total=False):
    page: Any
    flow: InsuranceFlowState
    mode: Mode
    data: dict
    failed_stage: str | None
    retry_stage: str | None
    last_error: BaseException | None
    recovery_steps: int
    stage_retries: dict[str, int]
    aborted: bool


def _stage_retries(state: InsuranceGraphState) -> dict[str, int]:
    return dict(state.get("stage_retries") or {})


def _bump_retry(state: InsuranceGraphState, stage: str) -> dict[str, int]:
    retries = _stage_retries(state)
    retries[stage] = retries.get(stage, 0) + 1
    return retries


async def _run_stage(
    state: InsuranceGraphState, stage: str, fn
) -> dict:
    flow = state["flow"]
    try:
        await fn(flow)
        return {"failed_stage": None, "retry_stage": None, "last_error": None}
    except Exception as exc:
        log.warning("insurance_graph stage=%s error=%s", stage, type(exc).__name__)
        return {
            "failed_stage": stage,
            "retry_stage": stage,
            "last_error": exc,
        }


async def scheduler_open_node(state: InsuranceGraphState) -> dict:
    return await _run_stage(
        state, "scheduler_open", stage_session_and_scheduler
    )


async def patient_found_node(state: InsuranceGraphState) -> dict:
    return await _run_stage(state, "patient_found", stage_patient_found)


async def patient_info_open_node(state: InsuranceGraphState) -> dict:
    return await _run_stage(
        state, "patient_info_open", stage_patient_info_open
    )


async def insurance_card_open_node(state: InsuranceGraphState) -> dict:
    return await _run_stage(
        state, "insurance_card_open", stage_insurance_card_open
    )


async def fields_scraped_node(state: InsuranceGraphState) -> dict:
    from portal.flows.insurance import _scrape_fields

    flow = state["flow"]
    try:
        async with flow.checkpoints.stage("fields_scraped", flow.app):
            data = await _scrape_fields(flow.ins)
        return {
            "data": data,
            "failed_stage": None,
            "retry_stage": None,
            "last_error": None,
        }
    except Exception as exc:
        return {
            "failed_stage": "fields_scraped",
            "retry_stage": "fields_scraped",
            "last_error": exc,
        }


async def eligibility_node(state: InsuranceGraphState) -> dict:
    flow = state["flow"]
    mode = state.get("mode") or "full"
    stage = "eligibility_details_open"
    try:
        async with flow.checkpoints.stage(
            "eligibility_details_open", flow.app
        ):
            frame = await open_eligibility_frame(flow.app, flow.ins)
        if mode == "check_eligibility":
            if frame is None:
                raise RuntimeError(
                    "eligibility frame missing; cannot Check Eligibility"
                )
            stage = "eligibility_check_fired"
            async with flow.checkpoints.stage(
                "eligibility_check_fired", flow.app
            ):
                await fire_check_eligibility(frame)
        if frame is None:
            elig = await read_eligibility_from_frame(None)
        else:
            elig = await read_eligibility_from_frame(frame)
        data = dict(state.get("data") or {})
        data.update(elig)
        return {
            "data": data,
            "failed_stage": None,
            "retry_stage": None,
            "last_error": None,
        }
    except Exception as exc:
        return {
            "failed_stage": stage,
            "retry_stage": stage,
            "last_error": exc,
        }


async def claims_address_node(state: InsuranceGraphState) -> dict:
    """Passively merge the claims-address field group from the open card."""
    flow = state["flow"]
    try:
        async with flow.checkpoints.stage(
            "claims_address_scraped", flow.app
        ):
            claims = await scrape_claims_address(flow.ins)
        data = dict(state.get("data") or {})
        data.update(claims)
        return {
            "data": data,
            "failed_stage": None,
            "retry_stage": None,
            "last_error": None,
        }
    except Exception as exc:
        return {
            "failed_stage": "claims_address_scraped",
            "retry_stage": "claims_address_scraped",
            "last_error": exc,
        }


async def finalize_node(state: InsuranceGraphState) -> dict:
    from portal.flows.insurance import FIELDS

    flow = state["flow"]
    data = dict(state.get("data") or {})
    data["patient"] = flow.patient
    data["insurance_index"] = flow.insurance_index
    if flow.relogin:
        data["session_reestablished"] = True
    log.info(
        "insurance_graph done field presence: %s",
        {
            f: bool(data.get(f))
            for f in (*FIELDS, *CLAIMS_ADDRESS_FIELDS, *ELIGIBILITY_FIELDS)
        },
    )
    return {"data": data}


async def llm_recover_node(state: InsuranceGraphState) -> dict:
    from portal.graphs.recovery_graph import run_recovery

    stage = state.get("retry_stage") or state.get("failed_stage") or "unknown"
    flow = state["flow"]
    recovered, steps = await run_recovery(flow.page, goal_stage=stage)
    flow.checkpoints.recovery_steps += steps
    flow.checkpoints.note_recovery(stage, cleared=recovered)
    total = int(state.get("recovery_steps") or 0) + steps
    retries = _bump_retry(state, stage)
    if not recovered:
        return {
            "aborted": True,
            "recovery_steps": total,
            "stage_retries": retries,
        }
    return {
        "failed_stage": None,
        "last_error": None,
        "recovery_steps": total,
        "stage_retries": retries,
    }


def _route_after_stage(state: InsuranceGraphState, on_ok: str) -> str:
    if state.get("aborted"):
        return "fail"
    stage = state.get("failed_stage")
    if not stage:
        return on_ok
    exc = state.get("last_error")
    retries = _stage_retries(state).get(stage, 0)
    if exc and is_recoverable_stage_error(exc, stage) and retries < MAX_STAGE_RETRIES:
        return "llm_recover"
    return "fail"


def _route_after_recover(state: InsuranceGraphState) -> str:
    if state.get("aborted"):
        return "fail"
    target = state.get("retry_stage") or "scheduler_open"
    if target in ("eligibility_details_open", "eligibility_check_fired"):
        return "eligibility"
    if target == "claims_address_scraped":
        return "claims_address"
    return target


def _route_after_insurance_card(state: InsuranceGraphState) -> str:
    routed = _route_after_stage(state, "fields_scraped")
    if routed != "fields_scraped":
        return routed
    if state.get("mode") == "navigation_only":
        return "finalize"
    return "fields_scraped"


def _build_graph():
    g = StateGraph(InsuranceGraphState)

    g.add_node("scheduler_open", scheduler_open_node)
    g.add_node("patient_found", patient_found_node)
    g.add_node("patient_info_open", patient_info_open_node)
    g.add_node("insurance_card_open", insurance_card_open_node)
    g.add_node("fields_scraped", fields_scraped_node)
    g.add_node("claims_address", claims_address_node)
    g.add_node("eligibility", eligibility_node)
    g.add_node("finalize", finalize_node)
    g.add_node("llm_recover", llm_recover_node)

    g.add_edge(START, "scheduler_open")

    g.add_conditional_edges(
        "scheduler_open",
        lambda s: _route_after_stage(s, "patient_found"),
        {
            "patient_found": "patient_found",
            "llm_recover": "llm_recover",
            "fail": END,
        },
    )
    g.add_conditional_edges(
        "patient_found",
        lambda s: _route_after_stage(s, "patient_info_open"),
        {
            "patient_info_open": "patient_info_open",
            "llm_recover": "llm_recover",
            "fail": END,
        },
    )
    g.add_conditional_edges(
        "patient_info_open",
        lambda s: _route_after_stage(s, "insurance_card_open"),
        {
            "insurance_card_open": "insurance_card_open",
            "llm_recover": "llm_recover",
            "fail": END,
        },
    )
    g.add_conditional_edges(
        "insurance_card_open",
        _route_after_insurance_card,
        {
            "fields_scraped": "fields_scraped",
            "finalize": "finalize",
            "llm_recover": "llm_recover",
            "fail": END,
        },
    )
    g.add_conditional_edges(
        "fields_scraped",
        lambda s: _route_after_stage(s, "claims_address"),
        {
            "claims_address": "claims_address",
            "llm_recover": "llm_recover",
            "fail": END,
        },
    )
    g.add_conditional_edges(
        "claims_address",
        lambda s: _route_after_stage(s, "eligibility"),
        {
            "eligibility": "eligibility",
            "llm_recover": "llm_recover",
            "fail": END,
        },
    )
    g.add_conditional_edges(
        "eligibility",
        lambda s: _route_after_stage(s, "finalize"),
        {
            "finalize": "finalize",
            "llm_recover": "llm_recover",
            "fail": END,
        },
    )
    g.add_edge("finalize", END)

    g.add_conditional_edges(
        "llm_recover",
        _route_after_recover,
        {
            "scheduler_open": "scheduler_open",
            "patient_found": "patient_found",
            "patient_info_open": "patient_info_open",
            "insurance_card_open": "insurance_card_open",
            "fields_scraped": "fields_scraped",
            "claims_address": "claims_address",
            "eligibility": "eligibility",
            "fail": END,
        },
    )

    return g.compile()


_COMPILED = None


def _graph():
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = _build_graph()
    return _COMPILED


async def _invoke(
    page,
    patient: str,
    insurance_index: int = 1,
    checkpoints: Checkpoints | None = None,
    *,
    mode: Mode = "full",
) -> InsuranceGraphState:
    cp = checkpoints if checkpoints is not None else Checkpoints(capture=False)
    flow = InsuranceFlowState(
        page=page,
        patient=patient,
        insurance_index=insurance_index,
        checkpoints=cp,
    )
    initial: InsuranceGraphState = {
        "page": page,
        "flow": flow,
        "mode": mode,
        "data": {},
        "failed_stage": None,
        "retry_stage": None,
        "last_error": None,
        "recovery_steps": 0,
        "stage_retries": {},
        "aborted": False,
    }
    return await _graph().ainvoke(initial)


def _raise_if_failed(final: InsuranceGraphState) -> None:
    if final.get("last_error"):
        raise final["last_error"]
    if final.get("failed_stage") or final.get("aborted"):
        stage = final.get("failed_stage") or final.get("retry_stage") or "unknown"
        raise RuntimeError(f"insurance graph failed at stage {stage}")


async def run_get_insurance_details_graph(
    page,
    patient: str,
    insurance_index: int = 1,
    checkpoints: Checkpoints | None = None,
) -> dict:
    """Run the full LangGraph (nav + scrape + on-file eligibility Details)."""
    final = await _invoke(
        page, patient, insurance_index, checkpoints, mode="full"
    )
    _raise_if_failed(final)
    return dict(final.get("data") or {})


async def run_check_eligibility_graph(
    page,
    patient: str,
    insurance_index: int = 1,
    checkpoints: Checkpoints | None = None,
) -> dict:
    """Nav + Details + billable Check Eligibility + scrape fresh 271."""
    final = await _invoke(
        page,
        patient,
        insurance_index,
        checkpoints,
        mode="check_eligibility",
    )
    _raise_if_failed(final)
    return dict(final.get("data") or {})


async def run_insurance_navigation_graph(
    page,
    patient: str,
    insurance_index: int = 1,
    checkpoints: Checkpoints | None = None,
) -> InsuranceFlowState:
    """Navigation-only subgraph (through ``insurance_card_open``)."""
    final = await _invoke(
        page, patient, insurance_index, checkpoints, mode="navigation_only"
    )
    _raise_if_failed(final)
    return final["flow"]
