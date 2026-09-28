"""Deterministic Playwright stages for get_insurance_details (no LLM).

LangGraph in ``portal/graphs/insurance_graph.py`` orchestrates these nodes and
routes to ``recovery_graph`` when a stage raises a recoverable error.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

from ._runner import (
    AmbiguousMatchError,
    BlockingDialogError,
    Checkpoints,
    PatientNotFoundError,
)
from .login import dismiss_blocking_dialogs, ensure_logged_in, took_relogin
from .state import reset_to_scheduler

_PATIENT_INFO_IFRAMES_DIAG = 'iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]'

log = logging.getLogger("amd_portal_mcp.stages")


@dataclass
class InsuranceFlowState:
    page: Page
    patient: str
    insurance_index: int
    checkpoints: Checkpoints
    app: Any = None
    sched: Any = None
    search: Any = None
    pinfo: Any = None
    ins: Any = None
    relogin: bool = False
    data: dict = field(default_factory=dict)
    # Optional caller context for System One disambiguation (DOB, appointment
    # date, address...). Free text; PHI; never logged.
    patient_hint: str = ""
    # Set by the graph's s1_disambiguate node: the exact option text to click
    # when the search returns several rows.
    chosen_candidate: str | None = None
    # Outcome of the System One pick (candidate count, probability), merged
    # into the result data as ``patient_disambiguation``.
    disambiguation: dict = field(default_factory=dict)


async def stage_session_and_scheduler(state: InsuranceFlowState) -> None:
    cp = state.checkpoints
    state.app = await ensure_logged_in(state.page, checkpoints=cp)
    state.relogin = took_relogin()
    await reset_to_scheduler(state.app)
    app = state.app

    async with cp.stage("scheduler_open", app):
        await dismiss_blocking_dialogs(app)
        state.sched = app.frame_locator('iframe[name="frmScheduler"]')
        state.search = state.sched.get_by_role("combobox", name="Search for patient")
        last_exc = None
        for attempt in range(3):
            await app.get_by_title("Scheduler").click()
            try:
                await state.search.wait_for(state="visible", timeout=10000)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                log.info(
                    "stage=scheduler_open attempt=%s dismiss dialogs", attempt + 1
                )
                try:
                    from .state import describe_ui_state
                    ui = await describe_ui_state(app)
                    log.warning("stage=scheduler_open blocked frames=%s",
                                " ".join(ui.get("frames", []))[:160])
                    log.warning("stage=scheduler_open blocked controls=%s", ui.get("summary", "")[:180])
                    n_pi = await app.locator(_PATIENT_INFO_IFRAMES_DIAG).count()
                    log.warning("stage=scheduler_open blocked patient_panels=%s", n_pi)
                except Exception:
                    pass
                if await dismiss_blocking_dialogs(app) and attempt == 2:
                    raise BlockingDialogError(
                        "a blocking dialog could not be dismissed"
                    ) from None
        if last_exc is not None:
            raise last_exc
        await state.search.click()


_CHART_HINT_RE = re.compile(r"chart(?:\s*number)?\s*[:=#]?\s*(\d{2,})", re.I)
_NAME_HINT_RE = re.compile(r"name\s*[:=]\s*\"?([^;\"\n]+?)\"?\s*(?:;|$)", re.I)


def chart_from_hint(hint: str) -> str:
    m = _CHART_HINT_RE.search(hint or "")
    return m.group(1) if m else ""


def name_from_hint(hint: str) -> str:
    m = _NAME_HINT_RE.search(hint or "")
    return m.group(1).strip() if m else ""


def _row_chart(text: str) -> str:
    """Leading chart number of a scheduler row ('2085 - VBMD ...' -> '2085')."""
    m = re.match(r"\s*(\d+)\s*-", text or "")
    return m.group(1) if m else ""


async def stage_patient_found(state: InsuranceFlowState) -> None:
    """Find and click the patient's scheduler row. Scripted first, model last.

    Order of decision (owner 2026-09-28: script whatever can be scripted):
    1. Known chart number (digit query or ``chart number N`` in the hint):
       search by NAME when one is in the hint (AMD's search returns no rows
       for some chart numbers), otherwise by the digits; then pick the row
       whose leading chart number equals ours, in code.
    2. No chart known, several rows: raise AmbiguousMatchError with the row
       texts; the graph asks System One, then fans out if it declines.
    """
    app = state.app
    sched = state.sched
    search = state.search
    patient = state.patient.strip()
    hint = state.patient_hint or ""
    cp = state.checkpoints

    chart = patient if patient.isdigit() else chart_from_hint(hint)
    name = name_from_hint(hint)
    # Search text: prefer the name when the chart is known and a name exists.
    search_text = name if (chart and name) else patient
    if search_text != patient:
        log.info("flow=insurance searching by name (chart known from hint)")

    async with cp.stage("patient_found", app):
        try:
            await search.fill("")
        except Exception:
            pass
        await search.press_sequentially(search_text, delay=10)
        log.info("flow=insurance patient search submitted")
        options = sched.get_by_role("option")
        if search_text.isdigit():
            result = options.filter(
                has_text=re.compile(rf"\b{re.escape(search_text)}\s*-")
            )
        else:
            result = options.filter(
                has_text=re.compile(re.escape(search_text), re.I)
            )
        try:
            await result.first.wait_for(state="visible", timeout=15000)
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                raise PatientNotFoundError(
                    "no search result matched the query"
                ) from None
            raise
        n = await result.count()
        texts = [t.strip() for t in await result.all_inner_texts()]

        if chart:
            # Deterministic pick: the row that starts with our chart number.
            matches = [i for i, t in enumerate(texts) if _row_chart(t) == chart]
            if len(matches) == 1:
                await result.nth(matches[0]).click()
                log.info(
                    "flow=insurance patient selected by chart rows=%s pick=%s",
                    n, matches[0],
                )
                return
            if not matches:
                log.info(
                    "flow=insurance rows=%s but none carry the known chart", n
                )
                raise PatientNotFoundError(
                    "no search result carries the requested chart number"
                )
            # Two rows with the same chart number cannot happen in AMD; if
            # the text ever does that, fall through to the ambiguity path.

        if n > 1:
            chosen = state.chosen_candidate
            if chosen and chosen in texts:
                idx = texts.index(chosen)
                await result.nth(idx).click()
                log.info(
                    "flow=insurance patient selected via system_one "
                    "candidates=%s pick=%s", n, idx
                )
                return
            log.info("flow=insurance patient search ambiguous candidates=%s", n)
            raise AmbiguousMatchError(candidates=texts)
        await result.first.click()
        log.info("flow=insurance patient selected")


async def stage_patient_info_open(state: InsuranceFlowState) -> None:
    app = state.app
    sched = state.sched
    cp = state.checkpoints

    async with cp.stage("patient_info_open", app):
        # No fixed sleeps: the only limit is AMD's web app, so wait on the
        # elements themselves. The Patient Memo modal, when it appears, is
        # caught by dismiss_blocking_dialogs (which polls) before each click.
        if await dismiss_blocking_dialogs(app):
            raise BlockingDialogError(
                "a blocking dialog could not be dismissed"
            )
        pencil = sched.locator(".amds-pencil")
        await pencil.wait_for(state="visible", timeout=15000)
        await pencil.click()
        log.info("flow=insurance opened patient info")
        pinfo_el = app.locator(
            'iframe[name^="frmPatientInfo"], iframe[id^="frmPatientInfo"]'
        ).last
        await pinfo_el.wait_for(state="attached", timeout=20000)
        state.pinfo = pinfo_el.content_frame
        nav = state.pinfo.locator("#cdk-drop-list-0").get_by_text("Insurance")
        await nav.wait_for(state="visible", timeout=20000)
        await dismiss_blocking_dialogs(app)
        await nav.click(timeout=20000)
        log.info("flow=insurance opened insurance list")
        # The accordion renders when the list has loaded; the card stage
        # then waits on the specific card.
        await state.pinfo.locator("cdk-accordion-item").first.wait_for(
            state="attached", timeout=20000
        )


async def stage_insurance_card_open(state: InsuranceFlowState) -> None:
    pinfo = state.pinfo
    idx = state.insurance_index
    cp = state.checkpoints
    app = state.app

    async with cp.stage("insurance_card_open", app):
        cards = pinfo.locator("cdk-accordion-item").filter(
            has=pinfo.get_by_text("Insurance", exact=True)
        )
        card = cards.nth(idx - 1)
        await card.wait_for(timeout=60000)
        if await card.get_attribute("aria-expanded") == "false":
            await card.click()
            # The legacy iframe attaches once the card expands; the body
            # wait below is the real gate, no clock needed.
            await card.locator("iframe.legacy-iframe").wait_for(
                state="attached", timeout=60000
            )
        state.ins = card.locator("iframe.legacy-iframe").content_frame
        await state.ins.locator("body").wait_for(state="attached", timeout=60000)
        log.info(
            "flow=insurance opened insurance card index=%s",
            idx,
        )
